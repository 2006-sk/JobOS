"""Live smoke test: hit every board in companies.yaml and report.

Network-bound and deliberately NOT part of `make test` or CI -- it makes ~32 real
API calls against other people's infrastructure, which is fine to run by hand and
rude to run on every push.

Exits non-zero if any board fails, so `make smoke` is a usable gate before you
trust a change to the watchlist.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import logging
import sys
from collections.abc import Sequence

import requests

from .fetch import Board, fetch_board, load_boards
from .models import BoardResult
from .relevance import classify, load_profile


def _relevant_count(result: BoardResult, profile: dict[str, object]) -> int:
    return sum(
        1
        for listing in result.listings
        if classify(listing.title, locations=listing.locations,
                    sponsorship=listing.sponsorship, profile=profile).relevant
    )


def run(boards: Sequence[Board], *, max_pages: int | None = None) -> int:
    profile = load_profile()
    session = requests.Session()
    rows: list[tuple[Board, BoardResult, int]] = []

    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        pending = {
            pool.submit(fetch_board, b, session, max_pages=max_pages): b for b in boards
        }
        for fut in futures.as_completed(pending):
            board = pending[fut]
            result = fut.result()
            rows.append((board, result, _relevant_count(result, profile)))

    rows.sort(key=lambda r: (r[0].tier, r[0].company.lower()))

    head = f"{'COMPANY':<22} {'T':<2} {'ATS':<16} {'STATUS':<8} {'JOBS':>6} {'RELEVANT':>9}"
    print(head)
    print("-" * len(head))
    failures = 0
    total_jobs = total_rel = 0
    for board, result, rel in rows:
        if result.ok:
            status = "OK"
        else:
            status = str(result.status or "ERR")
            failures += 1
        total_jobs += result.count
        total_rel += rel
        print(f"{board.company:<22} {board.tier:<2} {board.ats:<16} "
              f"{status:<8} {result.count:>6} {rel:>9}")
        if not result.ok:
            print(f"{'':<22} └─ {result.error}")

    print("-" * len(head))
    print(f"{'TOTAL':<22} {'':<2} {len(rows):<16} "
          f"{'':<8} {total_jobs:>6} {total_rel:>9}")
    print(f"\n{len(rows) - failures}/{len(rows)} boards OK")
    if failures:
        print(f"FAILED: {failures} board(s). Fix the token or drop the company "
              f"from config/companies.yaml.")
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m joboS.smoke", description=__doc__)
    parser.add_argument("--company", help="smoke-test a single company by name")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="cap pagination (Workday/SmartRecruiters)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    boards = load_boards()
    if args.company:
        needle = args.company.lower()
        boards = [b for b in boards if needle in b.company.lower()]
        if not boards:
            print(f"no company matching {args.company!r} in config/companies.yaml")
            return 1
    return run(boards, max_pages=args.max_pages)


if __name__ == "__main__":
    sys.exit(main())
