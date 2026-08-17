"""Greenhouse adapter -- the reference implementation.

Every adapter in this package follows the same two-function shape:

    parse(payload, token, company) -> list[Listing]   # pure, no network
    fetch(token, company, session) -> BoardResult     # network, never raises

Keeping `parse` pure is what lets the whole adapter suite be tested against
recorded fixtures with no network at all, and `fetch` swallowing its own errors
into `BoardResult.ok=False` is what gives us per-board failure isolation.

Endpoint: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from ..http import FetchError, request_json
from ..models import BoardResult, Listing, clean_locations, make_id, parse_ts

log = logging.getLogger(__name__)

ATS = "greenhouse"
BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false"


def parse(payload: Any, token: str, company: str) -> list[Listing]:
    if not isinstance(payload, dict):
        raise FetchError(f"greenhouse:{token} payload was {type(payload).__name__}")
    jobs = payload.get("jobs")
    if jobs is None:
        raise FetchError(f"greenhouse:{token} payload has no 'jobs' key")
    if not isinstance(jobs, list):
        raise FetchError(f"greenhouse:{token} 'jobs' was {type(jobs).__name__}")

    out: list[Listing] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        native = job.get("id") or job.get("internal_job_id")
        url = job.get("absolute_url") or ""
        title = (job.get("title") or "").strip()
        if not native or not title:
            # No id or no title means nothing downstream can act on it.
            continue

        # `first_published` is the real posting date; `updated_at` moves whenever
        # anyone edits the req. We record both and let state.py trigger on
        # first-time-seen, so an edited 6-month-old job never pings as new.
        out.append(
            Listing(
                id=make_id(ATS, token, native),
                # A board's self-reported company_name is the ground truth used
                # by the finder to catch mis-resolved tokens (IMC -> ING etc).
                company=(job.get("company_name") or company).strip() or company,
                title=title,
                url=url,
                locations=clean_locations(job.get("location"), job.get("offices")),
                posted_at=parse_ts(job.get("first_published"))
                or parse_ts(job.get("updated_at")),
                source_ats=ATS,
                board_token=token,
                employment_type=None,
                raw_category=_department(job),
                updated_at=parse_ts(job.get("updated_at")),
            )
        )
    return out


def _department(job: dict[str, Any]) -> str | None:
    depts = job.get("departments")
    if isinstance(depts, list) and depts:
        first = depts[0]
        if isinstance(first, dict) and first.get("name"):
            return str(first["name"])
    return None


def board_company_name(payload: Any) -> str | None:
    """Company this board actually belongs to, per Greenhouse itself.

    The finder uses this to reject tokens that resolve to a live board owned by
    the wrong company -- "the API returned jobs" proves the token works, not
    that it is the company we were looking for.
    """
    if isinstance(payload, dict):
        for job in payload.get("jobs") or []:
            if isinstance(job, dict) and job.get("company_name"):
                return str(job["company_name"])
    return None


def fetch(
    token: str, company: str, *, session: requests.Session | None = None
) -> BoardResult:
    url = BASE.format(token=token)
    try:
        payload = request_json(url, session=session)
        return BoardResult(company, ATS, token, parse(payload, token, company))
    except FetchError as exc:
        log.warning("greenhouse:%s failed: %s", token, exc)
        return BoardResult(
            company, ATS, token, [], ok=False, status=exc.status, error=str(exc)
        )
