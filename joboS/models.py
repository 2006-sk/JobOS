"""The normalized listing contract every adapter must emit.

An adapter's only job is to turn one board's payload into `Listing` objects.
Everything downstream -- relevance, state, notify -- consumes only this shape,
so an adapter that gets `id` or `posted_at` wrong breaks the whole pipeline in
ways that are invisible until you get duplicate pings at 3am.

Two rules that matter more than the rest:

1. `id` MUST be stable across runs. It is the only thing standing between you
   and re-notifying the same job every 30 minutes forever. Prefer the ATS's own
   job id via `make_id()`. Only fall back to `hashed_id()` when a board gives
   you nothing usable.
2. `posted_at` is UTC epoch seconds or None -- never a naive datetime, never a
   local timestamp, never a string. `parse_ts()` is the only sanctioned parser.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# Every ATS an adapter may claim. Keeping this closed catches typos like
# "smartrecruiter" that would otherwise silently create a second id namespace
# for the same board -- which reads as a whole new set of jobs, i.e. re-pings.
KNOWN_ATS = frozenset(
    {"greenhouse", "lever", "ashby", "smartrecruiters", "workday", "aggregator"}
)


@dataclass(frozen=True, slots=True)
class Listing:
    """One job posting, normalized across every source."""

    id: str
    company: str
    title: str
    url: str
    locations: tuple[str, ...]
    posted_at: int | None
    source_ats: str
    board_token: str
    employment_type: str | None = None
    raw_category: str | None = None
    first_seen_at: int | None = None
    # Greenhouse reports `updated_at` alongside `first_published`; a job edited
    # today may have been posted months ago. We keep both and let state.py treat
    # first-time-seen as the trigger, per spec, rather than trusting either.
    updated_at: int | None = None
    # Aggregator feeds carry an explicit sponsorship string. Direct boards don't,
    # so this is None for most listings and relevance.py must tolerate that.
    sponsorship: str | None = None

    def __post_init__(self) -> None:
        if self.source_ats not in KNOWN_ATS:
            raise ValueError(
                f"unknown source_ats {self.source_ats!r}; add it to KNOWN_ATS"
            )
        if not self.id:
            raise ValueError("listing id must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["locations"] = list(self.locations)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Listing:
        d = dict(d)
        d["locations"] = tuple(d.get("locations") or ())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class BoardResult:
    """Outcome of polling a single board.

    Adapters never raise past the fetch layer. A dead board must degrade to a
    warning, not a failed workflow -- one 404 taking down the run would mean
    missing every other company's postings that cycle.
    """

    company: str
    ats: str
    token: str
    listings: list[Listing] = field(default_factory=list)
    ok: bool = True
    status: int | None = None
    error: str | None = None

    @property
    def count(self) -> int:
        return len(self.listings)


def make_id(ats: str, token: str, native_id: str | int) -> str:
    """Build a stable id from the ATS's own job id: `greenhouse:stripe:4625767006`."""
    return f"{ats}:{token}:{native_id}"


def hashed_id(ats: str, token: str, title: str, url: str) -> str:
    """Fallback id for boards exposing no usable job id.

    Hashes (token, title, url) -- deliberately NOT the location or timestamp,
    which churn between runs and would mint a "new" job each poll.
    """
    digest = hashlib.sha256(
        "\x1f".join((token, title.strip().lower(), url.strip())).encode()
    ).hexdigest()[:16]
    return f"{ats}:{token}:h{digest}"


_EPOCH_MS_CUTOFF = 10_000_000_000  # anything larger is milliseconds, not seconds


def parse_ts(value: Any) -> int | None:
    """Coerce any ATS timestamp into UTC epoch seconds.

    Handles ISO-8601 with `Z` or numeric offsets, bare dates, and epoch numbers
    in seconds or milliseconds. Returns None rather than raising -- a missing
    date is normal (Lever omits it on some posts) and must not kill a board.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > _EPOCH_MS_CUTOFF:
            ts /= 1000.0
        return int(ts)
    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None
    if raw.isdigit():
        return parse_ts(int(raw))

    # `2026-08-06T12:10:17-04:00` and `...Z` both need normalizing for fromisoformat
    # on older behaviour; also tolerate a space separator and trailing microseconds.
    iso = raw.replace(" ", "T", 1) if " " in raw and "T" not in raw else raw
    iso = re.sub(r"[Zz]$", "+00:00", iso)
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        # A board that omits the offset is assumed UTC. Being off by a few hours
        # is harmless -- first-time-seen is the notification trigger, not this.
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp())


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def clean_locations(*values: Any) -> tuple[str, ...]:
    """Flatten whatever shape a board calls a location into deduped strings.

    Boards variously give a string, a dict with `name`, a list of either, or a
    comma-joined blob like "SF, NYC, SEA, CHI". Order is preserved because the
    first location is usually the primary one, which ranking cares about.
    """
    out: list[str] = []
    seen: set[str] = set()

    def push(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, dict):
            for key in ("name", "location", "text", "city"):
                if v.get(key):
                    push(v[key])
                    return
            return
        if isinstance(v, (list, tuple, set)):
            for item in v:
                push(item)
            return
        text = str(v).strip()
        if not text:
            return
        for part in re.split(r"\s*(?:;|\||\bor\b)\s*", text):
            part = part.strip().strip(",").strip()
            if part and part.lower() not in seen:
                seen.add(part.lower())
                out.append(part)

    for value in values:
        push(value)
    return tuple(out)
