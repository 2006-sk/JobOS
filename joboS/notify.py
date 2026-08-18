"""Push delivery. ntfy.sh ships; the backend is pluggable.

Why ntfy: free, no account, a real phone app, and one HTTP POST from Actions.
Two things to know about it, documented rather than designed around:

  * ntfy topics are PUBLIC. Anyone who learns your topic can read it AND publish
    to it, so a guessable topic means someone else can push fake job alerts to
    your phone. Use an unguessable topic and keep it in Actions secrets only --
    never in a committed file, since this repo is public.
  * The free server is best-effort. That is precisely why `send()` reports
    success/failure honestly and state.py refuses to mark a job seen until the
    send actually succeeded -- a dropped notification must ping again next run,
    not vanish.

`send()` returning False is a real signal, not decoration. Do not "fix" a caller
by ignoring it.
"""

from __future__ import annotations

import logging
import os
import smtplib
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import requests

from .http import USER_AGENT
from .models import Listing
from .relevance import Verdict

log = logging.getLogger(__name__)

MAX_BODY_ROLES_DEFAULT = 15


@dataclass(frozen=True)
class Hit:
    """A listing that passed relevance, plus why and how urgently."""

    listing: Listing
    verdict: Verdict
    tier: str = "unranked"

    @property
    def is_tier1(self) -> bool:
        return self.tier == "1"


class Notifier(Protocol):
    name: str

    def send(self, title: str, body: str, *, priority: str = "default",
             tags: Sequence[str] = ()) -> bool: ...


class NtfyNotifier:
    """POST to ntfy.sh. The shipping default."""

    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh") -> None:
        if not topic:
            raise ValueError("NTFY_TOPIC is empty")
        self.topic = topic
        self.server = server.rstrip("/")

    def send(self, title: str, body: str, *, priority: str = "default",
             tags: Sequence[str] = ()) -> bool:
        headers = {
            "User-Agent": USER_AGENT,
            # Non-ASCII in ntfy headers is rejected; titles carry em-dashes and
            # company names with accents, so normalize before it reaches the wire.
            "Title": _ascii(title),
            "Priority": priority,
        }
        if tags:
            headers["Tags"] = ",".join(tags)
        try:
            resp = requests.post(
                f"{self.server}/{self.topic}",
                data=body.encode("utf-8"),
                headers=headers,
                timeout=20,
            )
        except requests.RequestException as exc:
            log.error("ntfy send failed: %s", exc)
            return False
        if resp.status_code >= 300:
            log.error("ntfy returned HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        return True


class DiscordNotifier:
    name = "discord"

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def send(self, title: str, body: str, *, priority: str = "default",
             tags: Sequence[str] = ()) -> bool:
        try:
            resp = requests.post(
                self.webhook_url,
                json={"content": f"**{title}**\n{body}"[:1990]},
                timeout=20,
            )
        except requests.RequestException as exc:
            log.error("discord send failed: %s", exc)
            return False
        return resp.status_code < 300


class PushoverNotifier:
    """Paid ($5 once) but has delivery receipts. The upgrade path from ntfy."""

    name = "pushover"

    def __init__(self, token: str, user: str) -> None:
        self.token, self.user = token, user

    def send(self, title: str, body: str, *, priority: str = "default",
             tags: Sequence[str] = ()) -> bool:
        try:
            resp = requests.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": self.token,
                    "user": self.user,
                    "title": title,
                    "message": body[:1024],
                    "priority": 1 if priority in ("high", "urgent") else 0,
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            log.error("pushover send failed: %s", exc)
            return False
        return resp.status_code < 300


class SMTPNotifier:
    name = "smtp"

    def __init__(self, host: str, port: int, user: str, password: str, to: str) -> None:
        self.host, self.port, self.user, self.password, self.to = (
            host, port, user, password, to
        )

    def send(self, title: str, body: str, *, priority: str = "default",
             tags: Sequence[str] = ()) -> bool:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, self.user, self.to
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as srv:
                srv.starttls()
                srv.login(self.user, self.password)
                srv.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            log.error("smtp send failed: %s", exc)
            return False
        return True


class DryRunNotifier:
    """Prints instead of sending. Backs `--dry-run`."""

    name = "dry-run"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, body: str, *, priority: str = "default",
             tags: Sequence[str] = ()) -> bool:
        self.sent.append((title, body))
        print(f"\n{'=' * 68}\n[DRY RUN] {title}   (priority={priority})\n{'=' * 68}")
        print(body)
        print("=" * 68)
        return True


class UnconfiguredNotifier:
    """Used when no channel is configured. Always FAILS, deliberately.

    The obvious alternative -- quietly falling back to dry-run -- is a trap.
    DryRunNotifier reports success, `poll.py` records the roles as seen on a
    truthy send, and they are never notified once real credentials appear. The
    monitor looks healthy, commits state every 30 minutes, and silently eats
    every matching job in the meantime.

    Returning False keeps those roles unrecorded and makes the run exit
    non-zero, so an unfinished setup is loud and self-correcting rather than an
    invisible data-loss window.
    """

    name = "unconfigured"

    def send(self, title: str, body: str, *, priority: str = "default",
             tags: Sequence[str] = ()) -> bool:
        log.error(
            "NO NOTIFICATION CHANNEL CONFIGURED -- would have sent: %r. "
            "Set the SMTP_* secrets (or NTFY_TOPIC). These roles are left "
            "unrecorded and will be retried, so nothing is lost.", title
        )
        return False


class MultiNotifier:
    """Send through every configured channel.

    Reports success if AT LEAST ONE channel delivered. That threshold is
    deliberate: `poll.py` only records a role as seen when this returns True, so
    requiring every channel to succeed would mean a single flaky one re-sends the
    same roles through the healthy channels on every run until it recovers.
    One delivered message means you have been told, which is the thing that
    matters.
    """

    def __init__(self, backends: Sequence[Notifier]) -> None:
        self.backends = list(backends)
        self.name = "+".join(b.name for b in self.backends)

    def send(self, title: str, body: str, *, priority: str = "default",
             tags: Sequence[str] = ()) -> bool:
        delivered = False
        for backend in self.backends:
            try:
                if backend.send(title, body, priority=priority, tags=tags):
                    delivered = True
                else:
                    log.error("channel %s failed to deliver", backend.name)
            except Exception:  # noqa: BLE001 -- one bad channel must not sink the rest
                log.exception("channel %s raised", backend.name)
        return delivered


def build_notifier(dry_run: bool = False) -> Notifier:
    """Build a notifier from the environment.

    Every configured channel is used, not just the first one found. Returning
    early on the first match meant that setting NTFY_TOPIC silently disabled a
    fully-configured email backend -- you would set up email, see nothing arrive,
    and have no error to explain why.
    """
    if dry_run:
        return DryRunNotifier()

    backends: list[Notifier] = []
    if topic := os.environ.get("NTFY_TOPIC"):
        backends.append(
            NtfyNotifier(topic, os.environ.get("NTFY_SERVER", "https://ntfy.sh"))
        )
    if hook := os.environ.get("DISCORD_WEBHOOK"):
        backends.append(DiscordNotifier(hook))
    if (tok := os.environ.get("PUSHOVER_TOKEN")) and (usr := os.environ.get("PUSHOVER_USER")):
        backends.append(PushoverNotifier(tok, usr))
    if all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_TO")):
        backends.append(
            SMTPNotifier(
                os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")),
                os.environ["SMTP_USER"], os.environ["SMTP_PASS"], os.environ["SMTP_TO"],
            )
        )

    if not backends:
        return UnconfiguredNotifier()
    if len(backends) == 1:
        return backends[0]
    return MultiNotifier(backends)


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def _sort_key(hit: Hit) -> tuple[int, int, str]:
    # Tier-1 first, then better location match. Level is deliberately NOT part of
    # the key: internships and new-grad roles are equal priority per profile.yaml.
    return (0 if hit.is_tier1 else 1, hit.verdict.location_rank, hit.listing.company)


def format_instant(hits: Sequence[Hit], max_roles: int = MAX_BODY_ROLES_DEFAULT) -> tuple[str, str]:
    """Build the one-per-run instant notification.

    One notification per run, never one per role -- a bulk repost of 100 jobs
    (which really happens; the corpus shows 100+ single-day spikes) must be one
    buzz, not a hundred.
    """
    ordered = sorted(hits, key=_sort_key)
    n_tier1 = sum(1 for h in ordered if h.is_tier1)
    plural = "s" if len(ordered) != 1 else ""
    title = f"{len(ordered)} new role{plural}"
    if n_tier1:
        title += f" - {n_tier1} tier-1"

    lines: list[str] = []
    for hit in ordered[:max_roles]:
        listing, verdict = hit.listing, hit.verdict
        loc = listing.locations[0] if listing.locations else "location n/a"
        marker = "*" if hit.is_tier1 else "-"
        lines.append(
            f"{marker} {listing.company} - {listing.title}\n"
            f"  ({verdict.level}, {loc})\n"
            f"  {listing.url}"
        )
    if len(ordered) > max_roles:
        lines.append(f"+{len(ordered) - max_roles} more")
    return title, "\n".join(lines)


def format_digest(
    stretch: Sequence[Hit],
    excluded: Sequence[Hit],
    health: str,
    max_roles: int = MAX_BODY_ROLES_DEFAULT,
) -> tuple[str, str]:
    """The 09:00 UTC digest: stretch-tier roles, exclusions, and run health."""
    title = f"Daily digest - {len(stretch)} stretch role{'s' if len(stretch) != 1 else ''}"
    blocks: list[str] = []

    if stretch:
        blocks.append("STRETCH ROLES")
        for hit in sorted(stretch, key=_sort_key)[:max_roles]:
            loc = hit.listing.locations[0] if hit.listing.locations else "location n/a"
            blocks.append(f"- {hit.listing.company} - {hit.listing.title}\n"
                          f"  ({hit.verdict.level}, {loc})\n  {hit.listing.url}")
        if len(stretch) > max_roles:
            blocks.append(f"+{len(stretch) - max_roles} more")

    if excluded:
        # Surfaced, never silently dropped -- a filter that hides things without
        # telling you is the same failure mode as a monitor that stopped running.
        blocks.append(f"\nEXCLUDED ({len(excluded)}) - citizenship/clearance required")
        for hit in excluded[:max_roles]:
            blocks.append(f"- {hit.listing.company} - {hit.listing.title}")

    blocks.append(f"\n{health}")
    return title, "\n".join(blocks)


def notify_failure(notifier: Notifier, error: str) -> bool:
    """Alert that the run itself broke.

    A monitor that dies quietly is worse than no monitor -- you keep trusting it
    while it reports nothing.
    """
    return notifier.send(
        "JobOS run FAILED",
        f"The poll did not complete:\n\n{error[:800]}\n\n"
        f"Check: https://github.com/2006-sk/JobOS/actions",
        priority="high",
        tags=["warning"],
    )
