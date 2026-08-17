"""SmartRecruiters adapter.

Same two-function shape as every other adapter (see greenhouse.py, the
reference implementation):

    parse(payload, token, company) -> list[Listing]   # pure, no network
    fetch(token, company, session) -> BoardResult     # network, never raises

Endpoint: https://api.smartrecruiters.com/v1/companies/{token}/postings

Two things about this API bite people who assume it looks like Greenhouse:

1. The job title lives under the key "name", not "title". "title" does not
   exist on a posting at all. Get this wrong and every listing silently gets
   an empty title and gets skipped.
2. The API is paginated (`offset`/`limit`/`totalFound`) where Greenhouse's
   `jobs` endpoint returns everything in one shot. `fetch()` below loops
   pages; `parse()` stays pure and single-page so it is still fixture-testable
   with no network, matching the rest of the adapter suite.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from ..http import FetchError, request_json
from ..models import BoardResult, Listing, clean_locations, make_id, parse_ts

log = logging.getLogger(__name__)

ATS = "smartrecruiters"
BASE = (
    "https://api.smartrecruiters.com/v1/companies/{token}/postings"
    "?limit={limit}&offset={offset}"
)
PAGE_SIZE = 100


def parse(payload: Any, token: str, company: str) -> list[Listing]:
    if not isinstance(payload, dict):
        raise FetchError(
            f"smartrecruiters:{token} payload was {type(payload).__name__}"
        )
    content = payload.get("content")
    if content is None:
        raise FetchError(f"smartrecruiters:{token} payload has no 'content' key")
    if not isinstance(content, list):
        raise FetchError(
            f"smartrecruiters:{token} 'content' was {type(content).__name__}"
        )

    out: list[Listing] = []
    for posting in content:
        if not isinstance(posting, dict):
            continue
        native = posting.get("id")
        # The title is "name" here, not "title" -- and it comes with trailing
        # whitespace in the wild ("...Engineering "), so strip it or every
        # downstream string comparison (dedup, relevance) quietly misses.
        title = (posting.get("name") or "").strip()
        if not native or not title:
            # No id or no title means nothing downstream can act on it.
            continue

        location = posting.get("location")
        if not isinstance(location, dict):
            # Guard against a tenant sending "location" as a bare string or a
            # list -- rare, but `.get()` on anything else would raise past
            # `parse()` and, since `fetch()` only catches `FetchError`, past
            # `fetch()` too, breaking the "adapters never raise" contract.
            location = {}
        loc_values: list[Any] = [location.get("fullLocation"), location.get("city")]
        if location.get("remote"):
            loc_values.append("Remote")

        # `ref` is the API resource url (what you'd GET to re-fetch this
        # posting), not a page a human can open in a browser. The public apply
        # page instead follows this fixed, undocumented-but-stable pattern.
        url = f"https://jobs.smartrecruiters.com/{token}/{native}"

        dept = posting.get("department")
        func = posting.get("function")
        dept_label = dept.get("label") if isinstance(dept, dict) else None
        func_label = func.get("label") if isinstance(func, dict) else None
        # `department` is frequently `{}` (empty) on real postings; `function`
        # is the reliable one, so it's a fallback rather than the primary.
        raw_category = dept_label or func_label

        employment = posting.get("typeOfEmployment")
        employment_type = (
            employment.get("label") if isinstance(employment, dict) else None
        )

        comp = posting.get("company")
        company_name = comp.get("name") if isinstance(comp, dict) else None

        # `experienceLevel` (e.g. "Entry Level") is deliberately ignored: it
        # isn't a category or an employment type, and relevance.py already
        # classifies seniority from the title, so surfacing it here would
        # just be a second, possibly-conflicting signal for the same thing.

        out.append(
            Listing(
                id=make_id(ATS, token, native),
                company=str(company_name).strip() if company_name else company,
                title=title,
                url=url,
                locations=clean_locations(*loc_values),
                posted_at=parse_ts(posting.get("releasedDate")),
                source_ats=ATS,
                board_token=token,
                employment_type=str(employment_type) if employment_type else None,
                raw_category=str(raw_category) if raw_category else None,
            )
        )
    return out


def board_company_name(payload: Any) -> str | None:
    """Company this board actually belongs to, per SmartRecruiters itself.

    Used by the finder to reject tokens that resolve to a live board owned by
    the wrong company -- a 200 with postings only proves the token works, not
    that it's the company we were looking for.
    """
    if isinstance(payload, dict):
        for posting in payload.get("content") or []:
            if isinstance(posting, dict):
                comp = posting.get("company")
                if isinstance(comp, dict) and comp.get("name"):
                    return str(comp["name"])
    return None


def fetch(
    token: str,
    company: str,
    *,
    session: requests.Session | None = None,
    max_pages: int = 10,
) -> BoardResult:
    collected: list[Listing] = []
    total_found: int | None = None
    offset = 0
    page = 0

    while page < max_pages:
        url = BASE.format(token=token, limit=PAGE_SIZE, offset=offset)
        try:
            payload = request_json(url, session=session)
            page_listings = parse(payload, token, company)
        except FetchError as exc:
            if page == 0:
                log.warning("smartrecruiters:%s failed: %s", token, exc)
                return BoardResult(
                    company, ATS, token, [], ok=False, status=exc.status, error=str(exc)
                )
            # A later page failing (rate limit, transient 5xx) shouldn't wipe
            # out everything collected so far -- a partial board beats an
            # empty one for a source that was otherwise working.
            log.warning(
                "smartrecruiters:%s page %d failed, returning partial results: %s",
                token,
                page,
                exc,
            )
            break

        raw_content = payload.get("content") if isinstance(payload, dict) else None
        raw_count = len(raw_content) if isinstance(raw_content, list) else 0
        collected.extend(page_listings)
        if page == 0 and isinstance(payload, dict):
            found = payload.get("totalFound")
            total_found = found if isinstance(found, int) else None
        page += 1

        if raw_count == 0:
            # `totalFound` has been seen to be stale/wrong on this API; if we
            # trusted it alone, a bad count could make us re-request offsets
            # past the end forever. An empty page is the one signal that's
            # always reliable that there is nothing left to fetch.
            break
        offset += PAGE_SIZE
        if total_found is not None and len(collected) >= total_found:
            break

    return BoardResult(company, ATS, token, collected)
