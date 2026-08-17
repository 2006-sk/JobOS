"""Shared HTTP client. Every outbound request in JobOS goes through here.

Centralized so the politeness rules -- descriptive User-Agent, 20s timeout,
bounded retries, `Retry-After` compliance -- hold for every adapter rather than
being reimplemented (and forgotten) five times. We are an unauthenticated guest
on other people's job boards; behaving well is what keeps these endpoints open.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/2006-sk/JobOS"
USER_AGENT = f"JobOS-JobMonitor/1.0 (+{REPO_URL}; personal job-posting monitor)"

TIMEOUT = 20
MAX_ATTEMPTS = 3
BACKOFF_BASE = 1.5
# A 429 asking us to wait longer than this means the board is telling us to go
# away for the cycle. Better to skip it than to hold the workflow hostage.
MAX_RETRY_AFTER = 60


class FetchError(Exception):
    """Raised when a board cannot be read after all retries.

    Carries `status` so callers can distinguish "this token is wrong" (404) from
    "the board is having a bad day" (503) -- the finder needs that distinction
    to decide whether a candidate token is real.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _sleep_for(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            wait = float(retry_after)
            if wait <= MAX_RETRY_AFTER:
                return wait
            raise FetchError(f"Retry-After {wait}s exceeds budget", status=429)
        except ValueError:
            pass  # HTTP-date form; fall through to plain backoff
    # Jitter keeps 8 concurrent workers from retrying in lockstep.
    return BACKOFF_BASE**attempt + random.uniform(0, 0.4)


def request_json(
    url: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
    timeout: int = TIMEOUT,
) -> Any:
    """Fetch and decode JSON, retrying transient failures.

    Retries 429 and 5xx. Does NOT retry 4xx -- a 404 is a wrong token, and
    hammering it three times just wastes the board's capacity and our runtime.
    """
    sess = session or requests
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = sess.request(
                method, url, json=json_body, headers=hdrs, timeout=timeout
            )
        except requests.RequestException as exc:
            last = exc
            if attempt == MAX_ATTEMPTS - 1:
                break
            time.sleep(_sleep_for(attempt, None))
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            last = FetchError(f"HTTP {resp.status_code} from {url}", resp.status_code)
            if attempt == MAX_ATTEMPTS - 1:
                break
            time.sleep(_sleep_for(attempt, resp.headers.get("Retry-After")))
            continue

        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} from {url}", resp.status_code)

        try:
            return resp.json()
        except ValueError as exc:
            # A board serving an HTML error page with a 200 is not hypothetical
            # (some Workday tenants do exactly this behind a WAF).
            raise FetchError(f"non-JSON response from {url}: {exc}", 200) from exc

    status = getattr(last, "status", None)
    raise FetchError(f"giving up on {url} after {MAX_ATTEMPTS} attempts: {last}", status)


def request_json_cached(
    url: str, etag: str | None = None, *, session: requests.Session | None = None
) -> tuple[Any | None, str | None]:
    """Conditional GET. Returns (payload, etag); payload is None on 304.

    Exists for the aggregator feeds specifically: they total ~23MB and we poll
    every 30 minutes. Without `If-None-Match` that is roughly a gigabyte a day
    pulled from raw.githubusercontent for data that changes a few times daily.
    """
    sess = session or requests
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if etag:
        hdrs["If-None-Match"] = etag

    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = sess.get(url, headers=hdrs, timeout=60)
        except requests.RequestException as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise FetchError(f"{url}: {exc}") from exc
            time.sleep(_sleep_for(attempt, None))
            continue

        if resp.status_code == 304:
            return None, etag
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_ATTEMPTS - 1:
                raise FetchError(f"HTTP {resp.status_code}", resp.status_code)
            time.sleep(_sleep_for(attempt, resp.headers.get("Retry-After")))
            continue
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} from {url}", resp.status_code)
        try:
            return resp.json(), resp.headers.get("ETag")
        except ValueError as exc:
            raise FetchError(f"non-JSON from {url}") from exc

    raise FetchError(f"giving up on {url}")
