"""The three community GitHub job feeds -- the safety net, and the finder's corpus.

These matter more than "safety net" makes them sound. Of the 50 seeded companies,
about twenty run proprietary career sites with no public job-board API and no
minable token: Apple, Google, Amazon, Microsoft, TikTok, ByteDance, Tesla, Meta,
JPMorgan and the HFT shops among them. Every one of those is present in these
feeds. Without this module they would be completely invisible to the monitor.

The feeds total ~23MB. At a 30-minute cadence that is roughly a gigabyte a day
of pointless transfer, so every read is a conditional GET -- `data/etags.json`
carries the ETags between runs and an unchanged feed costs one 304.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from ..http import FetchError, request_json_cached
from ..models import BoardResult, Listing, clean_locations, hashed_id, parse_ts

log = logging.getLogger(__name__)

ATS = "aggregator"

FEEDS: dict[str, str] = {
    "new-grad": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "summer2026": "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/.github/scripts/listings.json",
    "summer2027": "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
}

ETAG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "etags.json"
CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "feed_cache.json"


def parse(rows: Any, feed: str) -> list[Listing]:
    """Normalize one feed. Keeps only active AND visible rows."""
    if not isinstance(rows, list):
        raise FetchError(f"aggregator feed {feed} was {type(rows).__name__}, expected list")

    out: list[Listing] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not (row.get("active") and row.get("is_visible")):
            continue
        title = (row.get("title") or "").strip()
        company = (row.get("company_name") or "").strip()
        url = (row.get("url") or "").strip()
        if not title or not company:
            continue

        native = row.get("id")
        # The feeds carry their own stable id; fall back to a content hash only
        # when one is missing, since an unstable id means repeated pings.
        job_id = f"{ATS}:{feed}:{native}" if native else hashed_id(ATS, feed, title, url)

        out.append(
            Listing(
                id=job_id,
                company=company,
                title=title,
                url=url,
                locations=clean_locations(row.get("locations")),
                posted_at=parse_ts(row.get("date_posted")),
                source_ats=ATS,
                board_token=feed,
                employment_type=None,
                raw_category=row.get("category"),
                updated_at=parse_ts(row.get("date_updated")),
                # ~99% of rows say "Other", so this is weak signal -- useful when
                # it explicitly says "U.S. Citizenship is Required" (142 rows in a
                # 33k corpus) and ignorable otherwise.
                sponsorship=row.get("sponsorship"),
            )
        )
    return out


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


def fetch_all(*, session: requests.Session | None = None,
              use_cache: bool = True) -> BoardResult:
    """Read all three feeds. A feed that fails is skipped, not fatal."""
    etags = _load_json(ETAG_PATH) if use_cache else {}
    cache = _load_json(CACHE_PATH) if use_cache else {}
    listings: list[Listing] = []
    errors: list[str] = []
    new_etags = dict(etags)
    new_cache = dict(cache)

    for feed, url in FEEDS.items():
        # Only send If-None-Match when we actually still hold the body it refers
        # to. An ETag that outlives its cached payload turns a 304 into silently
        # zero listings -- the exact silent-miss failure this project exists to
        # avoid. (Both files live in the Actions cache, not git: losing them
        # costs one full download, which is harmless, unlike losing seen.json.)
        etag_to_send = etags.get(feed) if feed in cache else None
        try:
            payload, etag = request_json_cached(url, etag_to_send, session=session)
        except FetchError as exc:
            log.warning("aggregator feed %s failed: %s", feed, exc)
            errors.append(f"{feed}: {exc}")
            # Fall back to the cached copy so one flaky feed does not blind us.
            if feed in cache:
                listings.extend(parse(cache[feed], feed))
            continue

        if payload is None:  # 304 Not Modified
            log.debug("aggregator feed %s unchanged", feed)
            if feed in cache:
                listings.extend(parse(cache[feed], feed))
            continue

        listings.extend(parse(payload, feed))
        if etag:
            new_etags[feed] = etag
        new_cache[feed] = payload

    if use_cache:
        _save_json(ETAG_PATH, new_etags)
        _save_json(CACHE_PATH, new_cache)

    ok = len(errors) < len(FEEDS)
    return BoardResult(
        company="(aggregators)", ats=ATS, token="feeds", listings=listings,
        ok=ok, error="; ".join(errors) or None,
    )


def fetch_raw(*, session: requests.Session | None = None) -> list[dict[str, Any]]:
    """Raw rows across all feeds -- what the finder mines for new companies."""
    rows: list[dict[str, Any]] = []
    for feed, url in FEEDS.items():
        try:
            payload, _ = request_json_cached(url, None, session=session)
        except FetchError as exc:
            log.warning("aggregator feed %s failed: %s", feed, exc)
            continue
        if isinstance(payload, list):
            rows.extend(r for r in payload if isinstance(r, dict))
    return rows
