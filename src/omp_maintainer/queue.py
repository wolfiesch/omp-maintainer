"""Async worker pool draining the durable sqlite event queue."""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from collections.abc import Callable
from contextlib import suppress

from omp_maintainer import tasks
from omp_maintainer.cancellation import clear_current_event, set_current_event
from omp_maintainer.config import Settings
from omp_maintainer.db import Database, EventRow
from omp_maintainer.github_backend import GitHubBackend
from omp_maintainer.sandbox import GitTransport, SandboxManager, _reap_slot
from omp_maintainer.slot_pool import SlotPool

log = logging.getLogger(__name__)


class WorkerPool:
    """Long-lived dispatcher: drains queued events into per-task coroutines."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        github: GitHubBackend,
        sandbox: SandboxManager,
        git_transport: GitTransport,
        slot_pool: SlotPool | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.github = github
        self.sandbox = sandbox
        self.git_transport = git_transport
        self._workers: list[asyncio.Task[None]] = []
        self._wakeup = asyncio.Event()
        self._stop = asyncio.Event()
        self._slot_pool: SlotPool | None
        self._semaphore: asyncio.Semaphore | None
        if slot_pool is not None:
            self._slot_pool = slot_pool
            self._semaphore = None
        elif os.geteuid() == 0:
            self._slot_pool = SlotPool(range(2001, 2001 + settings.max_concurrency))
            self._semaphore = None
        else:
            self._slot_pool = None
            self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._inflight: set[str] = set()
        self._inflight_lock = asyncio.Lock()
        # Cancellation: workers register a stop hook via the contextvar helpers
        # in this module; the API surface fires them on demand. Plain dict/set
        # are GIL-safe for single-key ops, which is all we do.
        self._cancel_hooks: dict[str, Callable[[], None]] = {}
        self._cancelled: set[str] = set()
        # Phase B (graceful shutdown): track each spawned `_run_event` task so
        # `stop()` can drain in-flight work, and a flag the exception path
        # checks to avoid marking shutdown-interrupted rows as `failed` (we
        # want them to stay `running` so `reset_stuck_running()` requeues
        # them on next start; the agent then resumes via `--continue`).
        self._inflight_tasks: dict[asyncio.Task[None], str] = {}
        self._shutting_down: bool = False
        # Deliveries whose `_run_event` we deliberately interrupted via
        # `stop()` (either by firing the registered cancel hook or by
        # cancelling the asyncio task itself). The exception path uses
        # this — NOT `_shutting_down` — to decide whether to suppress
        # `mark_event(..., 'failed')`. Without this distinction, an
        # unrelated dispatch failure during the drain window would be
        # silently masked and requeued as if nothing went wrong.
        self._shutdown_cancelled: set[str] = set()

    def wake(self) -> None:
        """Signal that new work is available."""
        self._wakeup.set()

    async def inflight_snapshot(self) -> list[str]:
        """Return a stable, sorted snapshot of currently in-flight issue keys."""
        async with self._inflight_lock:
            return sorted(self._inflight)

    async def _reap_all_slots(self) -> None:
        if self._slot_pool is None:
            return
        await asyncio.gather(*(asyncio.to_thread(_reap_slot, uid) for uid in self._slot_pool.slot_uids))

    async def start(self) -> None:
        await self._reap_all_slots()
        if self.settings.reclaim_workspace_caches:
            # Crash leftovers: strip dependency caches from every workspace
            # before the dispatcher can touch any of them again.
            swept = await asyncio.to_thread(self.sandbox.reclaim_all_caches)
            if swept:
                log.info("workspace cache sweep", extra={"workspaces": swept})
        recovered = self.db.reset_stuck_running()
        if recovered:
            log.info("recovered stuck events", extra={"count": recovered})
        # A single dispatcher owns capacity before claiming each row, so queued
        # work cannot inflate running rows or in-memory task count.
        self._workers.append(asyncio.create_task(self._dispatch_loop(), name="omp-maintainer-dispatch"))

    async def stop(self, *, drain_timeout: float = 25.0, kill_timeout: float = 5.0) -> None:
        """Halt the dispatcher, then drain (or kill) in-flight `_run_event` tasks.

        Cleanly interrupted tasks intentionally leave their DB row in
        `running` so the next `WorkerPool.start()` re-queues them via
        `reset_stuck_running()`. The resumed omp session then picks up via
        `--continue` from the persisted JSONL transcript.
        """
        self._shutting_down = True
        self._stop.set()
        self._wakeup.set()
        # 1. Halt the dispatcher (no new claims).
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            with suppress(asyncio.CancelledError):
                await worker
        self._workers.clear()
        # 2. Give in-flight tasks a chance to drain.
        pending = list(self._inflight_tasks)
        if not pending:
            return
        log.info("draining in-flight tasks", extra={"count": len(pending), "timeout": drain_timeout})
        _, still_running = await asyncio.wait(pending, timeout=drain_timeout)
        if not still_running:
            return
        # 3. Time's up — for every still-running task: fire its cancel hook
        #    if one was registered (kills the omp subprocess); otherwise
        #    cancel the asyncio task itself so a worker stuck pre-hook
        #    (e.g. waiting on the slot pool or inside RpcClient.__enter__)
        #    cannot proceed to spawn a fresh subprocess after stop()
        #    returns. Either way we record the delivery id in
        #    `_shutdown_cancelled` so `_run_event`'s exception path
        #    suppresses `mark_event(..., 'failed')` for that row only.
        log.warning("shutdown timeout; interrupting in-flight tasks", extra={"count": len(still_running)})
        for task in still_running:
            delivery_id = self._inflight_tasks.get(task)
            if delivery_id is None:
                # Task was already finalizing; nothing left to interrupt.
                task.cancel()
                continue
            self._shutdown_cancelled.add(delivery_id)
            hook = self._cancel_hooks.pop(delivery_id, None)
            if hook is not None:
                try:
                    await asyncio.to_thread(hook)
                except Exception:
                    log.exception("shutdown hook raised", extra={"delivery": delivery_id})
                continue
            # No hook armed yet — the worker hasn't reached the omp spawn
            # point. Cancel the asyncio task directly so its body cannot
            # run past stop().
            task.cancel()
        # 4. Brief wait for the exception path / cancellation to settle.
        with suppress(TimeoutError):
            await asyncio.wait(still_running, timeout=kill_timeout)

    async def _dispatch_loop(self) -> None:
        log.info("dispatch loop online")
        try:
            while not self._stop.is_set():
                slot_uid: int | None = None
                capacity_acquired = False
                wait_for_work = False
                row: EventRow | None = None
                try:
                    slot_uid = await self._acquire_capacity()
                    capacity_acquired = True
                    if self._stop.is_set():
                        continue
                    row = await self._claim_next_unique()
                    if row is None:
                        wait_for_work = True
                    else:
                        try:
                            task = asyncio.create_task(
                                self._run_event(row, slot_uid=slot_uid, capacity_acquired=True),
                                name=f"omp-maintainer-event-{row.delivery_id[:8]}",
                            )
                        except Exception:
                            log.exception("failed to start event worker", extra={"delivery": row.delivery_id})
                            await self._requeue_unstarted(row)
                            wait_for_work = True
                        else:
                            self._inflight_tasks[task] = row.delivery_id
                            task.add_done_callback(lambda t: self._inflight_tasks.pop(t, None))
                            # `_run_event` now owns this permit and releases it.
                            capacity_acquired = False
                finally:
                    if capacity_acquired:
                        self._release_capacity(slot_uid)

                if wait_for_work:
                    self._wakeup.clear()
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), timeout=10.0)
                    except TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("dispatch loop crashed")

    async def _acquire_capacity(self) -> int | None:
        """Acquire one execution permit before claiming a durable event."""
        if self._slot_pool is not None:
            return await self._slot_pool.acquire()
        if self._semaphore is not None:
            await self._semaphore.acquire()
        return None

    def _release_capacity(self, slot_uid: int | None) -> None:
        """Release a permit acquired by `_acquire_capacity`."""
        if self._slot_pool is not None:
            self._slot_pool.release(slot_uid)
        elif self._semaphore is not None:
            self._semaphore.release()

    async def _requeue_unstarted(self, row: EventRow) -> None:
        """Return a row whose worker task could not be created."""
        try:
            requeued = await asyncio.to_thread(self.db.requeue_event, row.delivery_id, from_states=("running",))
            if not requeued:
                log.error("failed to requeue unstarted event", extra={"delivery": row.delivery_id})
        except Exception:
            log.exception("failed to requeue unstarted event", extra={"delivery": row.delivery_id})
        finally:
            await self._release(row)

    async def _claim_next_unique(self) -> EventRow | None:
        """Claim the next event whose issue isn't already inflight."""
        # The DB layer doesn't filter by issue_key; we peek then guard with a set.
        async with self._inflight_lock:
            # Naive but fine for v1 (small queue).
            row = await asyncio.to_thread(self.db.claim_next_event)
            if row is None:
                return None
            key = row.issue_key or row.delivery_id
            if key in self._inflight:
                # Put it back; another in-flight task is touching the same issue.
                await asyncio.to_thread(self.db.requeue_event, row.delivery_id, from_states=("running",))
                # Sleep briefly so we don't spin.
                await asyncio.sleep(0.5)
                return None
            self._inflight.add(key)
        return row

    async def _release(self, row: EventRow) -> None:
        key = row.issue_key or row.delivery_id
        async with self._inflight_lock:
            self._inflight.discard(key)

    def _arm_cancel(self, delivery_id: str, hook: Callable[[], None]) -> None:
        """Worker-side: install the cancel hook.

        If cancellation was already requested before the worker reached this
        point, fire the hook immediately so we don't lose the signal.
        """
        if delivery_id in self._cancelled:
            try:
                hook()
            except Exception:
                log.exception("late cancel fire failed", extra={"delivery": delivery_id})
            return
        self._cancel_hooks[delivery_id] = hook

    def _disarm_cancel(self, delivery_id: str) -> None:
        """Worker-side: clear the cancel hook (the resource is gone)."""
        self._cancel_hooks.pop(delivery_id, None)

    async def cancel_event(self, delivery_id: str) -> bool:
        """Request cancellation of a running event. Returns whether a hook fired.

        Marks the delivery as cancelled regardless of whether a worker is
        currently armed, so a late-armed hook still observes the request. The
        worker thread's exception path is what eventually transitions the row
        to `failed` with a cancellation marker.
        """
        self._cancelled.add(delivery_id)
        hook = self._cancel_hooks.pop(delivery_id, None)
        if hook is None:
            return False
        # `hook` typically kills a subprocess; run it off the loop so its wait()
        # doesn't stall the event loop for up to the omp shutdown grace period.
        try:
            await asyncio.to_thread(hook)
        except Exception:
            log.exception("cancel hook raised", extra={"delivery": delivery_id})
        return True

    async def _run_event(
        self,
        row: EventRow,
        *,
        slot_uid: int | None = None,
        capacity_acquired: bool = False,
    ) -> None:
        token = set_current_event(self, row.delivery_id)
        try:
            # Dispatcher-owned permits arrive here already acquired. Retain
            # local acquisition for direct callers such as focused unit tests.
            if not capacity_acquired:
                slot_uid = await self._acquire_capacity()
                capacity_acquired = True
            await self._dispatch_and_mark(row, slot_uid=slot_uid)
        except Exception as exc:
            if row.delivery_id in self._shutdown_cancelled:
                # `stop()` deliberately interrupted this delivery —
                # leave the row in `running` so `reset_stuck_running()`
                # flips it back to `queued` on the next start and the
                # resumed omp session picks up via `--continue`.
                # Other exceptions during the drain window (which
                # would also see `_shutting_down=True`) MUST still
                # mark the row failed; otherwise a genuine bug gets
                # silently requeued.
                log.info(
                    "event interrupted by shutdown",
                    extra={"delivery": row.delivery_id, "key": row.issue_key},
                )
            elif row.delivery_id in self._cancelled:
                log.info("event cancelled", extra={"delivery": row.delivery_id})
                self.db.mark_event(row.delivery_id, "failed", error="cancelled by operator")
            else:
                tb = traceback.format_exc(limit=20)
                err = f"{exc}\n{tb}"
                max_retries = self.settings.event_max_retries
                delay = self.settings.retry_delay_seconds(row.attempts)
                if 0 < row.attempts <= max_retries and self.db.schedule_retry(
                    row.delivery_id, delay_seconds=delay, error=err
                ):
                    log.warning(
                        "event retry scheduled",
                        extra={
                            "delivery": row.delivery_id,
                            "key": row.issue_key,
                            "attempt": row.attempts,
                            "max_retries": max_retries,
                            "retry_in_seconds": round(delay, 1),
                        },
                    )
                else:
                    log.exception("event handler failed", extra={"delivery": row.delivery_id})
                    self.db.mark_event(row.delivery_id, "failed", error=err)
        finally:
            self._cancelled.discard(row.delivery_id)
            self._shutdown_cancelled.discard(row.delivery_id)
            self._cancel_hooks.pop(row.delivery_id, None)
            # Reap before cache cleanup, but retain the permit until this task
            # has also released its issue key. Otherwise a completed handler
            # can briefly coexist with a newly claimed successor.
            try:
                if capacity_acquired and self._slot_pool is not None:
                    _reap_slot(slot_uid)
                await self._reclaim_event_caches(row)
            finally:
                try:
                    await self._release(row)
                finally:
                    try:
                        if capacity_acquired:
                            self._release_capacity(slot_uid)
                    finally:
                        clear_current_event(token)

    async def _reclaim_event_caches(self, row: EventRow) -> None:
        """Drop the workspace's dependency caches now that its event is over.

        Runs before `_release` so the issue key is still in `_inflight`:
        nothing can re-enter `ensure_workspace` for this issue mid-reclaim.
        Skipped during shutdown (the row resumes right after restart and the
        startup sweep covers it). Best-effort: a reclaim failure never fails
        the event.
        """
        if not self.settings.reclaim_workspace_caches or self._shutting_down:
            return
        repo, sep, number = (row.issue_key or "").rpartition("#")
        if not sep or not repo or not number.isdigit():
            return
        try:
            reclaimed = await asyncio.to_thread(self.sandbox.reclaim_workspace_caches, repo=repo, number=int(number))
        except OSError as exc:
            log.warning("workspace cache reclaim failed", extra={"key": row.issue_key, "err": str(exc)})
            return
        if reclaimed:
            log.info("workspace caches reclaimed", extra={"key": row.issue_key})

    async def _dispatch_and_mark(self, row: EventRow, *, slot_uid: int | None = None) -> None:
        await self._dispatch(row, slot_uid=slot_uid)
        if row.delivery_id in self._cancelled:
            self.db.mark_event(row.delivery_id, "failed", error="cancelled by operator")
        else:
            self.db.mark_event(row.delivery_id, "done")

    async def _dispatch(self, row: EventRow, *, slot_uid: int | None = None) -> None:
        event = row.event_type
        action = str(row.payload.get("action") or "")
        log.info(
            "dispatch",
            extra={
                "event": event,
                "action": action,
                "delivery": row.delivery_id,
                "key": row.issue_key,
                "attempts": row.attempts,
                "recovered": row.attempts >= 2,
            },
        )
        if event == "issues" and action in ("opened", "reopened"):
            await tasks.triage_issue(
                settings=self.settings,
                db=self.db,
                github=self.github,
                sandbox=self.sandbox,
                git_transport=self.git_transport,
                payload=row.payload,
                delivery_id=row.delivery_id,
                attempts=row.attempts,
                slot_uid=slot_uid,
            )
        elif event == "workflow_run" and action == "completed":
            await tasks.handle_release_ci(
                settings=self.settings,
                db=self.db,
                github=self.github,
                sandbox=self.sandbox,
                git_transport=self.git_transport,
                payload=row.payload,
                delivery_id=row.delivery_id,
                attempts=row.attempts,
                slot_uid=slot_uid,
            )
        elif event == "issue_comment" and action == "created":
            issue = row.payload.get("issue") or {}
            if "pull_request" in issue:
                await tasks.handle_pr_conversation(
                    settings=self.settings,
                    db=self.db,
                    github=self.github,
                    sandbox=self.sandbox,
                    git_transport=self.git_transport,
                    payload=row.payload,
                    delivery_id=row.delivery_id,
                    attempts=row.attempts,
                    slot_uid=slot_uid,
                )
            else:
                await tasks.handle_comment(
                    settings=self.settings,
                    db=self.db,
                    github=self.github,
                    sandbox=self.sandbox,
                    git_transport=self.git_transport,
                    payload=row.payload,
                    delivery_id=row.delivery_id,
                    attempts=row.attempts,
                    slot_uid=slot_uid,
                )
        elif event == "pull_request" and action in ("opened", "reopened", "ready_for_review", "labeled"):
            await tasks.review_pr(
                settings=self.settings,
                db=self.db,
                github=self.github,
                sandbox=self.sandbox,
                git_transport=self.git_transport,
                payload=row.payload,
                delivery_id=row.delivery_id,
                attempts=row.attempts,
                slot_uid=slot_uid,
            )
        elif event == "pull_request_review_comment" and action == "created":
            await tasks.handle_review(
                settings=self.settings,
                db=self.db,
                github=self.github,
                sandbox=self.sandbox,
                git_transport=self.git_transport,
                payload=row.payload,
                delivery_id=row.delivery_id,
                attempts=row.attempts,
                slot_uid=slot_uid,
            )
        elif event == "issues" and action == "closed":
            await tasks.cleanup_workspace(
                settings=self.settings,
                db=self.db,
                sandbox=self.sandbox,
                payload=row.payload,
                target_state="closed",
            )
        elif event == "pull_request" and action == "closed":
            pr = row.payload.get("pull_request") or {}
            target_state = "merged" if bool(pr.get("merged")) else "closed"
            await tasks.cleanup_workspace(
                settings=self.settings,
                db=self.db,
                sandbox=self.sandbox,
                payload=row.payload,
                target_state=target_state,
            )
        else:
            log.info("no-op dispatch", extra={"event": event, "action": action})


__all__ = ["WorkerPool"]
