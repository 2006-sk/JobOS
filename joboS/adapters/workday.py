"""Workday adapter -- the trickiest board in this suite.

Follows the same two-function shape as `greenhouse.py`, the reference
implementation:

    parse(payload, tenant, site, host, company) -> list[Listing]  # pure
    fetch(tenant, site, host, company, session) -> BoardResult    # network

Endpoint: POST https://{host}/wday/cxs/{tenant}/{site}/jobs
Body:     {"limit": 20, "offset": <n>, "searchText": ""}

Three things make this adapter different from every other one in this repo,
and each is deliberate -- see the inline comments where they're implemented:

1. There is no native job id. `id` must be derived from the requisition
   number buried in `bulletFields` (or, failing that, the `externalPath`
   URL slug), and must exclude anything that changes between polls.
2. `postedOn` is relative human text ("Posted Today"), not a timestamp.
   `parse_posted_on()` turns it into an approximate epoch second.
3. A single Workday tenant can list thousands of jobs, and the API rejects
   `limit > 20`. Unbounded pagination would turn one company into ~100
   requests per poll, so `fetch()` is capped at `DEFAULT_MAX_PAGES` and
   relies on the board being sorted newest-first to still see everything
   that could plausibly be new since the last poll.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from ..http import FetchError, request_json
from ..models import BoardResult, Listing, clean_locations, hashed_id, make_id, now_ts

log = logging.getLogger(__name__)

ATS = "workday"
ENDPOINT = "https://{host}/wday/cxs/{tenant}/{site}/jobs"

# Verified against the live NVIDIA board: limit=100 returns an error body
# with no jobPostings at all. 20 is the API's actual ceiling, not a guess.
PAGE_SIZE = 20

# Verified: the board is sorted newest-first (offset 0 == "Posted Today",
# offset 1900 == "Posted 30+ Days Ago"). NVIDIA alone reports total=2000,
# which at PAGE_SIZE=20 is 100 POST requests per poll for one company --
# across ~10 Workday companies that's 1000 requests every 30 minutes and
# would get us blocked. 5 pages = 100 newest jobs, far more than a single
# 30-minute polling window could plausibly produce. Callers that need full
# coverage (bootstrap, smoke tests) should pass a large max_pages explicitly.
DEFAULT_MAX_PAGES = 5

# A locationsText placeholder like "3 Locations" carries no information at
# all -- it's Workday's way of saying "too many to list inline", not an
# actual location. Treating it as a real location would poison ranking and
# filtering with a literal string that isn't a place.
_PLACEHOLDER_LOCATIONS = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)

# Fallback for when bulletFields is empty: externalPath's last underscore-
# delimited segment is the requisition id, e.g. "..._JR2021016" -> "JR2021016".
# Some postings carry a trailing "-1" duplicate-posting suffix on this same
# segment (seen in the wild as "..._JR2023203-1"); this regex keeps it as
# part of the captured id rather than stripping it. That's fine in practice:
# this path only fires when bulletFields is absent, so a given posting can't
# flip between the bulletFields-derived id and this one from run to run.
_REQ_ID_FROM_PATH = re.compile(r"_([A-Za-z0-9-]+)$")

_DAY = 86_400


def parse_posted_on(text: str, *, now: int | None = None) -> int | None:
    """Turn Workday's relative posting text into an approximate epoch second.

    `now` is injectable so tests are deterministic -- production callers
    omit it and get the real clock via `now_ts()`.

    IMPORTANT: the result is APPROXIMATE, not authoritative. "30+ Days Ago"
    is a floor (it could be 30 or 300 days), and "N Months Ago" assumes a
    flat 30-day month. Nothing in this adapter should treat `posted_at` as
    exact -- state.py's first-seen-wins rule is what actually decides
    whether a job is new and needs a notification. This value is only for
    ranking and backtest windows, where being off by a few days is fine.
    """
    if now is None:
        now = now_ts()
    if not text:
        return None

    # Tolerate the "Posted " prefix being absent, and any casing.
    stripped = re.sub(r"^\s*posted\s+", "", text.strip(), flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", stripped).strip().lower()
    if not normalized:
        return None

    if normalized == "today":
        return now
    if normalized == "yesterday":
        return now - _DAY

    # "+" is optional so this also matches the plain "N Days Ago" form;
    # for "30+ Days Ago" this deliberately floors at 30 days rather than
    # trying to guess how much older the posting actually is.
    days_match = re.fullmatch(r"(\d+)\+?\s+days?\s+ago", normalized)
    if days_match:
        return now - int(days_match.group(1)) * _DAY

    months_match = re.fullmatch(r"(\d+)\s+months?\s+ago", normalized)
    if months_match:
        return now - int(months_match.group(1)) * 30 * _DAY

    return None


def _locations(locations_text: Any) -> tuple[str, ...]:
    if isinstance(locations_text, str) and _PLACEHOLDER_LOCATIONS.match(
        locations_text.strip()
    ):
        return ()
    return clean_locations(locations_text)


def _stable_id(posting: dict[str, Any], token: str, title: str, url: str) -> str:
    # 1) STABLE ID. Workday's cxs feed gives no native job id -- only the
    # human-facing requisition number, which lives in `bulletFields[0]`
    # (e.g. "JR2021016"). We deliberately build the id from ONLY the
    # (ats, token, req_id) triple -- never locationsText or postedOn, both
    # of which churn between polls (a job can move from "3 Locations" to a
    # specific city, or "Posted Today" ages into "Posted Yesterday") and
    # would otherwise mint a "new" job, and a re-notification, on every run.
    bullet_fields = posting.get("bulletFields")
    if isinstance(bullet_fields, list) and bullet_fields:
        req_id = str(bullet_fields[0]).strip()
        if req_id:
            return make_id(ATS, token, req_id)

    # Fall back to the requisition id embedded in the URL slug when
    # bulletFields is empty (seen on a handful of tenants).
    external_path = posting.get("externalPath")
    if isinstance(external_path, str):
        match = _REQ_ID_FROM_PATH.search(external_path)
        if match:
            return make_id(ATS, token, match.group(1))

    # Nothing usable at all -- hash (token, title, url), never location or
    # postedOn, for the same anti-churn reason as above.
    return hashed_id(ATS, token, title, url)


def parse(
    payload: Any, tenant: str, site: str, host: str, company: str
) -> list[Listing]:
    if not isinstance(payload, dict):
        raise FetchError(
            f"workday:{tenant}:{site} payload was {type(payload).__name__}"
        )
    postings = payload.get("jobPostings")
    if not isinstance(postings, list):
        raise FetchError(f"workday:{tenant}:{site} payload has no 'jobPostings' list")

    token = f"{tenant}:{site}"
    out: list[Listing] = []
    for posting in postings:
        if not isinstance(posting, dict):
            continue
        title = (posting.get("title") or "").strip()
        if not title:
            # No title means nothing downstream can display, regardless of
            # whether an id can be derived.
            continue

        external_path = posting.get("externalPath") or ""
        url = f"https://{host}{external_path}" if external_path else ""

        out.append(
            Listing(
                id=_stable_id(posting, token, title, url),
                company=company,
                title=title,
                url=url,
                locations=_locations(posting.get("locationsText")),
                # 2) RELATIVE DATES -- see parse_posted_on()'s docstring for
                # why this is approximate and not the notification trigger.
                posted_at=parse_posted_on(posting.get("postedOn") or ""),
                source_ats=ATS,
                board_token=token,
                # Workday's cxs search feed carries no category or
                # employment-type field anywhere in the posting shape.
                employment_type=None,
                raw_category=None,
            )
        )
    return out


def fetch(
    tenant: str,
    site: str,
    host: str,
    company: str,
    *,
    session: requests.Session | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> BoardResult:
    token = f"{tenant}:{site}"
    url = ENDPOINT.format(host=host, tenant=tenant, site=site)
    all_listings: list[Listing] = []
    offset = 0
    total: int | None = None

    # 3) BOUNDED PAGINATION. Stops when a page comes back empty, when
    # offset reaches the reported total, or when max_pages is hit --
    # whichever comes first. See DEFAULT_MAX_PAGES above for why the cap
    # exists at all.
    for page in range(max_pages):
        body = {"limit": PAGE_SIZE, "offset": offset, "searchText": ""}
        try:
            payload = request_json(url, method="POST", json_body=body, session=session)
            page_listings = parse(payload, tenant, site, host, company)
        except FetchError as exc:
            if page == 0:
                # A dead board on the very first page is a real failure --
                # the caller needs to know this token/tenant isn't working.
                log.warning("workday:%s failed: %s", token, exc)
                return BoardResult(
                    company, ATS, token, [], ok=False, status=exc.status, error=str(exc)
                )
            # A later page failing is a transient hiccup, not proof the
            # board is down -- keep everything already collected rather
            # than throwing away a partial, still-useful result.
            log.warning(
                "workday:%s failed at offset=%d after %d page(s), "
                "returning partial results: %s",
                token,
                offset,
                page,
                exc,
            )
            break

        all_listings.extend(page_listings)

        raw_postings = payload.get("jobPostings") if isinstance(payload, dict) else None
        raw_total = payload.get("total") if isinstance(payload, dict) else None
        # Only the FIRST page carries a real total: verified against NVIDIA,
        # offset=0 reports total=2000 while offset=20/40/60 all report total=0
        # while still returning a full page of jobs. Trusting the later value
        # made `offset >= total` fire on page 2 and silently capped every
        # Workday board at 40 jobs -- a coverage loss with no error anywhere.
        if isinstance(raw_total, int) and raw_total > 0 and total is None:
            total = raw_total

        if not raw_postings:
            break  # empty page -- nothing further to read
        offset += PAGE_SIZE
        if total is not None and offset >= total:
            break
    else:
        # The for-loop ran out of iterations without hitting any of the
        # `break`s above, i.e. we stopped only because max_pages was
        # reached while more jobs remained. Silent truncation here would
        # be invisible in normal operation, so log it explicitly.
        if total is not None and offset < total:
            log.info(
                "workday:%s stopped at max_pages=%d: read %d of %d jobs",
                token,
                max_pages,
                len(all_listings),
                total,
            )

    return BoardResult(company, ATS, token, all_listings)
