"""Replay aggregator history through relevance.py to sanity-check ping volume.

This answers the question that decides whether the whole thing is usable: how
many notifications would a normal day produce? If the answer is fifty, the
regexes are too loose and the monitor becomes noise you learn to ignore.

Note the `--watchlist-only` default. The feeds cover ~3,450 companies; passing
all of them through measured ~22 relevant roles/day, while restricting to the
watchlist measured ~4. The watchlist number is the real one, because that is
what the poll actually notifies on -- the aggregators exist to cover watchlist
companies that have no reachable board, not to surface the entire job market.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import logging
import sys
from collections.abc import Sequence
from typing import Any

from .adapters import aggregators
from .fetch import _company_watched, load_boards
from .models import now_ts
from .relevance import classify, load_profile


def _day(ts: int | None) -> dt.date | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(ts, dt.UTC).date()


def run(days: int, *, watchlist_only: bool = True, show: int = 25) -> int:
    profile = load_profile()
    rows: list[dict[str, Any]] = aggregators.fetch_raw()
    print(f"pulled {len(rows)} listings from {len(aggregators.FEEDS)} feeds")

    watched = {b.company.lower() for b in load_boards()} if watchlist_only else set()
    cutoff = now_ts() - days * 86_400

    per_day: collections.Counter[dt.date] = collections.Counter()
    kept: list[tuple[dt.date | None, str, str, str]] = []
    n_active = n_scope = 0

    for row in rows:
        if not (row.get("active") and row.get("is_visible")):
            continue
        n_active += 1
        company = (row.get("company_name") or "").strip()
        if watchlist_only and not _company_watched(company, watched):
            continue
        n_scope += 1

        posted = row.get("date_posted")
        posted_ts = int(posted) if isinstance(posted, (int, float)) else None
        if posted_ts is None or posted_ts < cutoff:
            continue

        verdict = classify(row.get("title") or "", locations=row.get("locations") or [],
                           sponsorship=row.get("sponsorship"), profile=profile)
        if verdict.channel != "instant":
            continue
        day = _day(posted_ts)
        if day:
            per_day[day] += 1
        kept.append((day, company, row.get("title") or "", verdict.level))

    scope = "watchlist companies" if watchlist_only else "ALL companies in the feeds"
    print(f"active+visible: {n_active}; in scope ({scope}): {n_scope}")
    print(f"would have pinged: {len(kept)} roles over the last {days} days\n")

    if per_day:
        counts = sorted(per_day.values())
        median = counts[len(counts) // 2]
        mean = sum(counts) / len(counts)
        print(f"{'DATE':<12} {'PINGS':>5}")
        for day in sorted(per_day):
            n = per_day[day]
            print(f"{day.isoformat():<12} {n:>5}  {'#' * min(n, 50)}")
        print(f"\nmedian {median}/day, mean {mean:.1f}/day, peak {max(counts)}/day")
        if median > 9:
            print("\nWARNING: median exceeds single digits -- tighten the regexes "
                  "in relevance.py and re-run before trusting this.")
    else:
        print("no dated matches in the window")

    if kept:
        print(f"\n--- sample of what would have pinged (first {show}) ---")
        for day, company, title, level in sorted(kept, key=lambda k: (k[0] or dt.date.min))[-show:]:
            stamp = day.isoformat() if day else "??????????"
            print(f"  {stamp}  [{level:<10}] {company[:20]:<20} {title[:60]}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m joboS.backtest", description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--all-companies", action="store_true",
                        help="do not restrict to the watchlist (shows the raw feed volume)")
    parser.add_argument("--show", type=int, default=25, help="sample size to print")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    return run(args.days, watchlist_only=not args.all_companies, show=args.show)


if __name__ == "__main__":
    sys.exit(main())
