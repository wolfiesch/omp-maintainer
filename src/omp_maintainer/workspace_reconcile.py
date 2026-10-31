"""Periodic recovery for close webhooks that never arrived.

A workspace is removed, and its issue row finalized, only by the
`issues.closed` / `pull_request.closed` delivery. When that delivery is lost
(GitHub delivery failure, ingress outage, orchestrator down past GitHub's
retry window) nothing else ever cleans up, so the workspace stays on disk and
the row stays in a working state forever.

Each tick collects two kinds of candidates for allowlisted repos:
  - unfinished issue rows (state not in `FINAL_ISSUE_STATES`), and
  - per-issue workspace directories on disk, whatever their row says.

Candidates touched within `min_idle`, or with a queued/running event, are left
alone. For the rest the live GitHub state decides:
  - A row linked to a PR follows the PR: once it is closed, the missed
    `pull_request.closed` is queued. While the PR is open nothing happens,
    even if the issue itself was closed, because review follow-ups on that PR
    still need the workspace.
  - Otherwise the item's own number decides: once closed, the missed
    `issues.closed` is queued.

The synthesized event goes through the durable queue under the same issue key
the webhook router would assign, so it serializes with any other work on that
item and runs the ordinary `cleanup_workspace` handler.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from omp_maintainer.config import Settings
from omp_maintainer.db import INACTIVE_EVENT_STATES, Database, IssueRow, issue_key
from omp_maintainer.github_backend import GitHubBackend
from omp_maintainer.github_client import GitHubError
from omp_maintainer.sandbox import SandboxManager, workspace_key

log = logging.getLogger(__name__)

# Leave recently active items alone. A maintainer directive may deliberately
# work on a closed item; this keeps that conversation's workspace for a day
# after its last activity instead of reclaiming it right after each run.
DEFAULT_MIN_IDLE = timedelta(hours=24)


@dataclass(slots=True)
class _Candidate:
    repo: str
    number: int
    key: str
    row: IssueRow | None
    last_activity: datetime


@dataclass(slots=True, frozen=True)
class _MissedClose:
    event_type: str
    issue_key: str
    number: int
    payload: Mapping[str, Any]


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def reconcile_delivery_id(event_type: str, repo: str, number: int) -> str:
    """Stable delivery id per item: at most one pending reconcile close each."""
    return f"reconcile-{event_type}-{workspace_key(repo, number)}"


class WorkspaceReconciler:
    """Background loop that queues close events GitHub never delivered."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        github: GitHubBackend,
        sandbox: SandboxManager,
        wake: Callable[[], None],
        min_idle: timedelta = DEFAULT_MIN_IDLE,
    ) -> None:
        self._settings = settings
        self._db = db
        self._github = github
        self._sandbox = sandbox
        self._wake = wake
        self._min_idle = min_idle
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.workspace_reconcile_seconds > 0

    async def start(self) -> None:
        """Spawn the background loop. No-op when disabled."""
        if not self.enabled:
            log.info("workspace reconcile disabled")
            return
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="workspace-reconcile")
        log.info(
            "workspace reconcile started",
            extra={"interval_seconds": self._settings.workspace_reconcile_seconds},
        )

    async def stop(self) -> None:
        """Signal the loop to exit and await its termination."""
        if self._task is None:
            return
        assert self._stop_event is not None
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            self._task = None
            self._stop_event = None

    async def _run(self) -> None:
        assert self._stop_event is not None
        interval = float(self._settings.workspace_reconcile_seconds)
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("workspace reconcile tick failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def tick(self) -> dict[str, int]:
        """Check every candidate once and queue missed close events. Exposed for tests."""
        now = datetime.now(UTC)
        counts = {"queued": 0, "open": 0, "active": 0, "recent": 0, "errors": 0}
        for cand in await asyncio.to_thread(self._candidates):
            if now - cand.last_activity < self._min_idle:
                counts["recent"] += 1
                continue
            if self._db.has_active_event(cand.key):
                counts["active"] += 1
                continue
            try:
                missed = await self._missed_close(cand)
            except GitHubError as exc:
                counts["errors"] += 1
                log.warning(
                    "workspace reconcile: live state check failed; will retry next tick",
                    extra={"key": cand.key, "status": exc.status, "gh_message": exc.message},
                )
                continue
            if missed is None:
                counts["open"] += 1
                continue
            if self._enqueue(cand, missed):
                counts["queued"] += 1
        if counts["queued"]:
            self._wake()
        log.info("workspace reconcile tick", extra=counts)
        return counts

    def _candidates(self) -> list[_Candidate]:
        by_key: dict[str, _Candidate] = {}
        for row in self._db.list_unfinished_issues():
            if not self._settings.allows(row.repo):
                continue
            updated = _parse_timestamp(row.updated_at)
            if updated is None:
                continue
            by_key[row.key] = _Candidate(row.repo, row.number, row.key, row, updated)
        for repo, number, path in self._sandbox.issue_workspaces():
            if not self._settings.allows(repo):
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            except OSError:
                continue
            key = issue_key(repo, number)
            existing = by_key.get(key)
            if existing is not None:
                existing.last_activity = max(existing.last_activity, modified)
                continue
            row = self._db.get_issue(key)
            updated = _parse_timestamp(row.updated_at) if row is not None else None
            last = max(modified, updated) if updated is not None else modified
            by_key[key] = _Candidate(repo, number, key, row, last)
        return list(by_key.values())

    async def _missed_close(self, cand: _Candidate) -> _MissedClose | None:
        """The close delivery GitHub would have sent, or None while still open."""
        repository = {"full_name": cand.repo}
        pr_number = cand.row.pr_number if cand.row is not None else None
        if pr_number is not None:
            pr = await self._github.get_pull_request(cand.repo, pr_number)
            if pr.state != "closed":
                return None
            # Same key the router assigns a `pull_request.closed` delivery.
            linked = self._db.find_issue_by_pr(cand.repo, pr_number)
            return _MissedClose(
                event_type="pull_request",
                issue_key=linked.key if linked is not None else issue_key(cand.repo, pr_number),
                number=pr_number,
                payload={
                    "action": "closed",
                    "pull_request": {"number": pr_number, "merged": pr.merged},
                    "repository": repository,
                },
            )
        issue = await self._github.get_issue(cand.repo, cand.number)
        if issue.state != "closed":
            return None
        return _MissedClose(
            event_type="issues",
            issue_key=cand.key,
            number=cand.number,
            payload={"action": "closed", "issue": {"number": cand.number}, "repository": repository},
        )

    def _enqueue(self, cand: _Candidate, missed: _MissedClose) -> bool:
        delivery_id = reconcile_delivery_id(missed.event_type, cand.repo, missed.number)
        queued = self._db.replace_event_if_state_in(
            delivery_id=delivery_id,
            event_type=missed.event_type,
            repo=cand.repo,
            issue_key=missed.issue_key,
            payload=missed.payload,
            state="queued",
            allowed_existing_states=INACTIVE_EVENT_STATES,
        )
        if queued:
            log.info(
                "workspace reconcile: queued missed close",
                extra={
                    "delivery": delivery_id,
                    "key": missed.issue_key,
                    "row_state": cand.row.state if cand.row is not None else None,
                },
            )
        return queued


__all__ = ["DEFAULT_MIN_IDLE", "WorkspaceReconciler", "reconcile_delivery_id"]
