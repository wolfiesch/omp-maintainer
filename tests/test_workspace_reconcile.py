"""WorkspaceReconciler: recover workspaces and rows whose close webhook was lost."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from omp_maintainer.config import Settings
from omp_maintainer.db import Database, close_database, get_database
from omp_maintainer.github_client import GitHubError, IssueInfo, PullRequestInfo
from omp_maintainer.queue import WorkerPool
from omp_maintainer.sandbox import SandboxManager, parse_workspace_key, workspace_key
from omp_maintainer.slot_pool import SlotPool
from omp_maintainer.workspace_reconcile import WorkspaceReconciler

REPO = "octo/widget"


class _LiveGitHub:
    """Live GitHub view: `closed`/`merged` numbers, everything else open."""

    def __init__(
        self,
        *,
        closed: set[int] = frozenset(),
        merged: set[int] = frozenset(),
        error: GitHubError | None = None,
    ) -> None:
        self.closed = set(closed) | set(merged)
        self.merged = set(merged)
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def get_issue(self, repo: str, number: int) -> IssueInfo:
        self.calls.append(("issue", number))
        if self.error is not None:
            raise self.error
        state = "closed" if number in self.closed else "open"
        return IssueInfo(repo, number, "t", "b", state, "alice", (), False)

    async def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
        self.calls.append(("pr", number))
        if self.error is not None:
            raise self.error
        state = "closed" if number in self.closed else "open"
        return PullRequestInfo(
            repo, number, f"https://x/{number}", "omp/b", "main", state, merged=number in self.merged
        )


class _Wake:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


@pytest.fixture
def db(settings: Settings) -> Database:
    database = get_database(settings.sqlite_path)
    yield database
    close_database()


@pytest.fixture
def sandbox(tmp_path: Path) -> SandboxManager:
    return SandboxManager(tmp_path / "ws")


def _make_workspace(sandbox: SandboxManager, repo: str, number: int, *, age_hours: float = 48.0) -> Path:
    root = sandbox.workspace_root(repo, number)
    (root / "repo").mkdir(parents=True)
    then = time.time() - age_hours * 3600
    os.utime(root, (then, then))
    return root


def _backdate(db: Database, key: str, *, hours: float = 48.0) -> None:
    db._conn.execute(  # noqa: SLF001
        "UPDATE issues SET updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) WHERE key=?",
        (f"-{hours} hours", key),
    )


def _reconciler(settings, db, sandbox, github, wake=None, **kwargs) -> WorkspaceReconciler:
    return WorkspaceReconciler(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        wake=wake or _Wake(),
        **kwargs,
    )


async def _drain(settings: Settings, db: Database, sandbox: SandboxManager) -> list[str]:
    """Run every queued event through the real dispatcher; return their delivery ids."""
    pool = WorkerPool(
        settings=settings,
        db=db,
        github=_LiveGitHub(),  # type: ignore[arg-type]
        sandbox=sandbox,
        git_transport=None,  # type: ignore[arg-type]
        slot_pool=SlotPool(),
    )
    ran: list[str] = []
    while (row := db.claim_next_event()) is not None:
        await pool._dispatch(row)  # noqa: SLF001
        db.mark_event(row.delivery_id, "done")
        ran.append(row.delivery_id)
    return ran


async def test_lost_merge_of_linked_pr_removes_workspace_and_marks_merged(settings, db, sandbox) -> None:
    db.upsert_issue(key=f"{REPO}#40", repo=REPO, number=40, state="opened", pr_number=41)
    _backdate(db, f"{REPO}#40")
    ws = _make_workspace(sandbox, REPO, 40)
    wake = _Wake()

    counts = await _reconciler(settings, db, sandbox, _LiveGitHub(merged={41}), wake).tick()

    assert counts["queued"] == 1 and wake.count == 1
    assert await _drain(settings, db, sandbox) == [f"reconcile-pull_request-{workspace_key(REPO, 41)}"]
    assert not ws.exists()
    assert db.get_issue(f"{REPO}#40").state == "merged"


async def test_stuck_row_without_workspace_is_finalized(settings, db, sandbox) -> None:
    """The observed shape: row left `reproducing` after its close webhook was lost."""
    db.upsert_issue(key=f"{REPO}#21", repo=REPO, number=21, state="reproducing")
    _backdate(db, f"{REPO}#21")

    await _reconciler(settings, db, sandbox, _LiveGitHub(closed={21})).tick()
    await _drain(settings, db, sandbox)

    assert db.get_issue(f"{REPO}#21").state == "closed"


async def test_orphan_workspace_without_row_is_removed(settings, db, sandbox) -> None:
    ws = _make_workspace(sandbox, REPO, 77)

    await _reconciler(settings, db, sandbox, _LiveGitHub(closed={77})).tick()
    await _drain(settings, db, sandbox)

    assert not ws.exists()
    assert db.get_issue(f"{REPO}#77") is None


async def test_open_pr_keeps_workspace_even_when_issue_closed(settings, db, sandbox) -> None:
    """Review follow-ups on the bot's still-open PR need the workspace."""
    db.upsert_issue(key=f"{REPO}#40", repo=REPO, number=40, state="opened", pr_number=41)
    _backdate(db, f"{REPO}#40")
    ws = _make_workspace(sandbox, REPO, 40)

    counts = await _reconciler(settings, db, sandbox, _LiveGitHub(closed={40})).tick()

    assert counts == {"queued": 0, "open": 1, "active": 0, "recent": 0, "errors": 0}
    assert db.claim_next_event() is None
    assert ws.exists()


async def test_recent_activity_is_left_alone(settings, db, sandbox) -> None:
    db.upsert_issue(key=f"{REPO}#5", repo=REPO, number=5, state="fixing")
    _make_workspace(sandbox, REPO, 5, age_hours=48)
    _make_workspace(sandbox, REPO, 6, age_hours=1)
    github = _LiveGitHub(closed={5, 6})

    counts = await _reconciler(settings, db, sandbox, github).tick()

    assert counts["recent"] == 2 and counts["queued"] == 0
    assert github.calls == []


async def test_item_with_pending_event_is_skipped(settings, db, sandbox) -> None:
    db.upsert_issue(key=f"{REPO}#9", repo=REPO, number=9, state="reviewing", pr_number=9)
    _backdate(db, f"{REPO}#9")
    db.record_event(
        delivery_id="retry-9",
        event_type="pull_request",
        repo=REPO,
        issue_key=f"{REPO}#9",
        payload={"action": "opened", "pull_request": {"number": 9}, "repository": {"full_name": REPO}},
    )
    github = _LiveGitHub(closed={9})

    counts = await _reconciler(settings, db, sandbox, github).tick()

    assert counts["active"] == 1 and counts["queued"] == 0
    assert github.calls == []


async def test_github_error_queues_nothing(settings, db, sandbox) -> None:
    ws = _make_workspace(sandbox, REPO, 12)

    counts = await _reconciler(
        settings, db, sandbox, _LiveGitHub(closed={12}, error=GitHubError(502, "bad gateway"))
    ).tick()

    assert counts["errors"] == 1 and counts["queued"] == 0
    assert db.claim_next_event() is None
    assert ws.exists()


async def test_ignores_release_and_non_allowlisted_workspaces(settings, db, sandbox) -> None:
    release = sandbox.workspace_root(REPO, "release")
    release.mkdir(parents=True)
    other = _make_workspace(sandbox, "someone/else", 3)
    github = _LiveGitHub(closed={3})

    counts = await _reconciler(settings, db, sandbox, github).tick()

    assert sum(counts.values()) == 0
    assert github.calls == []
    assert release.exists() and other.exists()


async def test_second_tick_does_not_duplicate_pending_close(settings, db, sandbox) -> None:
    _make_workspace(sandbox, REPO, 77)
    reconciler = _reconciler(settings, db, sandbox, _LiveGitHub(closed={77}))

    first = await reconciler.tick()
    second = await reconciler.tick()

    assert first["queued"] == 1
    assert second["active"] == 1 and second["queued"] == 0


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("octo__widget__12", ("octo/widget", 12)),
        ("SynthGL__wolfxl-oss__11", ("SynthGL/wolfxl-oss", 11)),
        ("octo__my__repo__7", ("octo/my__repo", 7)),
        ("octo__widget__release", None),
        ("_pool", None),
        ("octo__12", None),
    ],
)
def test_parse_workspace_key(name: str, expected: tuple[str, int] | None) -> None:
    assert parse_workspace_key(name) == expected
    if expected is not None:
        assert workspace_key(*expected) == name
