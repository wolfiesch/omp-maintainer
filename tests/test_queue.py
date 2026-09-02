"""Capacity ownership and issue-key serialization in ``WorkerPool``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from omp_maintainer.config import Settings
from omp_maintainer.db import Database, EventRow
from omp_maintainer.queue import WorkerPool
from omp_maintainer.slot_pool import SlotPool


class _StubGitHub:
    """Sentinel; queue tests do not call GitHub."""


class _StubSandbox:
    """Sentinel; queue tests do not touch workspaces."""

    def reclaim_workspace_caches(self, *, repo: str, number: int | str) -> bool:
        del repo, number
        return False

    def reclaim_all_caches(self) -> int:
        return 0


class _StubGitTransport:
    """Sentinel; queue tests do not push."""


def _make_pool(settings: Settings, db: Database, slot_pool: SlotPool) -> WorkerPool:
    return WorkerPool(
        settings=settings,
        db=db,
        github=_StubGitHub(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        git_transport=_StubGitTransport(),  # type: ignore[arg-type]
        slot_pool=slot_pool,
    )


def _record_event(db: Database, delivery_id: str, issue_key: str) -> None:
    db.record_event(
        delivery_id=delivery_id,
        event_type="issues",
        repo="octo/widget",
        issue_key=issue_key,
        payload={"action": "opened", "issue": {"number": issue_key.rsplit("#", 1)[1]}},
    )


def _event_state(db: Database, delivery_id: str) -> str:
    row = db.get_event(delivery_id)
    assert row is not None
    return row.state


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_dispatcher_claims_no_more_than_available_slots(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued flood cannot exceed its slot count in running rows or tasks."""
    settings.max_concurrency = 2
    slot_pool = SlotPool([2001, 2002])
    pool = _make_pool(settings, db, slot_pool)
    delivery_ids = [f"flood-{number}" for number in range(5)]
    for number, delivery_id in enumerate(delivery_ids, start=1):
        _record_event(db, delivery_id, f"octo/widget#{number}")

    both_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    started: list[str] = []

    async def blocked_dispatch(self: WorkerPool, row: EventRow, *, slot_uid: int | None = None) -> None:
        assert slot_uid in {2001, 2002}
        started.append(row.delivery_id)
        if len(started) == settings.max_concurrency:
            both_started.set()
        await release_dispatch.wait()

    monkeypatch.setattr(WorkerPool, "_dispatch", blocked_dispatch)

    await pool.start()
    try:
        await asyncio.wait_for(both_started.wait(), timeout=1.0)
        running = [delivery_id for delivery_id in delivery_ids if _event_state(db, delivery_id) == "running"]
        assert len(started) == settings.max_concurrency
        assert len(running) == settings.max_concurrency
        assert len(pool._inflight_tasks) == settings.max_concurrency  # noqa: SLF001
        assert len(await pool.inflight_snapshot()) == settings.max_concurrency

        release_dispatch.set()
        await pool.stop(drain_timeout=1.0, kill_timeout=0.1)

        # Both completed workers returned their slots; the remaining rows are
        # still durable queued work rather than unbounded in-memory tasks.
        first_slot = await asyncio.wait_for(slot_pool.acquire(), timeout=1.0)
        second_slot = await asyncio.wait_for(slot_pool.acquire(), timeout=1.0)
        assert {first_slot, second_slot} == {2001, 2002}
        slot_pool.release(first_slot)
        slot_pool.release(second_slot)
        assert sum(_event_state(db, delivery_id) == "done" for delivery_id in delivery_ids) == 2
        assert sum(_event_state(db, delivery_id) == "queued" for delivery_id in delivery_ids) == 3
    finally:
        release_dispatch.set()
        await pool.stop(drain_timeout=1.0, kill_timeout=0.1)


@pytest.mark.asyncio
async def test_dispatcher_serializes_same_issue_before_claiming_successor(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successor for one issue stays queued until its predecessor finishes."""
    settings.max_concurrency = 2
    pool = _make_pool(settings, db, SlotPool([2001, 2002]))
    _record_event(db, "first", "octo/widget#9")
    _record_event(db, "second", "octo/widget#9")

    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    started: list[str] = []

    async def blocked_dispatch(self: WorkerPool, row: EventRow, *, slot_uid: int | None = None) -> None:
        del self, slot_uid
        started.append(row.delivery_id)
        if row.delivery_id == "first":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
            await release_second.wait()

    monkeypatch.setattr(WorkerPool, "_dispatch", blocked_dispatch)

    await pool.start()
    try:
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await asyncio.sleep(0.05)
        assert started == ["first"]
        assert _event_state(db, "first") == "running"
        assert _event_state(db, "second") == "queued"
        assert await pool.inflight_snapshot() == ["octo/widget#9"]

        release_first.set()
        await _wait_until(lambda: _event_state(db, "first") == "done")
        await _wait_until(lambda: not pool._inflight)  # noqa: SLF001
        pool.wake()
        await asyncio.wait_for(second_started.wait(), timeout=1.0)
        assert started == ["first", "second"]
    finally:
        release_first.set()
        release_second.set()
        await pool.stop(drain_timeout=1.0, kill_timeout=0.1)


@pytest.mark.asyncio
async def test_dispatcher_releases_capacity_when_no_row_is_claimed(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polling an empty queue returns the acquired slot before sleeping."""
    settings.max_concurrency = 1
    slot_pool = SlotPool([2001])
    pool = _make_pool(settings, db, slot_pool)
    claim_attempted = asyncio.Event()
    claim_next_event = db.claim_next_event

    def observe_claim() -> EventRow | None:
        claim_attempted.set()
        return claim_next_event()

    monkeypatch.setattr(db, "claim_next_event", observe_claim)

    await pool.start()
    slot_uid: int | None = None
    try:
        await asyncio.wait_for(claim_attempted.wait(), timeout=1.0)
        slot_uid = await asyncio.wait_for(slot_pool.acquire(), timeout=1.0)
        assert slot_uid == 2001
    finally:
        if slot_uid is not None:
            slot_pool.release(slot_uid)
        await pool.stop(drain_timeout=1.0, kill_timeout=0.1)
