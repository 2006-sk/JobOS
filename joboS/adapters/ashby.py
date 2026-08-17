"""Ashby adapter.

Follows the same two-function shape as `greenhouse.py`, the reference
implementation:

    parse(payload, token, company) -> list[Listing]   # pure, no network
    fetch(token, company, session) -> BoardResult     # network, never raises

Endpoint: https://api.ashbyhq.com/posting-api/job-board/{token}

Two Ashby quirks that don't show up anywhere else in this codebase:

1. The board token is CASE-SENSITIVE -- "Etched" resolves, "etched" typically
   does not. Any caller deriving a token from a company name (lowercasing,
   slugifying) will silently 404 boards that actually exist, so tokens must
   be taken verbatim from wherever they were sourced.
2. Ashby returns *every* job it has ever indexed for the board, including
   ones the company has since unpublished; `isListed: false` is the only
   signal that a posting is a draft or pulled req rather than a live one.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from ..http import FetchError, request_json
from ..models import BoardResult, Listing, clean_locations, hashed_id, make_id, parse_ts

log = logging.getLogger(__name__)

ATS = "ashby"
BASE = "https://api.ashbyhq.com/posting-api/job-board/{token}"

# CamelCase-with-no-space is Ashby's own vocabulary for these, not ours.
# Unrecognized values (Ashby adds new ones without notice) fall through to
# the raw string rather than being dropped, per `_employment_type` below.
_EMPLOYMENT_TYPES = {
    "FullTime": "Full-time",
    "PartTime": "Part-time",
    "Intern": "Intern",
    "Contract": "Contract",
    "Temporary": "Temporary",
    "Apprenticeship": "Apprenticeship",
}


def _employment_type(value: Any) -> str | None:
    if not value:
        return None
    return _EMPLOYMENT_TYPES.get(value, str(value))


def parse(payload: Any, token: str, company: str) -> list[Listing]:
    if not isinstance(payload, dict):
        raise FetchError(f"ashby:{token} payload was {type(payload).__name__}")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise FetchError(f"ashby:{token} payload has no 'jobs' list")

    out: list[Listing] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        # `isListed` false means the req is a draft or has been pulled --
        # Ashby still serves it in this feed, but notifying on it would ping
        # for a job nobody can actually apply to.
        if job.get("isListed") is False:
            continue

        title = (job.get("title") or "").strip()
        if not title:
            # No title means nothing downstream can display, regardless of
            # whether an id exists.
            continue

        native = job.get("id")
        # `jobUrl` is the public listing page; `applyUrl` is a fallback for
        # the rare posting missing it -- both resolve to the same job.
        url = job.get("jobUrl") or job.get("applyUrl") or ""
        if native:
            listing_id = make_id(ATS, token, native)
        elif url:
            # No native id at all -- fall back to a content hash rather than
            # dropping the posting outright.
            listing_id = hashed_id(ATS, token, title, url)
        else:
            continue

        locations = clean_locations(job.get("location"), job.get("secondaryLocations"))
        if job.get("isRemote") and "remote" not in (loc.lower() for loc in locations):
            locations = (*locations, "Remote")

        out.append(
            Listing(
                id=listing_id,
                company=company,
                title=title,
                url=url,
                locations=locations,
                posted_at=parse_ts(job.get("publishedAt")),
                source_ats=ATS,
                board_token=token,
                employment_type=_employment_type(job.get("employmentType")),
                raw_category=job.get("department") or job.get("team"),
            )
        )
    return out


def board_company_name(payload: Any) -> str | None:
    """Ashby's posting-api payload carries no company-name field anywhere.

    Unlike Greenhouse, there's nothing here for the finder to cross-check a
    candidate token against -- it must fall back to matching the token
    against the company's known URL slug (jobs.ashbyhq.com/<token>) instead,
    keeping in mind that slug is case-sensitive.
    """
    return None


def fetch(
    token: str, company: str, *, session: requests.Session | None = None
) -> BoardResult:
    url = BASE.format(token=token)
    try:
        payload = request_json(url, session=session)
        return BoardResult(company, ATS, token, parse(payload, token, company))
    except FetchError as exc:
        log.warning("ashby:%s failed: %s", token, exc)
        return BoardResult(
            company, ATS, token, [], ok=False, status=exc.status, error=str(exc)
        )
