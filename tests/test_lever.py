"""Tests for the Lever adapter, against a real recorded payload.

The fixture is a genuine (trimmed) response for Palantir's Lever board, so
these tests double as a check that the adapter tolerates the real shape of
the API rather than an idealized one.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any
from unittest import mock

import pytest

from joboS.adapters import lever
from joboS.http import FetchError
from joboS.models import BoardResult

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "lever_palantir.json"
TOKEN = "palantir"
COMPANY = "Palantir"

_ID_RE = re.compile(
    r"^lever:palantir:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _payload() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(FIXTURE.read_text())
    return data


def test_parse_returns_all_listings_with_ats_and_token() -> None:
    listings = lever.parse(_payload(), TOKEN, COMPANY)
    assert len(listings) == 10
    for listing in listings:
        assert listing.source_ats == "lever"
        assert listing.board_token == TOKEN


def test_ids_are_unique_stable_and_shaped() -> None:
    first = lever.parse(_payload(), TOKEN, COMPANY)
    second = lever.parse(_payload(), TOKEN, COMPANY)

    ids_first = [listing.id for listing in first]
    ids_second = [listing.id for listing in second]
    assert len(ids_first) == len(set(ids_first))  # unique within one parse
    assert ids_first == ids_second  # stable across separate parse() calls

    for listing in first:
        # "lever:<token>:<uuid>" -- the native Lever id, not a content hash,
        # since every fixture posting carries an "id". A regex (rather than
        # a prefix check) also catches a regression to the hashed_id path.
        assert _ID_RE.match(listing.id)


def test_title_comes_from_text_field_not_title() -> None:
    listings = lever.parse(_payload(), TOKEN, COMPANY)
    titles = {listing.title for listing in listings}
    # Known title straight from the fixture's "text" field.
    assert "Administrative Business Partner" in titles


def test_posted_at_is_seconds_not_milliseconds() -> None:
    listings = lever.parse(_payload(), TOKEN, COMPANY)
    for listing in listings:
        assert listing.posted_at is not None
        # Regression guard for the ms-timestamp bug: a correct epoch-seconds
        # value for a real posting lands well within this window, whereas an
        # unconverted milliseconds value would be ~1000x too large.
        assert 1_600_000_000 < listing.posted_at < 2_000_000_000


def test_employment_type_and_locations_populated() -> None:
    listings = lever.parse(_payload(), TOKEN, COMPANY)
    admin = next(
        listing
        for listing in listings
        if listing.title == "Administrative Business Partner"
    )
    assert admin.employment_type == "Full-time"
    assert admin.locations == ("London, United Kingdom",)


def test_remote_workplace_type_appends_remote_location() -> None:
    listings = lever.parse(_payload(), TOKEN, COMPANY)
    fellowship = next(
        listing for listing in listings if listing.title == "American Tech Fellowship"
    )
    assert "Remote" in fellowship.locations


def test_empty_board_parses_to_empty_list() -> None:
    assert lever.parse([], TOKEN, COMPANY) == []


@pytest.mark.parametrize("bad_payload", [{"postings": []}, "not a list"])
def test_malformed_payload_raises_fetch_error(bad_payload: object) -> None:
    with pytest.raises(FetchError):
        lever.parse(bad_payload, TOKEN, COMPANY)


def test_fetch_returns_failed_board_result_on_404() -> None:
    with mock.patch(
        "joboS.adapters.lever.request_json",
        side_effect=FetchError("HTTP 404", 404),
    ):
        result = lever.fetch(TOKEN, COMPANY)

    assert isinstance(result, BoardResult)
    assert result.ok is False
    assert result.status == 404
    assert result.listings == []


def test_board_company_name_is_always_none() -> None:
    # Lever payloads carry no company name field; the finder must fall back
    # to matching the URL token instead of trusting a self-reported name.
    assert lever.board_company_name(_payload()) is None
