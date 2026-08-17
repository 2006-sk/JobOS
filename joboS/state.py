"""The seen-ID store -- the anti-duplicate-ping layer.

`data/seen.json` maps job id -> first-seen UTC epoch, and is committed back to
the repo. That commit is what makes state durable without a database.

Why a committed file and not the Actions cache: caches are evicted. An evicted
cache reads as an empty seen-set, and an empty seen-set means every job on every
board looks brand new -- roughly five thousand notifications in one run, months
after you last thought about this code. A committed file cannot silently vanish.

(The full board snapshot, `data/listings.json`, is deliberately NOT committed --
it is ~15MB of churning JSON and at 48 commits/day would add gigabytes of git
history. It is written locally and uploaded as a workflow artifact instead.)

THE ORDERING RULE, which is the whole point of this module:

    notify first, mark seen only after delivery succeeds.

The naive order -- mark seen, then notify -- loses a job forever if ntfy is down
or rate-limits that one call: the id is already recorded, so the next run treats
it as old and it never pings. A silent miss is the failure mode this project
exists to avoid. `--bootstrap` is the one sanctioned exception, because bootstrap
must record everything while sending nothing.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import Listing, now_ts

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "seen.json"
PRUNE_AFTER_DAYS = 180
SCHEMA_VERSION = 1


@dataclass
class SeenStore:
    """Job ids we have already notified about, with the time we first saw them."""

    path: Path
    entries: dict[str, int]
    # True when the store did not exist or held no entries. The workflow uses
    # this to auto-bootstrap, so a fresh clone can never fire 5,000 pings.
    was_empty: bool = False

    @classmethod
    def load(cls, path: Path | str | None = None) -> SeenStore:
        p = Path(path) if path else DEFAULT_PATH
        if not p.exists():
            log.info("no seen store at %s -- treating as first run", p)
            return cls(p, {}, was_empty=True)
        try:
            raw = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt store is NOT recoverable by starting fresh -- that would
            # re-notify everything. Fail loudly so the workflow's failure alert
            # fires and a human decides.
            raise RuntimeError(
                f"{p} is unreadable ({exc}). Refusing to continue: starting from "
                f"an empty store would re-notify every job. Restore it from git "
                f"history, or delete it deliberately and run with --bootstrap."
            ) from exc

        entries = raw.get("entries", raw) if isinstance(raw, dict) else {}
        clean = {str(k): int(v) for k, v in entries.items() if isinstance(v, (int, float))}
        return cls(p, clean, was_empty=not clean)

    def __contains__(self, job_id: str) -> bool:
        return job_id in self.entries

    def __len__(self) -> int:
        return len(self.entries)

    def unseen(self, listings: Iterable[Listing]) -> list[Listing]:
        """Listings we have never notified about.

        Deduplicates within the batch too: the same job can arrive from a direct
        board AND the aggregator feed in one run, and must produce one ping.
        """
        out: list[Listing] = []
        batch: set[str] = set()
        for listing in listings:
            if listing.id in self.entries or listing.id in batch:
                continue
            batch.add(listing.id)
            out.append(listing)
        return out

    def mark_seen(self, listings: Sequence[Listing] | Iterable[str], *, ts: int | None = None) -> int:
        """Record ids as seen. Call this only AFTER a successful notification."""
        stamp = ts if ts is not None else now_ts()
        added = 0
        for item in listings:
            job_id = item if isinstance(item, str) else item.id
            if job_id not in self.entries:
                self.entries[job_id] = stamp
                added += 1
        return added

    def prune(self, days: int = PRUNE_AFTER_DAYS, *, now: int | None = None) -> int:
        """Drop entries older than `days`.

        Safe because a job that has been gone from every board for six months is
        not coming back with the same id; keeping it forever would grow the file
        without bound and make every poll's git diff larger.
        """
        cutoff = (now if now is not None else now_ts()) - days * 86_400
        stale = [k for k, v in self.entries.items() if v < cutoff]
        for k in stale:
            del self.entries[k]
        if stale:
            log.info("pruned %d entries older than %d days", len(stale), days)
        return len(stale)

    def save(self) -> None:
        """Atomic write, sorted for a stable diff.

        Sorting matters: an unsorted dump reorders on every run and turns a
        one-line change into a whole-file diff, which defeats delta compression
        and makes the commit history useless for debugging.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "count": len(self.entries),
            "entries": dict(sorted(self.entries.items())),
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=1, sort_keys=False)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def should_bootstrap(store: SeenStore, explicit: bool = False) -> bool:
    """Bootstrap when asked, or when the store is empty.

    The auto-detect is what makes the very first workflow run safe without
    anyone remembering to pass a flag.
    """
    return explicit or store.was_empty
