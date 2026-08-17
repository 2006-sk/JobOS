"""Tests for the Ashby adapter, against a real recorded payload.

The fixture is a genuine response for Etched's Ashby board, so these tests
double as a check that the adapter tolerates the real shape of the API
rather than an idealized one.
"""

from __future__ import annotations

import json
import pathlib
from unittest import mock

import pytest

from joboS.adapters import ashby
from joboS.http import FetchError
from joboS.models import BoardResult

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "ashby_etched.json"
TOKEN = "Etched"  # Ashby tokens are case-sensitive; this is the real casing.
COMPANY = "Etched"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_returns_only_listed_jobs_with_ats_and_token() -> None:
    listings = ashby.parse(_payload(), TOKEN, COMPANY)
    assert len(listings) == 10  # every job in the fixture is isListed: true
    for listing in listings:
        assert listing.source_ats == "ashby"
        assert listing.board_token == TOKEN


def test_ids_are_unique_stable_and_shaped() -> None:
    first = ashby.parse(_payload(), TOKEN, COMPANY)
    second = ashby.parse(_payload(), TOKEN, COMPANY)

    ids_first = [listing.id for listing in first]
    ids_second = [listing.id for listing in second]
    assert len(ids_first) == len(set(ids_first))  # unique within one parse
    assert ids_first == ids_second  # stable across separate parse() calls

    for listing in first:
        # "ashby:<token>:<uuid>" -- the native Ashby job id, not a content
        # hash, since every fixture posting carries an "id".
        assert listing.id.startswith(f"ashby:{TOKEN}:")
        native = listing.id.removeprefix(f"ashby:{TOKEN}:")
        assert native and "h" != native[0]  # not the hashed_id fallback form


def test_employment_type_full_time_normalizes() -> None:
    listings = ashby.parse(_payload(), TOKEN, COMPANY)
    assert all(listing.employment_type == "Full-time" for listing in listings)


def test_employment_type_unknown_value_passes_through_unchanged() -> None:
    payload = {
        "apiVersion": "1",
        "jobs": [
            {
                "id": "unknown-type-job",
                "title": "Something Unusual",
                "location": "Remote",
                "secondaryLocations": [],
                "employmentType": "Fellowship",
                "publishedAt": "2025-06-11T05:03:53.978+00:00",
                "jobUrl": "https://jobs.ashbyhq.com/Etched/unknown-type-job",
                "department": "Ops",
                "isListed": True,
                "isRemote": False,
            }
        ],
    }
    listings = ashby.parse(payload, TOKEN, COMPANY)
    assert len(listings) == 1
    assert listings[0].employment_type == "Fellowship"


def test_unlisted_job_is_excluded() -> None:
    payload = {
        "apiVersion": "1",
        "jobs": [
            {
                "id": "draft-job",
                "title": "Secret Draft Role",
                "location": "San Jose",
                "secondaryLocations": [],
                "employmentType": "FullTime",
                "publishedAt": "2025-06-11T05:03:53.978+00:00",
                "jobUrl": "https://jobs.ashbyhq.com/Etched/draft-job",
                "department": "Ops",
                "isListed": False,
                "isRemote": False,
            }
        ],
    }
    assert ashby.parse(payload, TOKEN, COMPANY) == []


def test_posted_at_is_plausible_utc_epoch_seconds() -> None:
    listings = ashby.parse(_payload(), TOKEN, COMPANY)
    for listing in listings:
        assert listing.posted_at is not None
        # Regression guard for an ms-vs-seconds bug: a correct epoch-seconds
        # value for a real posting lands well within this window, whereas an
        # unconverted milliseconds value would be ~1000x too large.
        assert 1_600_000_000 < listing.posted_at < 2_000_000_000


def test_empty_board_parses_to_empty_list() -> None:
    assert ashby.parse({"jobs": []}, TOKEN, COMPANY) == []


@pytest.mark.parametrize("bad_payload", [{"apiVersion": "1"}, ["not", "a", "dict"]])
def test_malformed_payload_raises_fetch_error(bad_payload: object) -> None:
    with pytest.raises(FetchError):
        ashby.parse(bad_payload, TOKEN, COMPANY)


def test_fetch_returns_failed_board_result_on_404() -> None:
    with mock.patch(
        "joboS.adapters.ashby.request_json",
        side_effect=FetchError("HTTP 404", 404),
    ):
        result = ashby.fetch(TOKEN, COMPANY)

    assert isinstance(result, BoardResult)
    assert result.ok is False
    assert result.status == 404
    assert result.listings == []


def test_board_company_name_is_always_none() -> None:
    # Ashby's posting-api payload carries no company name field; the finder
    # must fall back to case-sensitive URL-token matching instead.
    assert ashby.board_company_name(_payload()) is None
