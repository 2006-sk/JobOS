"""Title-based relevance classification. No LLM, no network, fully deterministic.

Everything here is driven by `config/profile.yaml` -- retune the filter by editing
that file, not this one.

The ordering of the checks below is load-bearing and was derived from ~33k real
titles in the aggregator corpus. Three findings drove the design, each of which
silently breaks a naive implementation:

1. `intern` is a substring of "International" and "Internal". Matching without
   word boundaries classifies "Senior Product Manager, International Fulfillment"
   as an internship. Every pattern here is anchored with \\b.
2. "Member of Technical Staff" is a standard IC title, not a senior one. A blanket
   `staff` exclusion drops "Member of Technical Staff New Grad", which is exactly
   the kind of role we want.
3. "Rising Senior" describes a student. So does "Senior Student Associate".

The resolution for 2 and 3 is the override rule: an explicit early-career marker
beats a soft seniority word. Only genuinely-managerial titles (manager, director,
VP) are unconditional drops, because "Intern Manager" is still not a job for a
student.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "profile.yaml"

LEVEL_NEW_GRAD = "new-grad"
LEVEL_INTERNSHIP = "internship"
LEVEL_NEITHER = "neither"

FIELD_CORE = "core"
FIELD_ADJACENT = "adjacent"
FIELD_STRETCH = "stretch"
FIELD_NONE = "none"


def _rx(*alternatives: str) -> re.Pattern[str]:
    return re.compile("|".join(alternatives), re.IGNORECASE)


# --- level detection -------------------------------------------------------

INTERNSHIP_RX = _rx(
    r"\bintern(ship)?s?\b",  # \b is what stops "International" matching
    r"\bco-?op\b",
    r"\bapprentice(ship)?\b",
    r"\bsummer\s+20\d\d\b",
    r"\bfall\s+20\d\d\s+(intern|student)",
    r"\bindustrial\s+trainee\b",
    r"\bplacement\s+(student|year)\b",
    r"\bstudent\s+(worker|associate|researcher|assistant|position|role)\b",
    r"\bundergraduate\s+research\b",
)

NEW_GRAD_RX = _rx(
    r"\bnew\s+(college\s+)?grad(uate)?s?\b",
    r"\bncg\b",
    r"\bcollege\s+grad(uate)?\b",
    r"\bearly\s+career\b",
    r"\bearly[-\s]talent\b",
    r"\bentry[-\s]level\b",
    r"\buniversity\s+(grad(uate)?|hire|recruit|program)\b",
    r"\bcampus\s+(hire|recruit)\b",
    r"\bgraduate\s+(program|engineer|scheme|role|analyst|developer)\b",
    r"\brotation(al)?\s+(program|engineer)\b",
    r"\bleadership\s+development\s+program\b",
    r"\bclass\s+of\s+20\d\d\b",
    r"\bgrad(uate)?\s+20\d\d\b",
    r"\b20\d\d\s+grad(uate)?s?\b",
)

# Managerial titles are dropped even when an early-career marker is present --
# "University Recruiting Manager" is a recruiter's job, not a student's.
HARD_SENIORITY_RX = _rx(
    r"\bmanagers?\b",
    r"\bdirectors?\b",
    r"\bhead\s+of\b",
    r"\bvp\b",
    r"\bvice\s+president\b",
    r"\bchief\b",
    r"\bpresident\b",
    r"\bexecutive\s+(director|officer)\b",
)

# Soft seniority: real signals of a senior role, but an explicit early-career
# marker overrides them. "Principal Packaging Integration Engineer New Grad" and
# "[2026] Senior ML Engineer - PhD Early Career" are both real, both wanted.
SOFT_SENIORITY_RX = _rx(
    r"\bsenior\b",
    r"\bsr\.?\b",
    r"\bstaff\b",
    r"\bprincipal\b",
    r"\blead\b",  # \b keeps "Leadership Development Program" safe
    r"\barchitect\b",
    r"\bdistinguished\b",
    r"\bfellow\b",
    r"\bexpert\b",
    r"\b(?:ii|iii|iv|v)\b",
    r"\blevel\s*[3-9]\b",
    r"\bL[4-9]\b",
)

# Phrases that contain a seniority word but describe someone junior. Neutralized
# before seniority is evaluated so they cannot trip the soft check at all.
FALSE_SENIORITY_RX = _rx(
    r"\brising\s+(senior|junior)\b",
    r"\bmembers?\s+of\s+(the\s+)?technical\s+staff\b",
    r"\bmts\b",
    r"\bsenior\s+student\b",
    r"\bstudent\s+associate\b",
)

# --- function exclusion ----------------------------------------------------

# Non-engineering functions. Note "Sales Engineer" and "Marketing Analyst" both
# contain engineering-ish words, so this runs regardless of field match.
NON_ENGINEERING_RX = _rx(
    r"\bsales\b",
    r"\bmarketing\b",
    r"\brecruit(ing|er|ment)\b",
    r"\btalent\s+acquisition\b",
    r"\bhuman\s+resources\b",
    r"\bpeople\s+ops\b",
    r"\baccount\s+(executive|manager)\b",
    r"\bcustomer\s+(success|service|support)\b",
    r"\bbusiness\s+(analyst|development)\b",
    r"\bfinancial\s+analyst\b",
    r"\baccounting\b",
    r"\bauditor?\b",
    r"\blegal\b",
    r"\bcounsel\b",
    r"\bparalegal\b",
    r"\battorney\b",
    r"\bnurse\b",
    r"\bteacher\b",
    r"\btutor\b",
    r"\binstructor\b",
    r"\btherapist\b",
    r"\bbarista\b",
    r"\bdriver\b",
    r"\btechnician\b",
    r"\bwelder\b",
    r"\bmachinist\b",
    r"\bjanitor\b",
    r"\bcashier\b",
    r"\bsupply\s+chain\b",
    r"\bprocurement\b",
    r"\blogistics\b",
    r"\bwarehouse\b",
    r"\bcommunications\s+(specialist|associate)\b",
    r"\bpublic\s+relations\b",
)

# --- field keyword table ---------------------------------------------------

# The profile lists categories; this maps the far messier vocabulary that real
# titles actually use onto them. Keys are regex fragments matched with \b.
FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    FIELD_CORE: (
        r"software\s+(engineer|developer|development)",
        r"\bswe\b",
        r"\bsde\b",
        r"\bsdet\b",
        r"software\s+dev",
        r"\bprogrammer\b",
        r"back[-\s]?end",
        r"front[-\s]?end",
        r"full[-\s]?stack",
        r"web\s+develop",
        r"mobile\s+(engineer|developer)",
        r"\bios\s+(engineer|developer)",
        r"\bandroid\s+(engineer|developer)",
        r"distributed\s+systems?",
        r"infrastructure",
        r"platform\s+engineer",
        r"\bsystems?\s+(engineer|software|programmer)",
        r"embedded",
        r"security\s+engineer",
        r"application\s+(engineer|developer)",
        r"\bapi\s+engineer",
        r"cloud\s+engineer",
        r"compiler",
        r"operating\s+system",
        r"\bkernel\b",
        r"database\s+engineer",
        r"\bsoftware\b.*\bengineer",
    ),
    FIELD_ADJACENT: (
        r"machine\s+learning",
        r"\bml\b",
        r"\bmle\b",
        r"deep\s+learning",
        r"\bai\b",
        r"artificial\s+intelligence",
        r"data\s+engineer",
        r"data\s+scien(ce|tist)",
        r"research\s+engineer",
        r"research\s+scientist",
        r"computer\s+vision",
        r"\bnlp\b",
        r"natural\s+language",
        r"quantitative\s+(developer|researcher|trader|analyst)",
        r"\bquant\b",
        r"robotics?",
        r"autonomy",
        r"perception\s+engineer",
        r"\bllm\b",
        r"generative\s+ai",
        r"applied\s+scien(ce|tist)",
        r"algorithm\s+engineer",
    ),
    FIELD_STRETCH: (
        r"\bhardware\b",
        r"\bfirmware\b",
        r"\basic\b",
        r"\bfpga\b",
        r"silicon",
        r"\bdevops\b",
        r"\bsre\b",
        r"site\s+reliability",
        r"\bvlsi\b",
        r"physical\s+design",
        r"design\s+verification",
        r"verification\s+engineer",
        r"analog\s+(design|engineer)",
        r"\brtl\b",
        r"circuit\s+design",
        r"signal\s+integrity",
        r"\bpcb\b",
        r"electrical\s+engineer",
        r"mechanical\s+engineer",
        r"\bqa\b",
        r"quality\s+assurance",
        r"test\s+engineer",
        r"validation\s+engineer",
    ),
}

_FIELD_RX: dict[str, re.Pattern[str]] = {
    name: _rx(*(rf"\b(?:{k})" for k in pats)) for name, pats in FIELD_KEYWORDS.items()
}

# --- sponsorship / clearance ----------------------------------------------

CITIZENSHIP_RX = _rx(
    r"\bu\.?s\.?\s+citizen",
    r"\bcitizenship\s+(is\s+)?required",
    r"\bsecurity\s+clearance\b",
    r"\bactive\s+clearance\b",
    r"\bts/sci\b",
    r"\bpolygraph\b",
    r"\bitar\b",
    r"\bsecret\s+clearance\b",
    r"\bmust\s+be\s+a\s+us\s+person\b",
)

NO_SPONSORSHIP_RX = _rx(
    r"does\s+not\s+offer\s+sponsorship",
    r"no\s+sponsorship",
    r"not\s+provide\s+sponsorship",
    r"sponsorship\s+(is\s+)?not\s+available",
)


@dataclass(frozen=True)
class Verdict:
    """The full relevance decision for one listing."""

    level: str
    field: str
    relevant: bool
    channel: str  # "instant" | "digest" | "none"
    citizenship_required: bool = False
    excluded_reason: str | None = None
    location_rank: int = 99
    reasons: tuple[str, ...] = dc_field(default_factory=tuple)

    @property
    def is_early_career(self) -> bool:
        return self.level in (LEVEL_NEW_GRAD, LEVEL_INTERNSHIP)


@lru_cache(maxsize=1)
def load_profile(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else CONFIG_PATH
    with p.open() as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{p} did not parse to a mapping")
    return data


def _normalize(title: str) -> str:
    """Lowercase, unify dash variants, strip bracketed year prefixes.

    Real titles arrive as `[2026] Senior ML Engineer, X - PhD Early Career` and
    `Software Development Engineer – Early Career` (en-dash). Both need to reduce
    to something the patterns can see.
    """
    t = title.lower()
    t = t.replace("–", "-").replace("—", "-").replace("’", "'")
    t = re.sub(r"[\[\(\{]\s*20\d\d\s*[\]\)\}]", " ", t)  # leading [2026] tags
    t = re.sub(r"[/,|]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def classify_level(title: str) -> str:
    """new-grad / internship / neither.

    Internship wins over new-grad when both match, because a title carrying both
    ("New Grad & Intern Program") is an internship posting in practice.
    """
    t = _normalize(title)

    if HARD_SENIORITY_RX.search(t):
        return LEVEL_NEITHER

    if INTERNSHIP_RX.search(t):
        return LEVEL_INTERNSHIP
    if NEW_GRAD_RX.search(t):
        return LEVEL_NEW_GRAD

    # No explicit early-career marker. We deliberately do NOT try to infer that a
    # bare "Software Engineer" is entry-level -- requiring an explicit marker is
    # what keeps this at single-digit pings per day instead of fifty.
    return LEVEL_NEITHER


def overridden_seniority(title: str) -> str | None:
    """The soft seniority word an early-career marker beat, if any.

    Not used for filtering -- it exists so a surprising ping ("why did a *Senior*
    role notify me?") is explainable from the stored verdict rather than needing
    a rerun. `FALSE_SENIORITY_RX` is stripped first so "Member of Technical Staff"
    and "Rising Senior" never show up here.
    """
    t = FALSE_SENIORITY_RX.sub(" ", _normalize(title))
    m = SOFT_SENIORITY_RX.search(t)
    return m.group(0) if m else None


def classify_field(title: str) -> str:
    """core / adjacent / stretch / none, most-specific wins."""
    t = _normalize(title)
    for name in (FIELD_CORE, FIELD_ADJACENT, FIELD_STRETCH):
        if _FIELD_RX[name].search(t):
            return name
    return FIELD_NONE


def is_non_engineering(title: str) -> bool:
    return bool(NON_ENGINEERING_RX.search(_normalize(title)))


def requires_citizenship(*texts: str | None) -> bool:
    for text in texts:
        if text and CITIZENSHIP_RX.search(text):
            return True
    return False


def location_rank(locations: Iterable[str], preferred: list[str]) -> int:
    """Index of the best-matching preferred location; 99 if none match.

    Ranking only -- `acceptable_anywhere` means location never removes a listing.
    """
    joined = " | ".join(locations).lower()
    if not joined:
        return 98
    for i, pref in enumerate(preferred):
        needle = pref.lower()
        if needle in joined:
            return i
        if needle == "remote us" and "remote" in joined:
            return i
        if needle == "bay area" and any(
            c in joined for c in ("san jose", "san francisco", "palo alto", "sunnyvale",
                                  "mountain view", "santa clara", "menlo park", "cupertino")
        ):
            return i
    return 99


def classify(
    title: str,
    *,
    locations: Iterable[str] = (),
    sponsorship: str | None = None,
    profile: dict[str, Any] | None = None,
) -> Verdict:
    """Full decision for one listing. This is the only entry point callers need."""
    prof = profile if profile is not None else load_profile()
    locs = tuple(locations)
    reasons: list[str] = []

    level = classify_level(title)
    fld = classify_field(title)
    loc_rank = location_rank(locs, prof.get("locations", {}).get("preferred", []))

    cit = requires_citizenship(title, sponsorship)
    no_sponsor = bool(sponsorship and NO_SPONSORSHIP_RX.search(sponsorship))

    def reject(reason: str) -> Verdict:
        return Verdict(
            level=level,
            field=fld,
            relevant=False,
            channel="none",
            citizenship_required=cit,
            excluded_reason=reason,
            location_rank=loc_rank,
            reasons=tuple(reasons),
        )

    if level == LEVEL_NEITHER:
        return reject("not early-career")
    if is_non_engineering(title):
        return reject("non-engineering function")
    if fld == FIELD_NONE:
        return reject("field not relevant")

    sponsor_cfg = prof.get("sponsorship", {}) or {}
    if sponsor_cfg.get("needs_sponsorship"):
        if cit and sponsor_cfg.get("drop_citizenship_required", True):
            # Surfaced in the digest rather than dropped -- see profile.yaml.
            return Verdict(
                level=level,
                field=fld,
                relevant=False,
                channel="digest",
                citizenship_required=True,
                excluded_reason="citizenship or clearance required",
                location_rank=loc_rank,
                reasons=tuple(reasons),
            )
        if no_sponsor:
            return reject("employer does not sponsor")

    notif = prof.get("notifications", {}) or {}
    instant = set(notif.get("instant_tiers", [FIELD_CORE, FIELD_ADJACENT]))
    channel = "instant" if fld in instant else "digest"
    reasons.append(f"{level}/{fld}")
    if (beaten := overridden_seniority(title)) is not None:
        reasons.append(f"early-career marker overrode {beaten!r}")

    return Verdict(
        level=level,
        field=fld,
        relevant=True,
        channel=channel,
        citizenship_required=cit,
        excluded_reason=None,
        location_rank=loc_rank,
        reasons=tuple(reasons),
    )
