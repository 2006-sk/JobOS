"""Tests for title-based relevance classification.

Fixture-driven: tests/fixtures/titles.yaml holds real titles from the
aggregator corpus, each pinned against the actual classifier output (see the
NOTE at the top of that file for the one real gap it surfaced). Everything
else here exercises classify() end to end: the citizenship-surfaced-not-
dropped rule, location ranking, and the instant/digest channel split.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

from joboS.relevance import classify, classify_field, classify_level, location_rank

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "titles.yaml"


def _cases() -> list[dict[str, Any]]:
    data = yaml.safe_load(FIXTURE.read_text())
    return data["cases"]


CASES = _cases()


@pytest.mark.parametrize("case", CASES, ids=[c["title"] for c in CASES])
def test_classify_level_matches_fixture(case: dict[str, Any]) -> None:
    assert classify_level(case["title"]) == case["level"]


@pytest.mark.parametrize("case", CASES, ids=[c["title"] for c in CASES])
def test_classify_field_matches_fixture(case: dict[str, Any]) -> None:
    assert classify_field(case["title"]) == case["field"]


def test_citizenship_required_is_surfaced_not_silently_dropped() -> None:
    # Default profile (config/profile.yaml) has needs_sponsorship: true and
    # drop_citizenship_required: true -- but "drop" means "pull out of the
    # instant channel", not "discard". It must still come back as relevant
    # information routed to the digest, with the reason recorded, so a
    # citizen-only role is never simply invisible.
    verdict = classify(
        "Software Engineer New Grad - US Citizenship Required",
        locations=["San Jose, CA"],
    )
    assert verdict.channel == "digest"
    assert verdict.citizenship_required is True
    assert verdict.excluded_reason is not None


def test_location_rank_prefers_san_jose_over_dublin() -> None:
    san_jose = classify("New Grad Software Engineer, Backend", locations=["San Jose, CA"])
    dublin = classify("New Grad Software Engineer, Backend", locations=["Dublin, Ireland"])
    assert san_jose.location_rank < dublin.location_rank


def test_location_rank_function_directly() -> None:
    preferred = ["Bay Area", "San Jose", "San Francisco", "Seattle", "New York", "Remote US"]
    assert location_rank(["San Jose, CA"], preferred) < location_rank(
        ["Dublin, Ireland"], preferred
    )


def test_stretch_tier_goes_to_digest_core_tier_goes_instant() -> None:
    stretch = classify("Hardware Engineering Intern", locations=["Austin, TX"])
    core = classify("New Grad Software Engineer, Backend", locations=["San Jose, CA"])
    assert stretch.channel == "digest"
    assert core.channel == "instant"
