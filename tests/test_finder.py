"""Tests for `joboS.finder`. No network: `verify_token` is patched out, and
every input (feed rows, watchlist boards, profile) is synthetic and inline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

from joboS import finder
from joboS.fetch import Board
from joboS.resolve import Candidate, Verification

# A fixed "now" so recency/roles_90d/repeat_posting are deterministic instead
# of depending on when the test happens to run. 2026-08-17T00:00:00Z.
FIXED_NOW = 1786924800

# Sponsorship off so relevance.classify never needs citizenship text on our
# synthetic rows, and locations are irrelevant to `.relevant` -- keeps the
# fixture profile minimal instead of duplicating config/profile.yaml.
TEST_PROFILE: dict[str, Any] = {
    "locations": {"preferred": ["Remote US", "Bay Area"]},
    "sponsorship": {"needs_sponsorship": False},
    "notifications": {"instant_tiers": ["core", "adjacent"], "digest_tiers": ["stretch"]},
}


def _ts(days_ago: int) -> int:
    return FIXED_NOW - days_ago * 86_400


def _row(
    company: str,
    *,
    url: str,
    date_posted: int,
    title: str = "Software Engineer, New Grad 2026",
    active: bool = True,
    is_visible: bool = True,
) -> dict[str, Any]:
    return {
        "company_name": company,
        "title": title,
        "url": url,
        "date_posted": date_posted,
        "active": active,
        "is_visible": is_visible,
        "locations": ["Remote"],
        "sponsorship": None,
    }


BOARDS = [
    Board(company="Palantir Technologies", ats="greenhouse", token="palantir", tier="1"),
    Board(company="Citadel Securities", ats="greenhouse", token="citadelsecurities", tier="1"),
    Board(company="Akuna Capital", ats="greenhouse", token="akunacapital", tier="1"),
]

ROWS: list[dict[str, Any]] = [
    # Already on the watchlist under a shorter trading name -- must be
    # dropped even though it has plenty of evidence.
    _row("Palantir", url="https://job-boards.greenhouse.io/palantir/jobs/1", date_posted=_ts(10)),
    _row("Palantir", url="https://job-boards.greenhouse.io/palantir/jobs/2", date_posted=_ts(40)),
    # Same story: "Citadel" (feed) vs "Citadel Securities" (watchlist).
    _row(
        "Citadel",
        url="https://job-boards.greenhouse.io/citadelsecurities/jobs/1",
        date_posted=_ts(10),
    ),
    _row(
        "Citadel",
        url="https://job-boards.greenhouse.io/citadelsecurities/jobs/2",
        date_posted=_ts(40),
    ),
    # New company, but only one listing carries a resolvable apply URL --
    # evidence == 1, below MIN_EVIDENCE, must be dropped.
    _row("Sparse Co", url="https://jobs.ashbyhq.com/sparseco/aaaa", date_posted=_ts(10)),
    # New company, repeat poster: two listings, two distinct calendar months,
    # same token both times.
    _row(
        "Cyclic Corp",
        url="https://job-boards.greenhouse.io/cycliccorp/jobs/1",
        date_posted=_ts(10),
    ),
    _row(
        "Cyclic Corp",
        url="https://job-boards.greenhouse.io/cycliccorp/jobs/2",
        date_posted=_ts(70),
    ),
    # New company, one-shot poster: same volume (2 roles in 90d) as Cyclic
    # Corp, but both postings land in the same calendar month.
    _row("Oneshot Inc", url="https://jobs.lever.co/oneshotinc/1", date_posted=_ts(5)),
    _row("Oneshot Inc", url="https://jobs.lever.co/oneshotinc/2", date_posted=_ts(8)),
    # New company with good evidence, but live verification will fail it --
    # must not appear in the output.
    _row(
        "Failverify Co",
        url="https://job-boards.greenhouse.io/failverify/jobs/1",
        date_posted=_ts(10),
    ),
    _row(
        "Failverify Co",
        url="https://job-boards.greenhouse.io/failverify/jobs/2",
        date_posted=_ts(20),
    ),
    # Not early-career / non-engineering -- dropped before it ever becomes a
    # company group.
    _row(
        "Marketing Corp",
        title="Senior Marketing Manager",
        url="https://job-boards.greenhouse.io/marketingcorp/jobs/1",
        date_posted=_ts(10),
    ),
    # Inactive listing -- dropped by the active/is_visible gate.
    _row(
        "Inactive Co",
        url="https://job-boards.greenhouse.io/inactiveco/jobs/1",
        date_posted=_ts(10),
        active=False,
    ),
]


def _fake_verify_token(cand: Candidate, *, strict_name: bool = True) -> Verification:
    if cand.company == "Failverify Co":
        return Verification(False, count=0, error="synthetic failure")
    return Verification(True, count=17, reported_name=None)


def test_pipeline_drops_watchlist_evidence_failures_and_ranks_repeat_over_oneshot() -> None:
    with mock.patch("joboS.finder.verify_token", side_effect=_fake_verify_token) as mocked:
        scored = finder.find_candidates(
            ROWS, BOARDS, days=90, profile=TEST_PROFILE, now=FIXED_NOW
        )

    names = [c.company for c in scored]
    # Watchlist companies dropped despite good evidence.
    assert "Palantir" not in names
    assert "Citadel" not in names
    # Insufficient evidence dropped.
    assert "Sparse Co" not in names
    # Failed live verification dropped.
    assert "Failverify Co" not in names
    # Never grouped at all (irrelevant title / inactive listing).
    assert "Marketing Corp" not in names
    assert "Inactive Co" not in names

    # verify_token must only be called for companies that cleared both the
    # watchlist filter and the evidence bar.
    verified_companies = {call.args[0].company for call in mocked.call_args_list}
    assert verified_companies == {"Cyclic Corp", "Oneshot Inc", "Failverify Co"}

    assert names == ["Cyclic Corp", "Oneshot Inc"]

    cyclic = next(c for c in scored if c.company == "Cyclic Corp")
    oneshot = next(c for c in scored if c.company == "Oneshot Inc")

    assert cyclic.roles_90d == oneshot.roles_90d == 2
    assert cyclic.repeat_posting is True
    assert oneshot.repeat_posting is False
    # The core requirement: a repeat poster outranks a one-shot poster with
    # identical volume.
    assert cyclic.score > oneshot.score

    assert cyclic.ats == "greenhouse"
    assert cyclic.token == "cycliccorp"
    assert oneshot.ats == "lever"
    assert oneshot.token == "oneshotinc"
    assert cyclic.verified_job_count == 17


def test_removal_candidates_are_advisory_and_use_a_180_day_window() -> None:
    boards = [
        Board(company="Akuna Capital", ats="greenhouse", token="akunacapital", tier="1"),
        Board(company="Ghost Co", ats="greenhouse", token="ghost", tier="2"),
        Board(company="Stale Co", ats="greenhouse", token="stale", tier="2"),
    ]
    rows = [
        # Posted recently -- must NOT be a removal candidate.
        _row(
            "Akuna Capital",
            url="https://job-boards.greenhouse.io/akunacapital/jobs/1",
            date_posted=_ts(30),
        ),
        # Posted, but 200 days ago -- outside the 180-day removal window, so
        # it still counts as "no recent postings".
        _row(
            "Stale Co",
            url="https://job-boards.greenhouse.io/stale/jobs/1",
            date_posted=_ts(200),
        ),
        # Ghost Co has no rows in the feed at all.
    ]

    removals = finder.removal_candidates(rows, boards, profile=TEST_PROFILE, now=FIXED_NOW)
    removed_names = {r.company for r in removals}

    assert removed_names == {"Ghost Co", "Stale Co"}
    assert "Akuna Capital" not in removed_names


def test_write_json_writes_all_candidates_to_given_path(tmp_path: Path) -> None:
    scored = [
        finder.ScoredCandidate(
            company="Cyclic Corp",
            ats="greenhouse",
            token="cycliccorp",
            site=None,
            host=None,
            roles_90d=2,
            recency_days=10,
            repeat_posting=True,
            score=101.9,
            sample_title="Software Engineer, New Grad 2026",
            verified_job_count=17,
        ),
    ]
    removals = [
        finder.RemovalCandidate(company="Ghost Co", ats="greenhouse", token="ghost", tier="2"),
    ]

    # Snapshot the real output path first: can't assert it's absent, since a
    # real (non-test) run of the finder may have legitimately populated it
    # already. The property under test is narrower -- write_json must never
    # touch it when given a different path.
    real_path = finder.DEFAULT_OUT_PATH
    before = real_path.read_bytes() if real_path.exists() else None

    out_path = tmp_path / "finder_candidates.json"
    finder.write_json(out_path, scored, removals, days=90, now=FIXED_NOW)

    assert out_path.exists()
    after = real_path.read_bytes() if real_path.exists() else None
    assert before == after

    payload = json.loads(out_path.read_text())
    assert payload["window_days"] == 90
    assert len(payload["candidates"]) == 1
    cand = payload["candidates"][0]
    for key in (
        "company", "ats", "token", "site", "host", "roles_90d", "recency_days",
        "repeat_posting", "score", "sample_title", "verified_job_count",
    ):
        assert key in cand
    assert cand["company"] == "Cyclic Corp"
    assert cand["repeat_posting"] is True

    assert len(payload["removal_candidates"]) == 1
    assert payload["removal_candidates"][0]["company"] == "Ghost Co"
    assert "advisory" in payload["removal_candidates_caveat"].lower()
    assert "smoke" in payload["removal_candidates_caveat"]


def test_load_unresolved_names_parses_the_comment_block(tmp_path: Path) -> None:
    fixture = tmp_path / "companies.yaml"
    fixture.write_text(
        "companies:\n"
        '  - company: "Foo"\n'
        '    tier: "1"\n'
        "    ats: greenhouse\n"
        '    token: "foo"\n'
        "\n"
        "# ---------------------------------------------------------------\n"
        "# UNRESOLVED -- no public job-board API we can reach.\n"
        "#\n"
        "# Proprietary career sites, listed so the gap is documented.\n"
        "#   - Amazon (tier 1)\n"
        "#   - Google (tier 1)\n"
        "#   - Citadel (tier 1)\n"
        "#   - Garmin (tier 2)\n"
    )
    names = finder._load_unresolved_names(fixture)
    assert names == ["Amazon", "Google", "Citadel", "Garmin"]


def test_load_unresolved_names_missing_file_returns_empty_list(tmp_path: Path) -> None:
    assert finder._load_unresolved_names(tmp_path / "nope.yaml") == []


def test_run_dry_run_writes_json_and_never_touches_git(tmp_path: Path) -> None:
    out_path = tmp_path / "out.json"
    companies_path = tmp_path / "companies.yaml"  # deliberately does not exist

    with mock.patch("joboS.finder.verify_token", side_effect=_fake_verify_token), \
         mock.patch("joboS.finder.subprocess.run") as mocked_run:
        code = finder.run(
            days=90,
            top=10,
            open_pr=True,  # --dry-run must win over --open-pr
            dry_run=True,
            rows=ROWS,
            boards=BOARDS,
            profile=TEST_PROFILE,
            out_path=out_path,
            companies_path=companies_path,
            now=FIXED_NOW,
        )

    assert code == 0
    assert out_path.exists()
    mocked_run.assert_not_called()


def test_open_pr_prints_and_exits_zero_when_gh_unavailable(tmp_path: Path) -> None:
    out_path = tmp_path / "out.json"
    companies_path = tmp_path / "companies.yaml"

    with mock.patch("joboS.finder.verify_token", side_effect=_fake_verify_token), \
         mock.patch("joboS.finder.shutil.which", return_value=None), \
         mock.patch("joboS.finder.subprocess.run") as mocked_run:
        code = finder.run(
            days=90,
            top=10,
            open_pr=True,
            dry_run=False,
            rows=ROWS,
            boards=BOARDS,
            profile=TEST_PROFILE,
            out_path=out_path,
            companies_path=companies_path,
            now=FIXED_NOW,
        )

    assert code == 0
    assert out_path.exists()
    # `shutil.which` returning None must short-circuit before any git/gh
    # subprocess is ever invoked.
    mocked_run.assert_not_called()
