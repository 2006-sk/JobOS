"""Tests for the seen-ID state machine -- the anti-duplicate-ping layer.

This is the suite the repo owner said he cares about most, so it is worth
restating the rule under test: **notify first, mark seen only after delivery
succeeds.** state.py's own docstring calls a violation of that rule "the
failure mode this project exists to avoid" -- a job silently missed forever
because it got recorded as seen before the notification actually landed.

Approach for (a)-(e): every one of them drives the real `joboS.poll.run_poll`
through a `poll_env` fixture that monkeypatches only the I/O boundaries --
`fetch_all`, `load_boards`, `build_notifier`, and the on-disk paths
(`state.DEFAULT_PATH`, `poll.DATA_DIR`, `poll.PENDING_DIGEST`,
`poll.LISTINGS_SNAPSHOT`) -- all redirected into `tmp_path`. Nothing else is
mocked: `SeenStore`, `classify()`, and `format_instant()` all run for real.
That means (e) (NOTIFY-BEFORE-SEEN) is a genuine behavioral proof that
`run_poll` implements the ordering rule, not an inspection of its source.

`data/seen.json` in the real repo is never opened by this file.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from joboS import poll, state
from joboS.fetch import _dedupe
from joboS.models import Listing, now_ts
from joboS.state import SeenStore

SEED_ID = "greenhouse:seed:0"


def _listing(
    n: int,
    *,
    title: str = "Software Engineer New Grad, Backend",
    company: str = "Acme",
    source_ats: str = "greenhouse",
    board_token: str = "acme",
    id_: str | None = None,
    url: str | None = None,
    locations: tuple[str, ...] = ("San Jose, CA",),
) -> Listing:
    """A core/new-grad listing by default -- one that WOULD ping instantly,
    so tests that expect zero notifications (bootstrap, unchanged input) are
    proving silence despite relevance, not silence because nothing matched.
    """
    return Listing(
        id=id_ or f"{source_ats}:{board_token}:{n}",
        company=company,
        title=title,
        url=url or f"https://boards.acme.com/jobs/{n}",
        locations=locations,
        posted_at=now_ts(),
        source_ats=source_ats,
        board_token=board_token,
    )


def _seed(seen_path: Path) -> None:
    """Pre-populate the store with one throwaway id.

    Without this, the store's `was_empty` flag is True on the first load and
    `run_poll` auto-bootstraps (see `should_bootstrap`) -- which sends zero
    notifications by design. Tests (b)/(d)/(e) need a genuine non-bootstrap
    run, so they must start from a non-empty store, written with the real
    `SeenStore.save()` rather than hand-rolled JSON.
    """
    SeenStore(seen_path, {SEED_ID: now_ts() - 1000}).save()


class FakeNotifier:
    """Records every send; returns a scripted sequence of success/failure."""

    name = "fake"

    def __init__(self, results: Sequence[bool] = (True,)) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, str]] = []

    def send(
        self, title: str, body: str, *, priority: str = "default",
        tags: Sequence[str] = (),
    ) -> bool:
        self.calls.append((title, body))
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class PollEnv:
    """Wires run_poll to tmp_path only; nothing it touches is the real repo."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self.seen_path = tmp_path / "seen.json"
        self.data_dir = tmp_path / "data"
        self.notifier: FakeNotifier = FakeNotifier()

        monkeypatch.setattr(state, "DEFAULT_PATH", self.seen_path)
        monkeypatch.setattr(poll, "DATA_DIR", self.data_dir)
        pending = self.data_dir / "pending_digest.json"
        monkeypatch.setattr(poll, "PENDING_DIGEST", pending)
        monkeypatch.setattr(poll, "LISTINGS_SNAPSHOT", self.data_dir / "listings.json")
        monkeypatch.setattr(poll, "load_boards", lambda *a, **k: [])

    def run(self, listings: Sequence[Listing], **kwargs: object) -> int:
        def _fake_fetch_all(boards: object, include_aggregators: bool = True) -> object:
            return (list(listings), [])

        self._monkeypatch.setattr(poll, "fetch_all", _fake_fetch_all)
        self._monkeypatch.setattr(
            poll, "build_notifier", lambda dry_run=False: self.notifier
        )
        return poll.run_poll(**kwargs)  # type: ignore[arg-type]

    def store(self) -> SeenStore:
        return SeenStore.load(self.seen_path)


@pytest.fixture
def poll_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PollEnv:
    return PollEnv(monkeypatch, tmp_path)


# --- (a) bootstrap sends zero notifications ---------------------------------


def test_bootstrap_sends_zero_notifications_and_records_all(
    poll_env: PollEnv,
) -> None:
    listings = [_listing(i) for i in range(50)]

    rc = poll_env.run(listings, bootstrap=True)

    assert rc == 0
    assert poll_env.notifier.calls == []  # zero sends, even though every
    # listing here is a core/new-grad title that would otherwise ping instant

    store = poll_env.store()
    assert len(store) == 50
    assert all(listing.id in store for listing in listings)


def test_bootstrap_also_triggers_automatically_on_an_empty_store(
    poll_env: PollEnv,
) -> None:
    # No --bootstrap flag at all: should_bootstrap() must still catch this
    # via was_empty, or a fresh clone's first run fires one ping per listing.
    listings = [_listing(i) for i in range(10)]

    rc = poll_env.run(listings, bootstrap=False)

    assert rc == 0
    assert poll_env.notifier.calls == []
    assert len(poll_env.store()) == 10


# --- (b) a job seen in run 1 does not renotify in run 2 ---------------------


def test_seen_job_does_not_renotify_in_run_two(poll_env: PollEnv) -> None:
    _seed(poll_env.seen_path)
    listings = [_listing(1)]

    rc1 = poll_env.run(listings, bootstrap=False)
    rc2 = poll_env.run(listings, bootstrap=False)

    assert rc1 == 0
    assert rc2 == 0
    assert len(poll_env.notifier.calls) == 1  # exactly one notification, total


# --- (c) the same job from two sources notifies once ------------------------


def test_same_job_two_sources_dedupes_and_notifies_once(poll_env: PollEnv) -> None:
    # Same (company, title, url); different ids, as a direct greenhouse board
    # and the aggregator feed would independently mint for the same posting.
    direct = _listing(
        1, source_ats="greenhouse", board_token="acme", id_="greenhouse:acme:42",
        url="https://acme.com/jobs/42",
    )
    aggregator = _listing(
        1, source_ats="aggregator", board_token="aggregator",
        id_="aggregator:simplify:xyz789", url="https://acme.com/jobs/42",
    )
    assert direct.id != aggregator.id
    assert (direct.company, direct.title, direct.url) == (
        aggregator.company, aggregator.title, aggregator.url,
    )

    deduped = _dedupe([direct, aggregator])
    assert len(deduped) == 1  # collapsed at the batch level, before poll ever sees it

    _seed(poll_env.seen_path)
    rc = poll_env.run(deduped, bootstrap=False)

    assert rc == 0
    assert len(poll_env.notifier.calls) == 1
    _, body = poll_env.notifier.calls[0]
    assert body.count("acme.com/jobs/42") == 1  # the URL appears once, not twice

    recorded = set(poll_env.store().entries) - {SEED_ID}
    assert len(recorded) == 1


# --- (d) unchanged input produces no commit and no notification -------------


def test_unchanged_input_is_byte_identical_and_stays_silent(poll_env: PollEnv) -> None:
    _seed(poll_env.seen_path)
    listings = [_listing(1)]

    rc1 = poll_env.run(listings, bootstrap=False)
    assert rc1 == 0
    assert len(poll_env.notifier.calls) == 1
    bytes_after_run1 = poll_env.seen_path.read_bytes()

    rc2 = poll_env.run(listings, bootstrap=False)
    assert rc2 == 0
    assert len(poll_env.notifier.calls) == 1  # no second send for the same job
    bytes_after_run2 = poll_env.seen_path.read_bytes()

    # This is what stops the workflow committing 48 times a day for nothing:
    # a rerun with nothing new must rewrite the exact same bytes.
    assert bytes_after_run2 == bytes_after_run1


# --- (e) notify-before-seen: THE critical ordering test ---------------------


def test_notify_before_seen_failed_delivery_is_not_recorded_and_retries(
    poll_env: PollEnv,
) -> None:
    """A regression here means silently missed jobs.

    Run 1: the notifier fails (ntfy is down / rate-limited). The job's id
    must NOT land in the seen store. Run 2, same input, notifier now
    succeeds: the job must be retried -- not skipped as "already seen".
    """
    _seed(poll_env.seen_path)
    listings = [_listing(1)]

    failing_notifier = FakeNotifier(results=[False])
    poll_env.notifier = failing_notifier
    rc1 = poll_env.run(listings, bootstrap=False)

    assert rc1 == 1  # run_poll signals the failure
    assert len(failing_notifier.calls) == 1

    store_after_failure = poll_env.store()
    assert listings[0].id not in store_after_failure
    assert set(store_after_failure.entries) == {SEED_ID}  # nothing new recorded

    succeeding_notifier = FakeNotifier(results=[True])
    poll_env.notifier = succeeding_notifier
    rc2 = poll_env.run(listings, bootstrap=False)

    assert rc2 == 0
    assert len(succeeding_notifier.calls) == 1  # it WAS retried, not skipped
    # Both attempts notified about the exact same job set, proving this is a
    # retry of the dropped job and not some unrelated notification.
    assert failing_notifier.calls[0] == succeeding_notifier.calls[0]

    store_after_retry = poll_env.store()
    assert listings[0].id in store_after_retry


# --- (f) prune drops entries older than 180 days -----------------------------


def test_prune_drops_entries_older_than_180_days_keeps_newer(tmp_path: Path) -> None:
    now = 1_800_000_000
    store = SeenStore(
        tmp_path / "seen.json",
        {
            "old:1": now - 181 * 86_400,  # just past the cutoff -- dropped
            "new:1": now - 179 * 86_400,  # just inside it -- kept
        },
    )

    dropped = store.prune(now=now)

    assert dropped == 1
    assert "old:1" not in store.entries
    assert "new:1" in store.entries


# --- (g) a corrupt seen.json must fail loudly, not start empty --------------


def test_corrupt_seen_json_raises_runtime_error_not_silently_empty(
    tmp_path: Path,
) -> None:
    p = tmp_path / "seen.json"
    p.write_text("{not valid json!!!")

    with pytest.raises(RuntimeError):
        SeenStore.load(p)


def test_non_dict_json_top_level_raises_rather_than_bootstrapping(
    tmp_path: Path,
) -> None:
    """A seen.json clobbered into a JSON array must fail loudly.

    `[1, 2, 3]` is syntactically valid JSON, so `json.loads` succeeds and the
    corrupt-file except clause never fires. Treating that as an empty store
    would set `was_empty=True` and silently AUTO-BOOTSTRAP: the real history is
    discarded and every currently-open job is marked already-seen, so none of
    them ever notify. That is a silent miss wearing the costume of a clean run.
    """
    p = tmp_path / "seen.json"
    p.write_text("[1, 2, 3]")

    with pytest.raises(RuntimeError, match="not an object"):
        SeenStore.load(p)


def test_entries_key_holding_non_dict_raises_runtimeerror(
    tmp_path: Path,
) -> None:
    """A non-object `entries` is corruption, and must surface as RuntimeError.

    `{"entries": "garbage"}` is valid JSON and a dict at the top level, so it
    clears the json.loads guard. Callers are promised RuntimeError for an
    unreadable store; an AttributeError escaping from `.items()` would be an
    unhandled crash rather than the documented, actionable failure.
    """
    p = tmp_path / "seen.json"
    p.write_text(json.dumps({"entries": "garbage"}))

    with pytest.raises(RuntimeError, match="non-object 'entries'"):
        SeenStore.load(p)


# --- (h) save() is stable and sorted -----------------------------------------


def test_save_output_is_stable_and_sorted(tmp_path: Path) -> None:
    p = tmp_path / "seen.json"
    store = SeenStore(p, {"zeta:1": 100, "alpha:1": 200, "mid:1": 150})

    store.save()
    bytes_first = p.read_bytes()
    store.save()
    bytes_second = p.read_bytes()

    assert bytes_first == bytes_second

    payload = json.loads(bytes_first)
    keys = list(payload["entries"].keys())
    assert keys == sorted(keys)
