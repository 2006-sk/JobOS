"""Fan out across every configured board and return one normalized list.

Two properties this module guarantees, both of which exist because the failure
they prevent is silent:

  * **Per-board failure isolation.** One dead board logs a warning and the run
    continues. A single 404 must never fail the workflow -- that would mean
    missing every other company's postings for that cycle.
  * **Bounded concurrency (8).** We are an unauthenticated guest on other
    people's infrastructure. Eight parallel requests is brisk; eighty is abuse.
"""

from __future__ import annotations

import concurrent.futures as futures
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml

from .adapters import aggregators, ashby, greenhouse, lever, smartrecruiters, workday
from .models import BoardResult, Listing

log = logging.getLogger(__name__)

COMPANIES_PATH = Path(__file__).resolve().parent.parent / "config" / "companies.yaml"
MAX_WORKERS = 8


@dataclass
class Board:
    company: str
    ats: str
    token: str
    tier: str = "unranked"
    site: str | None = None
    host: str | None = None

    @property
    def label(self) -> str:
        return f"{self.ats}:{self.token}" + (f":{self.site}" if self.site else "")


def load_boards(path: Path | str | None = None) -> list[Board]:
    p = Path(path) if path else COMPANIES_PATH
    with p.open() as fh:
        data = yaml.safe_load(fh) or {}
    boards: list[Board] = []
    for entry in data.get("companies", []):
        boards.append(
            Board(
                company=entry["company"],
                ats=entry["ats"],
                token=entry["token"],
                tier=str(entry.get("tier", "unranked")),
                site=entry.get("site"),
                host=entry.get("host"),
            )
        )
    return boards


def tier_map(boards: Iterable[Board]) -> dict[str, str]:
    """company (lowercased) -> tier, for ranking notifications."""
    return {b.company.lower(): b.tier for b in boards}


def fetch_board(board: Board, session: requests.Session | None = None,
                *, max_pages: int | None = None) -> BoardResult:
    """Dispatch to the right adapter. Never raises."""
    try:
        if board.ats == "greenhouse":
            return greenhouse.fetch(board.token, board.company, session=session)
        if board.ats == "lever":
            return lever.fetch(board.token, board.company, session=session)
        if board.ats == "ashby":
            return ashby.fetch(board.token, board.company, session=session)
        if board.ats == "smartrecruiters":
            kw: dict[str, Any] = {"max_pages": max_pages} if max_pages else {}
            return smartrecruiters.fetch(board.token, board.company, session=session, **kw)
        if board.ats == "workday":
            kw = {"max_pages": max_pages} if max_pages else {}
            return workday.fetch(board.token, board.site or "", board.host or "",
                                 board.company, session=session, **kw)
    except Exception as exc:  # noqa: BLE001 -- isolation is the whole point
        # An adapter bug must not take down the run either. This is the backstop
        # behind each adapter's own error handling.
        log.exception("unexpected error fetching %s", board.label)
        return BoardResult(board.company, board.ats, board.token, [], ok=False,
                           error=f"{type(exc).__name__}: {exc}")
    return BoardResult(board.company, board.ats, board.token, [], ok=False,
                       error=f"unknown ats {board.ats!r}")


def fetch_all(
    boards: list[Board],
    *,
    include_aggregators: bool = True,
    max_pages: int | None = None,
    max_workers: int = MAX_WORKERS,
) -> tuple[list[Listing], list[BoardResult]]:
    """Poll every board concurrently. Returns (listings, per-board results)."""
    results: list[BoardResult] = []
    session = requests.Session()

    with futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending = {
            pool.submit(fetch_board, b, session, max_pages=max_pages): b for b in boards
        }
        for fut in futures.as_completed(pending):
            results.append(fut.result())

    listings: list[Listing] = []
    for res in results:
        if res.ok:
            listings.extend(res.listings)
        else:
            log.warning("board %s:%s failed: %s", res.ats, res.token, res.error)

    if include_aggregators:
        # The safety net. Companies like Apple, Google and TikTok run proprietary
        # career sites with no minable board token, so the aggregator feeds are
        # the ONLY way they are visible at all.
        agg_result = aggregators.fetch_all(session=session)
        results.append(agg_result)
        if agg_result.ok:
            watched = {b.company.lower() for b in boards}
            kept = [
                x for x in agg_result.listings
                if _company_watched(x.company, watched)
            ]
            # Restricting to the watchlist is what keeps volume at single digits
            # per day: the feeds cover ~3,450 companies, and passing all of them
            # through measured ~22 relevant roles/day instead of ~4.
            log.info("aggregators: %d listings, %d on watchlist", len(agg_result.listings), len(kept))
            listings.extend(kept)

    return _dedupe(listings), results


def _company_watched(company: str, watched: set[str]) -> bool:
    c = company.lower().strip()
    if c in watched:
        return True
    return any(c.startswith(w) or w.startswith(c) for w in watched if len(c) > 3)


def _dedupe(listings: list[Listing]) -> list[Listing]:
    """Collapse the same job arriving from two sources.

    A job can appear on a company's own board AND in an aggregator feed. Those
    carry different ids, so dedupe on (company, title, url) as well -- otherwise
    the same role notifies twice, which is the behaviour this project is built
    to prevent.
    """
    by_id: dict[str, Listing] = {}
    by_content: dict[tuple[str, str, str], str] = {}
    for listing in listings:
        if listing.id in by_id:
            continue
        key = (
            listing.company.lower().strip(),
            listing.title.lower().strip(),
            listing.url.split("?")[0].rstrip("/").lower(),
        )
        if key in by_content:
            continue
        by_content[key] = listing.id
        by_id[listing.id] = listing
    return list(by_id.values())
