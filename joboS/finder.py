"""Weekly discovery: find new companies worth adding to the watchlist.

Pipeline: pull the aggregator feeds -> keep active+visible, early-career,
relevant-field rows (`relevance.classify`) -> drop anything already on the
watchlist (`resolve.name_matches`) -> mine an ATS token from the survivors'
apply URLs (`resolve.mine_candidates`) -> live-verify the top token
(`resolve.verify_token`) -> score and rank what is left. Every non-trivial
piece of company-name matching and token extraction is reused from
`resolve.py`, which already paid for the false-positive lessons (IMC ->
"ing", Applied Intuition -> Applied Materials, etc); this module does not
re-derive any of that.

SCORING (see `_score`): a company's score is

    score = REPEAT_BONUS (100, only if repeat_posting)
          + min(roles_90d, VOLUME_CAP=20)
          - min(recency_days, 90) * 0.01

`REPEAT_BONUS` is chosen to exceed `VOLUME_CAP` by 5x, so the volume term
can NEVER close the gap between a repeat poster and a one-shot poster no
matter how large a one-time burst of postings a one-shot company had. That
makes "repeat posters outrank one-shot posters" a structural property of the
formula rather than something that happens to hold for the numbers we
tested with. `repeat_posting` matters this much because it is the signal
that separates a company hiring new-grads on a recurring cycle (worth
watching indefinitely) from one that posted a single role once (worth
nothing once that role closes) -- volume alone cannot tell them apart: a
company that dumps 10 reqs in one week and never posts again outscores a
company posting one role a quarter on raw count, which is exactly backwards
for a watchlist meant to catch next season's postings too. The recency term
is a pure tie-breaker (max swing 0.9) between candidates that already tied
on (bonus, volume); it never crosses a whole point of `volume`.

`repeat_posting` itself is computed over ALL relevant postings a company has
in this run's feed pull, not windowed to `--days`/90 days: a narrow window
would understate a company that hires every fall by missing last fall's
postings, and would falsely flag a company that happened to post twice in
one week straddling a calendar-month boundary. The feed's own retention
(the aggregators only carry active-ish postings) is the only bound that
applies.

REMOVAL CANDIDATES ARE ADVISORY ONLY. A watchlist company can have zero
postings in these three community feeds while actively hiring on its own
board -- the feeds cover ~3,450 companies, not every company that exists, so
"not in the feed" proves nothing about the company's own site (which
`fetch.py` polls directly every run, independent of this module). Treat a
removal candidate as "worth a `python -m joboS.smoke --company X` check",
never as "safe to delete".
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .adapters import aggregators
from .fetch import COMPANIES_PATH, MAX_WORKERS, Board, load_boards
from .models import now_ts
from .relevance import classify, load_profile
from .resolve import (
    Candidate,
    Verification,
    mine_candidates,
    name_matches,
    normalize_company,
    verify_token,
)

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_OUT_PATH = DATA_DIR / "finder_candidates.json"

# The bar `resolve.verify_token` itself uses to trust provenance over string
# similarity: one listing's apply URL could be a typo or an unrelated link,
# but two independent postings converging on the same token is real signal.
MIN_EVIDENCE = 2

REMOVAL_WINDOW_DAYS = 180

REMOVAL_CAVEAT = (
    "Advisory only: a watchlist company can have zero postings in these "
    "aggregator feeds while still actively hiring on its own board -- the "
    "feeds cover ~3,450 companies, not every company that exists. Verify "
    "with `python -m joboS.smoke --company <name>` before removing anything."
)

_REPEAT_BONUS = 100.0
_VOLUME_CAP = 20
_RECENCY_PENALTY_PER_DAY = 0.01  # tie-break only -- see module docstring


# --- data shapes -------------------------------------------------------


@dataclass
class ScoredCandidate:
    """One company the finder recommends adding to the watchlist."""

    company: str
    ats: str
    token: str
    site: str | None
    host: str | None
    roles_90d: int
    recency_days: int
    repeat_posting: bool
    score: float
    sample_title: str
    verified_job_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RemovalCandidate:
    """A watchlist company with no recent relevant postings in the feeds."""

    company: str
    ats: str
    token: str
    tier: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Metrics:
    roles_90d: int
    recency_days: int
    repeat_posting: bool
    score: float
    sample_title: str


# --- shared row helpers --------------------------------------------------


def _posted_ts(row: dict[str, Any]) -> int | None:
    """Mirrors `backtest.py`'s guard: only trust a numeric `date_posted`."""
    posted = row.get("date_posted")
    return int(posted) if isinstance(posted, (int, float)) else None


def _year_month(ts: int) -> tuple[int, int]:
    d = dt.datetime.fromtimestamp(ts, dt.UTC)
    return (d.year, d.month)


def _relevant_rows(
    rows: list[dict[str, Any]], profile: dict[str, Any]
) -> list[dict[str, Any]]:
    """Active+visible rows whose title passes `relevance.classify`.

    `fetch_raw()` hands back the feeds' raw JSON, unfiltered -- unlike
    `aggregators.parse()`, which callers normally go through, so the
    active/is_visible check that lives there has to be repeated here.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        if not (row.get("active") and row.get("is_visible")):
            continue
        title = (row.get("title") or "").strip()
        company = (row.get("company_name") or "").strip()
        if not title or not company:
            continue
        verdict = classify(
            title,
            locations=row.get("locations") or [],
            sponsorship=row.get("sponsorship"),
            profile=profile,
        )
        if verdict.relevant:
            out.append(row)
    return out


def _group_by_company(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        company = (row.get("company_name") or "").strip()
        if company:
            groups.setdefault(company, []).append(row)
    return groups


def _is_known(company: str, known_norms: set[str], known_names: list[str]) -> bool:
    """Whether `company` is already on the watchlist (or documented as
    unresolvable -- see `_load_unresolved_names`).

    `normalize_company` collapses the common variants ("Palantir" vs
    "Palantir Technologies", "Citadel" vs "Citadel Securities") to the same
    key, so a plain set lookup catches most matches in O(1) rather than
    running `name_matches`'s SequenceMatcher against every watchlist name.
    `name_matches` is the fallback for cases a suffix-strip alone misses
    (fuzzy prefix / ratio matches).
    """
    if normalize_company(company) in known_norms:
        return True
    return any(name_matches(company, k) for k in known_names)


# --- discovery -------------------------------------------------------------


def _score(roles_90d: int, repeat_posting: bool, recency_days: int) -> float:
    volume = min(roles_90d, _VOLUME_CAP)
    bonus = _REPEAT_BONUS if repeat_posting else 0.0
    recency_penalty = min(recency_days, 90) * _RECENCY_PENALTY_PER_DAY
    return bonus + volume - recency_penalty


def _score_company(rows: list[dict[str, Any]], *, days: int, now: int) -> _Metrics | None:
    dated: list[tuple[int, str]] = []
    for row in rows:
        ts = _posted_ts(row)
        if ts is None or ts > now:
            # A "posted" timestamp in the future isn't plausible -- more
            # likely a unit mismatch slipping past `_posted_ts`. Treating it
            # as missing is safer than letting it corrupt recency/roles_90d.
            continue
        dated.append((ts, (row.get("title") or "").strip()))
    if not dated:
        return None
    dated.sort(key=lambda pair: pair[0])

    cutoff = now - days * 86_400
    roles_90d = sum(1 for ts, _ in dated if ts >= cutoff)
    recency_days = (now - dated[-1][0]) // 86_400

    # See the module docstring: deliberately NOT windowed to `days`.
    months = {_year_month(ts) for ts, _ in dated}
    repeat_posting = len(months) >= 2

    score = _score(roles_90d, repeat_posting, recency_days)
    return _Metrics(roles_90d, recency_days, repeat_posting, score, dated[-1][1])


def _verify_concurrently(
    candidates: dict[str, Candidate], max_workers: int
) -> dict[str, Verification]:
    """Live-verify every top candidate token in parallel.

    Bounded at `max_workers` (default matches `fetch.py`'s cap) for the same
    reason `fetch.py` bounds its own concurrency: we are an unauthenticated
    guest on these boards' infrastructure.
    """
    if not candidates:
        return {}
    out: dict[str, Verification] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending = {
            pool.submit(verify_token, cand, strict_name=True): company
            for company, cand in candidates.items()
        }
        for fut in as_completed(pending):
            out[pending[fut]] = fut.result()
    return out


def find_candidates(
    rows: list[dict[str, Any]],
    boards: list[Board],
    *,
    days: int,
    profile: dict[str, Any] | None = None,
    unresolved_names: Iterable[str] = (),
    now: int | None = None,
    max_workers: int = MAX_WORKERS,
) -> list[ScoredCandidate]:
    """Run the full discovery pipeline and return candidates sorted by score."""
    prof = profile if profile is not None else load_profile()
    ts_now = now if now is not None else now_ts()

    relevant = _relevant_rows(rows, prof)
    groups = _group_by_company(relevant)

    known_names = [b.company for b in boards] + list(unresolved_names)
    known_norms = {normalize_company(n) for n in known_names}
    new_groups = {
        company: company_rows
        for company, company_rows in groups.items()
        if not _is_known(company, known_norms, known_names)
    }

    # mine_candidates only needs the (company, url) pairs; feeding it exactly
    # the rows that survived the relevance + watchlist filters keeps a
    # borderline company's evidence count meaning "N relevant listings agree
    # on this token", not "N listings of any kind, relevant or not".
    flattened = [row for company_rows in new_groups.values() for row in company_rows]
    mined = mine_candidates(flattened)
    top_candidates = {
        company: cands[0]
        for company, cands in mined.items()
        if cands and cands[0].evidence >= MIN_EVIDENCE
    }

    verifications = _verify_concurrently(top_candidates, max_workers)

    scored: list[ScoredCandidate] = []
    for company, cand in top_candidates.items():
        verification = verifications.get(company)
        if verification is None or not verification.ok:
            continue
        metrics = _score_company(new_groups[company], days=days, now=ts_now)
        if metrics is None:
            continue
        scored.append(
            ScoredCandidate(
                company=company,
                ats=cand.ats,
                token=cand.token,
                site=cand.site,
                host=cand.host,
                roles_90d=metrics.roles_90d,
                recency_days=metrics.recency_days,
                repeat_posting=metrics.repeat_posting,
                score=metrics.score,
                sample_title=metrics.sample_title,
                verified_job_count=verification.count,
            )
        )

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


# --- removal candidates ------------------------------------------------


def removal_candidates(
    rows: list[dict[str, Any]],
    boards: list[Board],
    *,
    profile: dict[str, Any] | None = None,
    now: int | None = None,
    window_days: int = REMOVAL_WINDOW_DAYS,
) -> list[RemovalCandidate]:
    """Watchlist companies with zero relevant feed postings in `window_days`.

    See `REMOVAL_CAVEAT` -- this is advisory. It also only considers boards
    actually present in `config/companies.yaml` (via `load_boards`), not the
    UNRESOLVED comment block, because there is nothing there to "remove".
    """
    prof = profile if profile is not None else load_profile()
    ts_now = now if now is not None else now_ts()
    cutoff = ts_now - window_days * 86_400

    relevant = _relevant_rows(rows, prof)
    recent_names: set[str] = set()
    for row in relevant:
        ts = _posted_ts(row)
        if ts is not None and cutoff <= ts <= ts_now:
            recent_names.add((row.get("company_name") or "").strip())

    out: list[RemovalCandidate] = []
    for board in boards:
        if not any(name_matches(board.company, name) for name in recent_names):
            out.append(
                RemovalCandidate(board.company, board.ats, board.token, board.tier)
            )
    return out


# --- the UNRESOLVED suppression list ------------------------------------

_UNRESOLVED_RX = re.compile(r"^#\s+-\s+(.+?)\s+\(tier\s+\S+\)\s*$", re.MULTILINE)


def _load_unresolved_names(path: Path) -> list[str]:
    """Company names in `companies.yaml`'s commented-out UNRESOLVED block.

    Those are real watchlist intent (Apple, Google, Citadel, Meta, Two
    Sigma...) that YAML parses right past because they're comments, not
    entries. Without this, the finder would "discover" the same well-known,
    deliberately-unresolved companies every single week. Best-effort: if the
    block's format ever drifts this just returns fewer names rather than
    raising -- a missed suppression costs one extra row for a human to
    reject in the PR, which is far cheaper than a crash blocking the run.
    """
    try:
        text = path.read_text()
    except OSError:
        return []
    return [m.group(1).strip() for m in _UNRESOLVED_RX.finditer(text)]


# --- output ---------------------------------------------------------------


def write_json(
    path: Path,
    scored: list[ScoredCandidate],
    removals: list[RemovalCandidate],
    *,
    days: int,
    now: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": dt.datetime.fromtimestamp(now, dt.UTC).isoformat(),
        "window_days": days,
        "candidates": [c.to_dict() for c in scored],
        "removal_candidates": [r.to_dict() for r in removals],
        "removal_candidates_caveat": REMOVAL_CAVEAT,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _print_candidates_table(scored: list[ScoredCandidate], top: int) -> None:
    shown = scored[:top]
    print(f"\n{len(scored)} new-company candidate(s) found (showing top {len(shown)}):\n")
    head = (
        f"{'COMPANY':<24} {'ROLES/90D':>9} {'REPEAT':>7} {'ATS':<16} "
        f"{'SCORE':>7}  SAMPLE TITLE"
    )
    print(head)
    print("-" * len(head))
    for c in shown:
        repeat = "yes" if c.repeat_posting else "no"
        print(
            f"{c.company:<24} {c.roles_90d:>9} {repeat:>7} {c.ats:<16} "
            f"{c.score:>7.1f}  {c.sample_title[:50]}"
        )


def _print_removal_table(removals: list[RemovalCandidate]) -> None:
    if not removals:
        return
    print(f"\n{len(removals)} removal candidate(s) (ADVISORY ONLY -- see caveat):\n")
    for r in removals:
        print(f"  {r.company:<24} {r.ats:<16} {r.token}")
    print(f"\n{REMOVAL_CAVEAT}")


# --- git / PR ---------------------------------------------------------


def _render_company_entry(c: ScoredCandidate) -> str:
    lines = [
        f'  - company: "{c.company}"',
        '    tier: "unranked"',
        f"    ats: {c.ats}",
        f'    token: "{c.token}"',
    ]
    if c.site:
        lines.append(f'    site: "{c.site}"')
    if c.host:
        lines.append(f'    host: "{c.host}"')
    return "\n".join(lines) + "\n"


_UNRESOLVED_MARKER = "# UNRESOLVED -- no public job-board API we can reach."


def _append_companies_yaml(path: Path, scored_top: list[ScoredCandidate]) -> None:
    """Append entries as raw text, NOT via yaml.dump.

    `companies.yaml` is hand-annotated with comments (see its own header and
    the UNRESOLVED block); round-tripping it through PyYAML would silently
    discard every one of them.
    """
    text = path.read_text()
    block = "\n" + "\n".join(_render_company_entry(c) for c in scored_top)
    marker_idx = text.find(_UNRESOLVED_MARKER)
    if marker_idx == -1:
        new_text = text.rstrip("\n") + "\n" + block
    else:
        # Land new entries among the real, parsed companies rather than
        # visually underneath "UNRESOLVED" -- that section is specifically
        # about companies with no token, and these very much have one.
        head = text[:marker_idx].rstrip("\n") + "\n"
        tail = text[marker_idx:]
        new_text = head + block + "\n" + tail
    path.write_text(new_text)


def _pr_body(scored_top: list[ScoredCandidate], removals: list[RemovalCandidate]) -> str:
    lines = [
        "Auto-generated by `python -m joboS.finder --open-pr`. Human review "
        "required before merge -- this workflow never auto-merges.",
        "",
        "## New candidates",
        "",
        "| Company | Roles/90d | Repeat? | ATS | Token | Sample title |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for c in scored_top:
        repeat = "yes" if c.repeat_posting else "no"
        title = c.sample_title.replace("|", "/")
        lines.append(
            f"| {c.company} | {c.roles_90d} | {repeat} | {c.ats} | `{c.token}` | {title} |"
        )
    lines += ["", "## Removal candidates", "", f"_{REMOVAL_CAVEAT}_", ""]
    if removals:
        lines += ["| Company | ATS | Token |", "| --- | --- | --- |"]
        for r in removals:
            lines.append(f"| {r.company} | {r.ats} | `{r.token}` |")
    else:
        lines.append("None this run.")
    return "\n".join(lines) + "\n"


def _gh_available() -> bool:
    if shutil.which("gh") is None:
        return False
    try:
        subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, OSError):
        return False
    return True


def _run_git(args: list[str], *, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _open_pr(
    scored: list[ScoredCandidate],
    removals: list[RemovalCandidate],
    *,
    top: int,
    branch_date: dt.date,
    companies_path: Path,
    repo_root: Path,
) -> int:
    if not scored:
        print("no new candidates to add -- skipping PR.")
        return 0
    if not _gh_available():
        print(
            "gh CLI is missing or not authenticated -- printing the candidate "
            "table above instead of opening a PR. Install/auth `gh` to enable "
            "--open-pr."
        )
        return 0

    scored_top = scored[:top]
    branch = f"finder/candidates-{branch_date.strftime('%Y%m%d')}"

    try:
        _run_git(["checkout", "-b", branch], cwd=repo_root)
        _append_companies_yaml(companies_path, scored_top)
        _run_git(["add", str(companies_path)], cwd=repo_root)
        _run_git(
            ["commit", "-m", f"finder: {len(scored_top)} new watchlist candidate(s)"],
            cwd=repo_root,
        )
        _run_git(["push", "-u", "origin", branch], cwd=repo_root)

        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as body_file:
            body_file.write(_pr_body(scored_top, removals))
            body_path = Path(body_file.name)
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--title",
                    f"finder: {len(scored_top)} new watchlist candidate(s)",
                    "--body-file",
                    str(body_path),
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
        finally:
            body_path.unlink(missing_ok=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        # The JSON was already written before this function ran (see `run`),
        # so a git/gh failure here loses only this run's PR -- rerunning
        # `--open-pr`, or opening it by hand from the pushed branch, recovers.
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"failed to open PR: {detail}")
        return 1

    print(result.stdout.strip())
    return 0


# --- CLI ---------------------------------------------------------------


def run(
    *,
    days: int = 90,
    top: int = 10,
    open_pr: bool = False,
    dry_run: bool = False,
    rows: list[dict[str, Any]] | None = None,
    boards: list[Board] | None = None,
    profile: dict[str, Any] | None = None,
    out_path: Path | None = None,
    companies_path: Path | None = None,
    now: int | None = None,
    branch_date: dt.date | None = None,
    max_workers: int = MAX_WORKERS,
) -> int:
    prof = profile if profile is not None else load_profile()
    ts_now = now if now is not None else now_ts()
    brds = boards if boards is not None else load_boards()
    row_data = rows if rows is not None else aggregators.fetch_raw()
    companies_p = companies_path if companies_path is not None else COMPANIES_PATH
    out_p = out_path if out_path is not None else DEFAULT_OUT_PATH
    repo_root = Path(__file__).resolve().parent.parent

    unresolved = _load_unresolved_names(companies_p)
    scored = find_candidates(
        row_data,
        brds,
        days=days,
        profile=prof,
        unresolved_names=unresolved,
        now=ts_now,
        max_workers=max_workers,
    )
    removals = removal_candidates(row_data, brds, profile=prof, now=ts_now)

    # Write the JSON before doing anything with git/gh: a failure below must
    # never cost this run's computed results.
    write_json(out_p, scored, removals, days=days, now=ts_now)

    _print_candidates_table(scored, top)
    _print_removal_table(removals)

    if dry_run:
        print("\n--dry-run: not touching git or GitHub.")
        return 0
    if not open_pr:
        return 0

    bdate = branch_date if branch_date is not None else dt.datetime.now(dt.UTC).date()
    return _open_pr(
        scored, removals, top=top, branch_date=bdate,
        companies_path=companies_p, repo_root=repo_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m joboS.finder", description=__doc__)
    parser.add_argument("--days", type=int, default=90,
                        help="roles_90d / recency window in days")
    parser.add_argument("--top", type=int, default=10,
                        help="how many candidates go into the PR / stdout table")
    parser.add_argument("--open-pr", action="store_true",
                        help="commit the top candidates and open a PR via gh")
    parser.add_argument("--dry-run", action="store_true",
                        help="write the JSON and print the table, never touch git")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return run(days=args.days, top=args.top, open_pr=args.open_pr, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
