"""Tests for the SmartRecruiters adapter.

`parse()` and `board_company_name()` are exercised against the real fixture
recorded from Western Digital's board; `fetch()` is exercised with
`request_json` mocked out so pagination and failure handling are tested
without ever touching the network.
"""

from __future__ import annotations

import json
import pathlib
from unittest import mock

import pytest

from joboS.adapters import smartrecruiters as sr
from joboS.http import FetchError

FIXTURE = (
    pathlib.Path(__file__).parent / "fixtures" / "smartrecruiters_wd.json"
)


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_basic_fields() -> None:
    payload = _load_fixture()
    listings = sr.parse(payload, "WesternDigital", "Western Digital")

    assert listings, "fixture should yield at least one listing"
    for listing in listings:
        assert listing.source_ats == "smartrecruiters"
        assert listing.board_token == "WesternDigital"


def test_title_comes_from_name_and_is_stripped() -> None:
    payload = _load_fixture()
    listings = sr.parse(payload, "WesternDigital", "Western Digital")

    raw_names = [job["name"] for job in payload["content"]]
    # The fixture has at least one title with trailing whitespace -- assert
    # that condition holds (guards the test itself against a stale fixture)
    # and that parse() stripped it.
    assert any(name != name.strip() for name in raw_names)
    for listing in listings:
        assert listing.title == listing.title.strip()
        assert not listing.title.endswith(" ")


def test_url_is_human_page_not_api_ref() -> None:
    payload = _load_fixture()
    listings = sr.parse(payload, "WesternDigital", "Western Digital")

    for listing in listings:
        assert listing.url.startswith("https://jobs.smartrecruiters.com/")
        assert "api.smartrecruiters.com" not in listing.url


def test_ids_unique_and_stable_across_calls() -> None:
    payload = _load_fixture()
    first = sr.parse(payload, "WesternDigital", "Western Digital")
    second = sr.parse(payload, "WesternDigital", "Western Digital")

    first_ids = [listing.id for listing in first]
    second_ids = [listing.id for listing in second]

    assert len(first_ids) == len(set(first_ids)), "ids must be unique"
    assert first_ids == second_ids, "ids must be stable across parse() calls"


def test_posted_at_is_plausible_utc_epoch_seconds() -> None:
    payload = _load_fixture()
    listings = sr.parse(payload, "WesternDigital", "Western Digital")

    assert listings
    for listing in listings:
        assert listing.posted_at is not None
        assert 1_600_000_000 < listing.posted_at < 2_000_000_000


def test_board_company_name() -> None:
    payload = _load_fixture()
    assert sr.board_company_name(payload) == "Western Digital"


def test_remote_posting_gets_remote_location() -> None:
    payload = {
        "offset": 0,
        "limit": 10,
        "totalFound": 1,
        "content": [
            {
                "id": "999",
                "name": "Remote Software Engineer",
                "releasedDate": "2026-01-01T00:00:00.000Z",
                "location": {
                    "city": "Anywhere",
                    "region": "",
                    "country": "us",
                    "remote": True,
                    "hybrid": False,
                    "fullLocation": "Anywhere, US",
                },
                "company": {"identifier": "Acme", "name": "Acme Inc"},
                "department": {},
                "function": {"label": "Engineering"},
                "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
                "experienceLevel": {"id": "mid", "label": "Mid Level"},
                "ref": "https://api.smartrecruiters.com/v1/companies/Acme/postings/999",
            }
        ],
    }

    listings = sr.parse(payload, "Acme", "Acme Inc")
    assert len(listings) == 1
    assert "Remote" in listings[0].locations


def test_zero_jobs_returns_empty_list() -> None:
    payload = {"offset": 0, "limit": 10, "totalFound": 0, "content": []}
    assert sr.parse(payload, "Acme", "Acme Inc") == []


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"offset": 0, "limit": 10, "totalFound": 0},
    ],
)
def test_malformed_payload_raises_fetch_error(payload: object) -> None:
    with pytest.raises(FetchError):
        sr.parse(payload, "Acme", "Acme Inc")


def _page(items: list[dict], total_found: int) -> dict:
    return {
        "offset": 0,
        "limit": sr.PAGE_SIZE,
        "totalFound": total_found,
        "content": items,
    }


def _job(native_id: str) -> dict:
    return {
        "id": native_id,
        "name": f"Job {native_id}",
        "releasedDate": "2026-01-01T00:00:00.000Z",
        "location": {"city": "Remote", "remote": False, "fullLocation": "Remote"},
        "company": {"identifier": "Acme", "name": "Acme Inc"},
        "department": {},
        "function": {"label": "Engineering"},
        "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
        "experienceLevel": {"id": "mid", "label": "Mid Level"},
        "ref": (
            f"https://api.smartrecruiters.com/v1/companies/Acme/postings/{native_id}"
        ),
    }


def test_fetch_paginates_and_stops_on_empty_page() -> None:
    # totalFound is deliberately wrong (10) to prove the empty-page guard,
    # not totalFound, is what stops the loop.
    page1 = _page([_job("1"), _job("2")], total_found=10)
    page2 = _page([_job("3"), _job("4")], total_found=10)
    page3 = _page([], total_found=10)

    with mock.patch.object(
        sr, "request_json", side_effect=[page1, page2, page3]
    ) as mocked:
        result = sr.fetch("Acme", "Acme Inc", max_pages=10)

    assert result.ok is True
    assert mocked.call_count == 3, "must stop after the empty page, not loop forever"
    assert {listing.id for listing in result.listings} == {
        "smartrecruiters:Acme:1",
        "smartrecruiters:Acme:2",
        "smartrecruiters:Acme:3",
        "smartrecruiters:Acme:4",
    }
    assert result.count == 4


def test_fetch_404_on_first_page_returns_not_ok() -> None:
    with mock.patch.object(
        sr, "request_json", side_effect=FetchError("HTTP 404", status=404)
    ):
        result = sr.fetch("DoesNotExist", "Nobody")

    assert result.ok is False
    assert result.status == 404
    assert result.listings == []


def test_fetch_partial_failure_on_later_page_keeps_ok_true() -> None:
    page1 = _page([_job("1"), _job("2")], total_found=10)

    with mock.patch.object(
        sr,
        "request_json",
        side_effect=[page1, FetchError("HTTP 500", status=500)],
    ):
        result = sr.fetch("Acme", "Acme Inc", max_pages=10)

    assert result.ok is True
    assert result.count == 2
