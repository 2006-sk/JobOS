"""Tests for the Workday adapter, against a real recorded payload.

The fixture is a genuine (trimmed) response from NVIDIA's Workday cxs feed,
so these tests double as a check that the adapter tolerates the real shape
of the API rather than an idealized one.
"""

from __future__ import annotations

import json
import logging
import pathlib
from unittest import mock

import pytest

from joboS.adapters import workday
from joboS.http import FetchError
from joboS.models import BoardResult, now_ts

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "workday_nvidia.json"
TENANT = "nvidia"
SITE = "NVIDIAExternalCareerSite"
HOST = "nvidia.wd5.myworkdayjobs.com"
COMPANY = "NVIDIA"
TOKEN = f"{TENANT}:{SITE}"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_returns_listings_with_ats_and_token() -> None:
    listings = workday.parse(_payload(), TENANT, SITE, HOST, COMPANY)
    assert len(listings) == 10
    for listing in listings:
        assert listing.source_ats == "workday"
        assert listing.board_token == TOKEN


def test_id_derives_from_bullet_fields() -> None:
    listings = workday.parse(_payload(), TENANT, SITE, HOST, COMPANY)
    assert any(listing.id.endswith("JR2021016") for listing in listings)


def test_parse_wires_posted_on_into_posted_at() -> None:
    # Proves parse() actually feeds postedOn through parse_posted_on() --
    # every fixture posting says "Posted Today", so posted_at must be
    # populated and land within a day of the real clock, mirroring Lever's
    # ms-vs-seconds regression test for the same kind of wiring bug.
    listings = workday.parse(_payload(), TENANT, SITE, HOST, COMPANY)
    now = now_ts()
    for listing in listings:
        assert listing.posted_at is not None
        assert now - 86_400 <= listing.posted_at <= now


def test_id_stable_across_parses() -> None:
    first = workday.parse(_payload(), TENANT, SITE, HOST, COMPANY)
    second = workday.parse(_payload(), TENANT, SITE, HOST, COMPANY)
    assert [listing.id for listing in first] == [listing.id for listing in second]


def test_id_does_not_change_when_location_or_posted_on_differ() -> None:
    # This is the critical anti-duplicate-ping regression test: if location
    # or postedOn text leaked into the id, a job moving from "3 Locations"
    # to a resolved city (or aging from "Posted Today" to "Posted
    # Yesterday") would look like a brand-new job on the next poll.
    base = {
        "title": "Senior Widget Engineer",
        "externalPath": "/job/US-CA-Santa-Clara/Senior-Widget-Engineer_JR9999999",
        "bulletFields": ["JR9999999"],
    }
    payload_a = {
        "total": 1,
        "jobPostings": [
            {**base, "locationsText": "3 Locations", "postedOn": "Posted Today"}
        ],
    }
    payload_b = {
        "total": 1,
        "jobPostings": [
            {
                **base,
                "locationsText": "US, CA, Santa Clara",
                "postedOn": "Posted 12 Days Ago",
            }
        ],
    }
    listing_a = workday.parse(payload_a, TENANT, SITE, HOST, COMPANY)[0]
    listing_b = workday.parse(payload_b, TENANT, SITE, HOST, COMPANY)[0]
    assert listing_a.id == listing_b.id


def test_id_falls_back_to_external_path_when_bullet_fields_empty() -> None:
    payload = {
        "total": 1,
        "jobPostings": [
            {
                "title": "Fallback Job",
                "externalPath": "/job/US-CA-Santa-Clara/Fallback-Job_JR1234567",
                "locationsText": "US, CA, Santa Clara",
                "postedOn": "Posted Today",
                "bulletFields": [],
            }
        ],
    }
    listing = workday.parse(payload, TENANT, SITE, HOST, COMPANY)[0]
    assert listing.id.endswith("JR1234567")


def test_id_falls_back_to_external_path_keeps_duplicate_suffix() -> None:
    # Field-mapping surprise found in the live fixture: one NVIDIA posting's
    # externalPath ends "..._JR2023203-1" while its own bulletFields is
    # ["JR2023203"] (no suffix). This test pins the fallback-only behavior:
    # when bulletFields is empty, the "-1" is kept as part of the id rather
    # than stripped, since the fallback regex has no way to know it's a
    # duplicate-posting marker rather than part of the requisition id itself.
    payload = {
        "total": 1,
        "jobPostings": [
            {
                "title": "SDET Fallback",
                "externalPath": "/job/China-Shanghai/SDET_JR2023203-1",
                "locationsText": "China, Shanghai",
                "postedOn": "Posted Today",
                "bulletFields": [],
            }
        ],
    }
    listing = workday.parse(payload, TENANT, SITE, HOST, COMPANY)[0]
    assert listing.id.endswith("JR2023203-1")


def test_id_hashes_when_nothing_usable() -> None:
    payload = {
        "total": 1,
        "jobPostings": [
            {
                "title": "No Requisition Id",
                "externalPath": "/job/US-CA-Santa-Clara/No-Requisition-Id",
                "locationsText": "US, CA, Santa Clara",
                "postedOn": "Posted Today",
                "bulletFields": [],
            }
        ],
    }
    listing = workday.parse(payload, TENANT, SITE, HOST, COMPANY)[0]
    assert f"workday:{TOKEN}:h" in listing.id


@pytest.mark.parametrize(
    ("text", "expected_days_ago"),
    [
        ("Posted Today", 0),
        ("posted today", 0),
        ("Today", 0),
        ("Posted Yesterday", 1),
        ("Yesterday", 1),
        ("Posted 5 Days Ago", 5),
        ("Posted 1 Day Ago", 1),
        ("Posted 30+ Days Ago", 30),
        ("Posted 2 Months Ago", 60),
        ("Posted 1 Month Ago", 30),
    ],
)
def test_parse_posted_on_covers_every_relative_form(
    text: str, expected_days_ago: int
) -> None:
    now = 1_800_000_000
    assert workday.parse_posted_on(text, now=now) == now - expected_days_ago * 86_400


def test_parse_posted_on_unrecognized_returns_none() -> None:
    now = 1_800_000_000
    assert workday.parse_posted_on("Posted 3 Weeks Ago", now=now) is None
    assert workday.parse_posted_on("", now=now) is None
    assert workday.parse_posted_on("garbage", now=now) is None


def test_placeholder_locations_text_yields_empty_tuple() -> None:
    listings = workday.parse(_payload(), TENANT, SITE, HOST, COMPANY)
    three_locations = next(
        listing
        for listing in listings
        if listing.id.endswith("JR2021016")  # locationsText == "3 Locations"
    )
    assert three_locations.locations == ()


def test_real_locations_text_yields_non_empty_tuple() -> None:
    listings = workday.parse(_payload(), TENANT, SITE, HOST, COMPANY)
    resolved = next(
        listing
        for listing in listings
        if listing.id.endswith("JR2018518")  # locationsText == "US, CA, Santa Clara"
    )
    assert resolved.locations != ()


def test_url_is_built_from_host_and_external_path() -> None:
    listings = workday.parse(_payload(), TENANT, SITE, HOST, COMPANY)
    listing = next(listing for listing in listings if listing.id.endswith("JR2021016"))
    assert listing.url == (
        f"https://{HOST}/job/US-CA-Santa-Clara/"
        "Verification-Engineer---New-College-Grad-2026_JR2021016"
    )


def test_skips_postings_with_no_title() -> None:
    payload = {
        "total": 1,
        "jobPostings": [
            {
                "title": "",
                "externalPath": "/job/US-CA-Santa-Clara/_JR0000001",
                "locationsText": "US, CA, Santa Clara",
                "postedOn": "Posted Today",
                "bulletFields": ["JR0000001"],
            }
        ],
    }
    assert workday.parse(payload, TENANT, SITE, HOST, COMPANY) == []


def test_zero_jobs_parses_to_empty_list() -> None:
    payload = {"total": 0, "jobPostings": [], "facets": [], "userAuthenticated": False}
    assert workday.parse(payload, TENANT, SITE, HOST, COMPANY) == []


@pytest.mark.parametrize(
    "bad_payload",
    [
        ["not", "a", "dict"],
        {"facets": [], "userAuthenticated": False},  # no jobPostings key
        {"jobPostings": "not a list"},
    ],
)
def test_malformed_payload_raises_fetch_error(bad_payload: object) -> None:
    with pytest.raises(FetchError):
        workday.parse(bad_payload, TENANT, SITE, HOST, COMPANY)


def _full_page_payload() -> dict:
    posting = {
        "title": "Repeated Job",
        "externalPath": "/job/US-CA-Santa-Clara/Repeated-Job_JR0000000",
        "locationsText": "US, CA, Santa Clara",
        "postedOn": "Posted Today",
        "bulletFields": ["JR0000000"],
    }
    return {
        "total": 2000,
        "jobPostings": [posting] * workday.PAGE_SIZE,
        "facets": [],
        "userAuthenticated": False,
    }


def test_fetch_stops_at_max_pages() -> None:
    with mock.patch(
        "joboS.adapters.workday.request_json", return_value=_full_page_payload()
    ) as mocked:
        result = workday.fetch(TENANT, SITE, HOST, COMPANY, max_pages=3)

    assert mocked.call_count == 3
    assert result.ok is True
    assert result.count == 60

    # Pin the exact request contract -- a wrong body key, limit=100, or a
    # GET instead of POST would still pass every other assertion here but
    # fail against the live board, which is exactly the failure mode the
    # spec's measured findings called out.
    expected_url = workday.ENDPOINT.format(host=HOST, tenant=TENANT, site=SITE)
    offsets = []
    for call in mocked.call_args_list:
        assert call.args[0] == expected_url
        assert call.kwargs["method"] == "POST"
        body = call.kwargs["json_body"]
        assert body["limit"] == workday.PAGE_SIZE
        assert body["searchText"] == ""
        offsets.append(body["offset"])
    assert offsets == [0, workday.PAGE_SIZE, 2 * workday.PAGE_SIZE]


def test_fetch_logs_when_it_stops_early_due_to_max_pages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _full_page_payload()
    with (
        caplog.at_level(logging.INFO, logger="joboS.adapters.workday"),
        mock.patch("joboS.adapters.workday.request_json", return_value=payload),
    ):
        workday.fetch(TENANT, SITE, HOST, COMPANY, max_pages=3)

    # "Silent truncation must be visible" -- there must be exactly one INFO
    # line stating how many of `total` were actually read.
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    assert "60" in info_records[0].message
    assert "2000" in info_records[0].message


def test_fetch_stops_when_offset_reaches_total() -> None:
    # total=25 with PAGE_SIZE=20 should read 2 pages (offset 0, offset 20)
    # and stop naturally, well under a generous max_pages.
    small_total_payload = _full_page_payload()
    small_total_payload["total"] = 25
    with mock.patch(
        "joboS.adapters.workday.request_json", return_value=small_total_payload
    ) as mocked:
        result = workday.fetch(TENANT, SITE, HOST, COMPANY, max_pages=50)

    assert mocked.call_count == 2
    assert result.count == 40


def test_fetch_stops_on_empty_page() -> None:
    with mock.patch(
        "joboS.adapters.workday.request_json",
        return_value={"total": 0, "jobPostings": [], "facets": []},
    ) as mocked:
        result = workday.fetch(TENANT, SITE, HOST, COMPANY, max_pages=50)

    assert mocked.call_count == 1
    assert result.count == 0
    assert result.ok is True


def test_fetch_returns_failed_board_result_on_page_one_404() -> None:
    with mock.patch(
        "joboS.adapters.workday.request_json",
        side_effect=FetchError("HTTP 404", 404),
    ):
        result = workday.fetch(TENANT, SITE, HOST, COMPANY)

    assert isinstance(result, BoardResult)
    assert result.ok is False
    assert result.status == 404
    assert result.listings == []


def test_fetch_keeps_partial_results_when_a_later_page_fails() -> None:
    call_count = 0

    def _side_effect(*args: object, **kwargs: object) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _full_page_payload()
        raise FetchError("HTTP 503", 503)

    with mock.patch(
        "joboS.adapters.workday.request_json", side_effect=_side_effect
    ):
        result = workday.fetch(TENANT, SITE, HOST, COMPANY, max_pages=5)

    assert result.ok is True
    assert result.count == workday.PAGE_SIZE
