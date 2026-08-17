"""Lever adapter.

Follows the same two-function shape as `greenhouse.py`, the reference
implementation:

    parse(payload, token, company) -> list[Listing]   # pure, no network
    fetch(token, company, session) -> BoardResult     # network, never raises

Endpoint: https://api.lever.co/v0/postings/{token}?mode=json

Two Lever quirks that don't show up anywhere else in this codebase:

1. The payload root is a JSON *list* of postings, not a dict with a jobs
   key. A dict here means the token resolved to something else entirely
   (Lever's own error shape, or a redirected page) and must be treated as
   malformed, same as Greenhouse returning non-list `jobs`.
2. The job title lives under `"text"`, not `"title"`. Every other adapter
   in this repo uses `title`; trusting that naming here silently produces
   empty titles instead of a KeyError, which is why it's called out.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from ..http import FetchError, request_json
from ..models import BoardResult, Listing, clean_locations, hashed_id, make_id, parse_ts

log = logging.getLogger(__name__)

ATS = "lever"
BASE = "https://api.lever.co/v0/postings/{token}?mode=json"


def parse(payload: Any, token: str, company: str) -> list[Listing]:
    if not isinstance(payload, list):
        raise FetchError(f"lever:{token} payload was {type(payload).__name__}")

    out: list[Listing] = []
    for posting in payload:
        if not isinstance(posting, dict):
            continue
        title = (posting.get("text") or "").strip()
        if not title:
            # No title means nothing downstream can display, regardless of
            # whether an id exists.
            continue

        native = posting.get("id")
        # `applyUrl` is a fallback for the rare posting missing `hostedUrl`;
        # both point at the same job, applyUrl just skips the read-only page.
        url = posting.get("hostedUrl") or posting.get("applyUrl") or ""
        if native:
            listing_id = make_id(ATS, token, native)
        elif url:
            # No native id at all -- fall back to a content hash rather than
            # dropping the posting outright.
            listing_id = hashed_id(ATS, token, title, url)
        else:
            continue

        categories = posting.get("categories")
        if not isinstance(categories, dict):
            categories = {}

        locations = clean_locations(
            categories.get("allLocations"), categories.get("location")
        )
        if posting.get("workplaceType") == "remote" and "remote" not in (
            loc.lower() for loc in locations
        ):
            locations = (*locations, "Remote")

        out.append(
            Listing(
                id=listing_id,
                company=company,
                title=title,
                url=url,
                locations=locations,
                # `createdAt` is epoch milliseconds; parse_ts divides down to
                # seconds automatically, but skipping this comment is exactly
                # how a board ends up 1000x in the future.
                posted_at=parse_ts(posting.get("createdAt")),
                source_ats=ATS,
                board_token=token,
                employment_type=categories.get("commitment"),
                raw_category=categories.get("team"),
            )
        )
    return out


def board_company_name(payload: Any) -> str | None:
    """Lever postings carry no company name anywhere in the payload.

    Unlike Greenhouse, there's nothing here for the finder to cross-check a
    candidate token against -- it must fall back to matching the token
    against the company's known URL slug (jobs.lever.co/<token>) instead.
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
        log.warning("lever:%s failed: %s", token, exc)
        return BoardResult(
            company, ATS, token, [], ok=False, status=exc.status, error=str(exc)
        )
