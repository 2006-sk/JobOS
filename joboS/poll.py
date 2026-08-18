"""The poll: fetch every board, decide what is new, notify, then record.

The order of the last two steps is the entire point of this module, so it is
worth stating once more: we notify first and record second. If a send fails, the
ids stay unrecorded and the job pings again next run. Recording first would bury
it permanently after a single transient ntfy failure.

Three channels come out of a run:
  instant  -- core/adjacent early-career roles, one notification per run
  digest   -- stretch-tier roles and citizenship-excluded roles, queued to
              data/pending_digest.json and sent by the 09:00 UTC digest job
  none     -- everything else, recorded as seen so it never comes back
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .fetch import fetch_all, load_boards, tier_map
from .models import Listing
from .notify import Hit, build_notifier, format_digest, format_instant, notify_failure
from .relevance import classify, load_profile
from .state import SeenStore, should_bootstrap

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PENDING_DIGEST = DATA_DIR / "pending_digest.json"
LISTINGS_SNAPSHOT = DATA_DIR / "listings.json"


def _to_hits(listings: Sequence[Listing], tiers: dict[str, str],
             profile: dict[str, Any]) -> tuple[list[Hit], list[Hit], list[Hit]]:
    """Split listings into (instant, digest, irrelevant)."""
    instant: list[Hit] = []
    digest: list[Hit] = []
    other: list[Hit] = []
    for listing in listings:
        verdict = classify(
            listing.title,
            locations=listing.locations,
            sponsorship=listing.sponsorship,
            profile=profile,
        )
        hit = Hit(listing, verdict, tiers.get(listing.company.lower(), "unranked"))
        if verdict.channel == "instant":
            instant.append(hit)
        elif verdict.channel == "digest":
            digest.append(hit)
        else:
            other.append(hit)
    return instant, digest, other


def _load_pending() -> list[dict[str, Any]]:
    if not PENDING_DIGEST.exists():
        return []
    try:
        data = json.loads(PENDING_DIGEST.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_pending(rows: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIGEST.write_text(json.dumps(rows, indent=1) + "\n")


def run_poll(*, dry_run: bool = False, bootstrap: bool = False,
             no_aggregators: bool = False) -> int:
    boards = load_boards()
    profile = load_profile()
    notifier = build_notifier(dry_run=dry_run)

    listings, results = fetch_all(boards, include_aggregators=not no_aggregators)
    failed = [r for r in results if not r.ok]
    log.info("fetched %d listings from %d boards (%d failed)",
             len(listings), len(results), len(failed))

    # Written for debugging and uploaded as a workflow artifact. Deliberately
    # gitignored: ~15MB of churning JSON committed 48x/day would add gigabytes
    # of history for nothing.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LISTINGS_SNAPSHOT.write_text(
        json.dumps([x.to_dict() for x in listings], indent=1) + "\n"
    )

    store = SeenStore.load()
    if should_bootstrap(store, bootstrap):
        added = store.mark_seen(listings)
        store.prune()
        store.save()
        print(f"BOOTSTRAP: recorded {added} listings as seen, sent 0 notifications.")
        return 0

    new = store.unseen(listings)
    tiers = tier_map(boards)
    instant, digest, other = _to_hits(new, tiers, profile)
    log.info("new=%d instant=%d digest=%d other=%d",
             len(new), len(instant), len(digest), len(other))

    # Irrelevant listings can be recorded immediately -- they will never notify,
    # so there is no delivery to gate on.
    store.mark_seen([h.listing for h in other])

    # Digest-tier roles are queued durably, so recording them here is safe: the
    # digest job reads the queue, not the seen store.
    if digest:
        pending = _load_pending()
        pending.extend(
            {"listing": h.listing.to_dict(), "tier": h.tier,
             "level": h.verdict.level, "field": h.verdict.field,
             "excluded_reason": h.verdict.excluded_reason}
            for h in digest
        )
        _save_pending(pending)
        store.mark_seen([h.listing for h in digest])

    if instant:
        title, body = format_instant(
            instant, profile.get("notifications", {}).get("max_roles_per_notification", 15)
        )
        delivered = notifier.send(title, body, priority="high", tags=["briefcase"])
        if delivered:
            store.mark_seen([h.listing for h in instant])
            print(f"notified: {title}")
        else:
            # Left unrecorded on purpose -- these must ping again next run.
            log.error("notification FAILED; %d roles left unrecorded for retry",
                      len(instant))
            store.prune()
            store.save()
            return 1
    else:
        print("no new relevant roles; staying silent")

    store.prune()
    store.save()

    if failed:
        print(f"WARNING: {len(failed)} board(s) failed: "
              + ", ".join(f"{r.ats}:{r.token}" for r in failed[:8]))
    return 0


def run_digest(*, dry_run: bool = False) -> int:
    """Send the daily digest and clear the queue."""
    profile = load_profile()
    notifier = build_notifier(dry_run=dry_run)
    pending = _load_pending()

    stretch: list[Hit] = []
    excluded: list[Hit] = []
    for row in pending:
        listing = Listing.from_dict(row["listing"])
        verdict = classify(listing.title, locations=listing.locations,
                           sponsorship=listing.sponsorship, profile=profile)
        hit = Hit(listing, verdict, row.get("tier", "unranked"))
        (excluded if row.get("excluded_reason") else stretch).append(hit)

    store = SeenStore.load()
    health = f"Run health: seen store holds {len(store)} ids."
    if not pending:
        print("digest: nothing queued; staying silent")
        return 0

    title, body = format_digest(stretch, excluded, health,
                                profile.get("notifications", {}).get("max_roles_per_notification", 15))
    if notifier.send(title, body, priority="default", tags=["calendar"]):
        if not dry_run:
            _save_pending([])
        print(f"digest sent: {title}")
        return 0
    log.error("digest send FAILED; queue left intact for the next attempt")
    return 1


def run_test_notification(*, dry_run: bool = False) -> int:
    """Send one sample notification, touching no state.

    Exists so delivery can be verified on demand. The alternative -- waiting for
    a real posting -- conflates "notifications work" with "a job happened to be
    posted", and when nothing arrives you cannot tell which one failed.
    """
    notifier = build_notifier(dry_run=dry_run)
    sample = Listing(
        id="test:sample:1",
        company="JobOS",
        title="Test notification - setup is working",
        url="https://github.com/2006-sk/JobOS",
        locations=("San Jose, CA",),
        posted_at=None,
        source_ats="aggregator",
        board_token="test",
    )
    from .relevance import Verdict

    hit = Hit(sample, Verdict(level="new-grad", field="core", relevant=True,
                              channel="instant", location_rank=1), tier="1")
    title, body = format_instant([hit])
    if notifier.send(title, body, priority="high", tags=["white_check_mark"]):
        where = "your inbox (check spam on the first one)" if "smtp" in notifier.name \
            else "your phone"
        print(f"test notification sent via {notifier.name}. Check {where}.")
        return 0
    print(f"test notification FAILED via {notifier.name}. "
          f"Check the SMTP_* secrets (or NTFY_TOPIC) and the error above.")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m joboS.poll", description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the notification instead of sending it")
    parser.add_argument("--bootstrap", action="store_true",
                        help="record everything as seen and send nothing")
    parser.add_argument("--digest", action="store_true",
                        help="send the queued daily digest instead of polling")
    parser.add_argument("--no-aggregators", action="store_true",
                        help="skip the community feeds (direct boards only)")
    parser.add_argument("--test-notification", action="store_true",
                        help="send one sample notification and exit, to prove "
                             "delivery works without waiting for a real posting")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.test_notification:
            return run_test_notification(dry_run=args.dry_run)
        if args.digest:
            return run_digest(dry_run=args.dry_run)
        return run_poll(dry_run=args.dry_run, bootstrap=args.bootstrap,
                        no_aggregators=args.no_aggregators)
    except Exception:
        # A monitor that dies quietly is worse than no monitor: you keep
        # trusting it while it silently reports nothing. Always try to alert.
        err = traceback.format_exc()
        log.error("run failed:\n%s", err)
        if not args.dry_run:
            try:
                notify_failure(build_notifier(), err)
            except Exception:  # noqa: BLE001
                log.exception("could not even send the failure alert")
        return 1


if __name__ == "__main__":
    sys.exit(main())
