import asyncio
import logging
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from omp_maintainer import tasks
from omp_maintainer.github_client import IssueInfo, RepoInfo
from omp_maintainer.policy import default_repo_policy
from omp_maintainer.worker import TaskInputs


def _release_policy(
    *,
    enabled: bool = True,
    max_rounds: int = 5,
    commit_prefix: str = "chore: bump version to ",
    allowed_tag_pattern: str = r"^v[0-9]+\.[0-9]+\.[0-9]+.*$",
):
    policy = default_repo_policy()
    release_sentinel = policy.ci_repair.release_sentinel.model_copy(
        update={
            "enabled": enabled,
            "max_rounds": max_rounds,
            "commit_prefix": commit_prefix,
            "allowed_tag_pattern": allowed_tag_pattern,
        }
    )
    return policy.model_copy(
        update={"ci_repair": policy.ci_repair.model_copy(update={"release_sentinel": release_sentinel})}
    )


async def test_triage_issue_keeps_event_loop_live_while_workspace_setup_blocks(db, settings, monkeypatch, tmp_path):
    async def _resolve_repo_and_issue(_github, _payload):
        repo = RepoInfo(
            full_name="octo/widget",
            default_branch="main",
            clone_url="https://x/octo/widget.git",
            private=False,
        )
        issue = IssueInfo(
            repo="octo/widget",
            number=1,
            title="bug",
            body="b",
            state="open",
            author="alice",
            labels=(),
            is_pull_request=False,
        )
        return repo, issue

    monkeypatch.setattr(tasks, "_resolve_repo_and_issue", _resolve_repo_and_issue)

    async def _no_closing(*a, **k):
        return ()

    github = SimpleNamespace(list_closing_pull_requests=_no_closing)

    entered = threading.Event()
    release = threading.Event()
    captured: dict[str, object] = {}
    expected_policy = default_repo_policy()
    monkeypatch.setattr(tasks, "load_repo_policy", lambda *, repo_dir, default_branch: expected_policy)

    def _blocking_ensure(**_kwargs):
        entered.set()
        # True ONLY if a concurrent coroutine set `release` while we blocked here.
        # Blocks a WORKER THREAD (via to_thread) in the fixed code; blocks the
        # LOOP itself in the broken code.
        captured["release_seen_in_time"] = release.wait(1.0)
        return SimpleNamespace(branch="farm/x/y", session_dir=str(tmp_path / "sess"), repo_dir=tmp_path)

    sandbox = SimpleNamespace(ensure_workspace=_blocking_ensure)

    async def _noop_run_task(**kwargs):
        captured["policy"] = kwargs["inputs"].policy
        return None

    monkeypatch.setattr(tasks, "run_task", _noop_run_task)

    async def _releaser():
        # Waits (off-loop) until ensure_workspace has actually started, then
        # releases it. This coroutine can ONLY make progress if the event loop
        # is live while ensure_workspace is blocking.
        await asyncio.to_thread(entered.wait, 1.0)
        assert entered.is_set(), "ensure_workspace never started"
        release.set()

    triage_task = asyncio.create_task(
        tasks.triage_issue(
            settings=settings,
            db=db,
            github=github,
            sandbox=sandbox,
            git_transport=SimpleNamespace(),
            payload={},
            delivery_id="d1",
        )
    )
    releaser_task = asyncio.create_task(_releaser())

    await asyncio.wait_for(triage_task, timeout=3.0)
    await asyncio.wait_for(releaser_task, timeout=1.0)

    assert captured.get("release_seen_in_time") is True, (
        "event loop was frozen during ensure_workspace: the concurrent releaser "
        "could not run, so release.wait timed out (this is the pre-fix hang)"
    )
    assert captured["policy"] is expected_policy


async def test_run_workspace_op_drains_thread_before_propagating_cancel():
    started = threading.Event()
    proceed = threading.Event()
    finished = threading.Event()

    def slow_op(**_kwargs):
        started.set()
        # Block on the worker thread until the test releases us.
        assert proceed.wait(2.0), "proceed was never set — test bug"
        finished.set()
        return "done"

    task = asyncio.create_task(tasks._run_workspace_op(slow_op))
    # Wait (off-loop) until the worker thread is actually running.
    await asyncio.to_thread(started.wait, 1.0)
    assert started.is_set()

    async def pump(turns: int = 20) -> None:
        # Deterministically advance the loop without a wall-clock sleep: each
        # sleep(0) drains the ready queue, so a DETACHING (pre-fix) helper would
        # resolve `task` within these turns. A draining helper keeps it pending
        # while the worker thread is still blocked on `proceed`.
        for _ in range(turns):
            await asyncio.sleep(0)

    # Cancel the AWAITING coroutine while the thread is mid-flight, then a SECOND
    # time while it is still blocked. The repeated cancel must land on the drain
    # loop's re-`await` and be swallowed by its `continue` branch, NOT abandon
    # the thread. The whole sequence runs under try/finally so any failed assert
    # still releases the worker and cannot leak a blocked thread into later tests.
    try:
        task.cancel()
        await pump()
        assert not task.done(), "helper propagated the first cancel before the thread completed (thread abandoned)"
        task.cancel()
        await pump()
        # The thread is still blocked on `proceed`, so it has not finished and
        # the task has not resolved despite two cancels.
        assert not finished.is_set(), "thread finished before we released it — impossible unless abandoned"
        assert not task.done(), "helper abandoned the thread after a repeated cancel"
    finally:
        proceed.set()

    # The helper must now let the thread finish, THEN raise CancelledError.
    with pytest.raises(asyncio.CancelledError):
        await task
    # Deterministic in the fixed helper: the thread completed before the cancel propagated.
    assert finished.is_set(), "thread did not complete before cancellation propagated"


async def test_run_workspace_op_logs_worker_exception_on_concurrent_cancel(caplog):
    started = threading.Event()
    proceed = threading.Event()
    boom = RuntimeError("git exploded")

    def failing_op(**_kwargs):
        started.set()
        assert proceed.wait(2.0), "proceed was never set — test bug"
        raise boom

    task = asyncio.create_task(tasks._run_workspace_op(failing_op))
    await asyncio.to_thread(started.wait, 1.0)
    assert started.is_set()

    # Cancel the caller while the worker is still blocked (mid-flight), so the
    # helper enters its cancel-drain loop and is awaiting the shielded inner.
    task.cancel()
    await asyncio.sleep(0.05)

    with caplog.at_level(logging.WARNING, logger="omp_maintainer.tasks"):
        # Release the worker so inner completes WITH an exception while the
        # helper is draining -> the drain's `await shield(inner)` re-raises boom,
        # breaks the loop, and the guarded log.warning must fire.
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "worker exception during cancel was not logged"
    assert any(r.exc_info and r.exc_info[1] is boom for r in warnings), (
        "the worker's exception was not attached to the warning"
    )


async def test_triage_issue_reopen_tears_down_finalized_workspace(db, settings, monkeypatch, tmp_path):
    """Re-triage of a finalized (reopened) issue must clear the stale workspace first.

    The prior branch was merged/deleted when the issue finalized, so a reopen has
    to branch afresh — mirroring the maintainer directive-reopen teardown.
    """

    async def _resolve_repo_and_issue(_github, _payload):
        repo = RepoInfo(
            full_name="octo/widget",
            default_branch="main",
            clone_url="https://x/octo/widget.git",
            private=False,
        )
        issue = IssueInfo(
            repo="octo/widget",
            number=1,
            title="bug",
            body="b",
            state="open",
            author="alice",
            labels=(),
            is_pull_request=False,
        )
        return repo, issue

    monkeypatch.setattr(tasks, "_resolve_repo_and_issue", _resolve_repo_and_issue)

    # The bot previously finalized this issue: a stale row + workspace exist.
    db.upsert_issue(key="octo/widget#1", repo="octo/widget", number=1, state="closed")

    calls: list[str] = []

    def _remove(**_kwargs):
        calls.append("remove")

    def _ensure(**_kwargs):
        calls.append("ensure")
        return SimpleNamespace(branch="farm/x/y", session_dir=str(tmp_path / "sess"), repo_dir=tmp_path)

    async def _fail_closing(*_a, **_k):
        raise AssertionError("closing-PR guard must not run when a DB row already exists")

    github = SimpleNamespace(list_closing_pull_requests=_fail_closing)
    sandbox = SimpleNamespace(ensure_workspace=_ensure, remove_workspace=_remove)

    async def _noop_run_task(**_kwargs):
        return None

    monkeypatch.setattr(tasks, "run_task", _noop_run_task)

    await tasks.triage_issue(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=SimpleNamespace(),
        payload={},
        delivery_id="d1",
    )

    # Teardown must precede re-provisioning, and the row resets to a live state.
    assert calls == ["remove", "ensure"]
    row = db.get_issue("octo/widget#1")
    assert row is not None
    assert row.state == "reproducing"


async def test_finalized_issue_comment_without_directive_does_not_mutate_github(db, settings, monkeypatch):
    repo = RepoInfo(
        full_name="octo/widget",
        default_branch="main",
        clone_url="https://x/octo/widget.git",
        private=False,
    )
    issue = IssueInfo(
        repo=repo.full_name,
        number=1,
        title="bug",
        body="b",
        state="closed",
        author="alice",
        labels=(),
        is_pull_request=False,
    )

    async def _resolve_repo_and_issue(_github, _payload):
        return repo, issue

    mutations: list[tuple[object, ...]] = []

    async def _post_comment(*args: object) -> None:
        mutations.append(args)

    monkeypatch.setattr(tasks, "_resolve_repo_and_issue", _resolve_repo_and_issue)
    db.upsert_issue(key="octo/widget#1", repo=repo.full_name, number=issue.number, state="closed")

    await tasks.handle_comment(
        settings=settings,
        db=db,
        github=SimpleNamespace(post_comment=_post_comment),
        sandbox=SimpleNamespace(),
        git_transport=SimpleNamespace(),
        payload={},
        delivery_id="finalized-issue-comment",
    )

    assert mutations == []


async def test_finalized_pr_conversation_without_directive_does_not_mutate_github(db, settings, monkeypatch):
    db.upsert_issue(
        key="octo/widget#1",
        repo="octo/widget",
        number=1,
        state="closed",
        branch="farm/issue-1",
        pr_number=7,
    )
    issue_row = db.get_issue("octo/widget#1")
    assert issue_row is not None

    async def _resolve_issue_row_for_pr(**_kwargs):
        return issue_row, None

    mutations: list[tuple[object, ...]] = []

    async def _post_comment(*args: object) -> None:
        mutations.append(args)

    monkeypatch.setattr(tasks, "_resolve_issue_row_for_pr", _resolve_issue_row_for_pr)

    await tasks.handle_pr_conversation(
        settings=settings,
        db=db,
        github=SimpleNamespace(post_comment=_post_comment),
        sandbox=SimpleNamespace(),
        git_transport=SimpleNamespace(),
        payload={"repository": {"full_name": "octo/widget"}, "issue": {"number": 7}},
        delivery_id="finalized-pr-conversation",
    )

    assert mutations == []


async def test_bare_pr_mention_does_not_mutate_github(db, settings, monkeypatch):
    db.upsert_issue(
        key="octo/widget#1",
        repo="octo/widget",
        number=1,
        state="opened",
        branch="farm/issue-1",
        pr_number=7,
    )
    issue_row = db.get_issue("octo/widget#1")
    assert issue_row is not None

    async def _resolve_issue_row_for_pr(**_kwargs):
        return issue_row, None

    mutations: list[tuple[object, ...]] = []

    async def _post_comment(*args: object) -> None:
        mutations.append(args)

    monkeypatch.setattr(tasks, "_resolve_issue_row_for_pr", _resolve_issue_row_for_pr)

    await tasks.handle_pr_conversation(
        settings=settings,
        db=db,
        github=SimpleNamespace(post_comment=_post_comment),
        sandbox=SimpleNamespace(),
        git_transport=SimpleNamespace(),
        payload={
            "repository": {"full_name": "octo/widget"},
            "issue": {"number": 7},
            "_omp_maintainer_directive": {},
        },
        delivery_id="bare-pr-mention",
    )

    assert mutations == []


async def test_finalized_issue_directive_reopens_and_runs_with_task_inputs(db, settings, monkeypatch, tmp_path):
    repo = RepoInfo(
        full_name="octo/widget",
        default_branch="main",
        clone_url="https://x/octo/widget.git",
        private=False,
    )
    issue = IssueInfo(
        repo=repo.full_name,
        number=1,
        title="bug",
        body="b",
        state="open",
        author="alice",
        labels=(),
        is_pull_request=False,
    )
    expected_policy = default_repo_policy()
    calls: list[str] = []
    captured: dict[str, object] = {}
    mutations: list[tuple[object, ...]] = []

    async def _resolve_repo_and_issue(_github, _payload):
        return repo, issue

    def _remove(**_kwargs):
        calls.append("remove")

    def _ensure(**_kwargs):
        calls.append("ensure")
        return SimpleNamespace(branch="farm/issue-1", session_dir=str(tmp_path / "sess"), repo_dir=tmp_path)

    async def _get_issue(_repo: str, _number: int) -> IssueInfo:
        return issue

    async def _list_comments(_repo: str, _number: int) -> tuple[()]:
        return ()

    async def _post_comment(*args: object) -> None:
        mutations.append(args)

    async def _run_task(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(tasks, "_resolve_repo_and_issue", _resolve_repo_and_issue)
    monkeypatch.setattr(tasks, "load_repo_policy", lambda **_kwargs: expected_policy)
    monkeypatch.setattr(tasks, "run_task", _run_task)
    db.upsert_issue(key="octo/widget#1", repo=repo.full_name, number=issue.number, state="closed")

    await tasks.handle_comment(
        settings=settings,
        db=db,
        github=SimpleNamespace(
            get_issue=_get_issue,
            list_comments=_list_comments,
            post_comment=_post_comment,
        ),
        sandbox=SimpleNamespace(remove_workspace=_remove, ensure_workspace=_ensure),
        git_transport=SimpleNamespace(),
        payload={"_omp_maintainer_directive": {"body": "continue", "author": "maintainer"}},
        delivery_id="finalized-issue-directive",
    )

    assert calls == ["remove", "ensure"]
    assert captured["task_kind"] == "handle_comment"
    assert isinstance(captured["inputs"], TaskInputs)
    assert captured["inputs"].policy is expected_policy
    assert captured["directive"].body == "continue"
    assert db.get_issue("octo/widget#1").state == "reproducing"
    assert mutations == []


async def test_finalized_pr_directive_reopens_and_runs_with_task_inputs(db, settings, monkeypatch, tmp_path):
    repo = RepoInfo(
        full_name="octo/widget",
        default_branch="main",
        clone_url="https://x/octo/widget.git",
        private=False,
    )
    issue = IssueInfo(
        repo=repo.full_name,
        number=1,
        title="bug",
        body="b",
        state="open",
        author="alice",
        labels=(),
        is_pull_request=False,
    )
    expected_policy = default_repo_policy()
    calls: list[str] = []
    captured: dict[str, object] = {}
    mutations: list[tuple[object, ...]] = []

    db.upsert_issue(
        key="octo/widget#1",
        repo=repo.full_name,
        number=issue.number,
        state="closed",
        branch="farm/issue-1",
        pr_number=7,
    )
    issue_row = db.get_issue("octo/widget#1")
    assert issue_row is not None

    async def _resolve_issue_row_for_pr(**_kwargs):
        return issue_row, None

    def _remove(**_kwargs):
        calls.append("remove")

    def _ensure(**_kwargs):
        calls.append("ensure")
        return SimpleNamespace(branch="farm/issue-1-reopened", session_dir=str(tmp_path / "sess"), repo_dir=tmp_path)

    async def _get_repo(_repo: str) -> RepoInfo:
        return repo

    async def _get_issue(_repo: str, _number: int) -> IssueInfo:
        return issue

    async def _list_comments(_repo: str, _number: int) -> tuple[()]:
        return ()

    async def _list_review_comments(_repo: str, _number: int) -> tuple[()]:
        return ()

    async def _list_pr_reviews(_repo: str, _number: int) -> tuple[()]:
        return ()

    async def _post_comment(*args: object) -> None:
        mutations.append(args)

    async def _run_task(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(tasks, "_resolve_issue_row_for_pr", _resolve_issue_row_for_pr)
    monkeypatch.setattr(tasks, "load_repo_policy", lambda **_kwargs: expected_policy)
    monkeypatch.setattr(tasks, "run_task", _run_task)

    await tasks.handle_pr_conversation(
        settings=settings,
        db=db,
        github=SimpleNamespace(
            get_repo=_get_repo,
            get_issue=_get_issue,
            list_comments=_list_comments,
            list_review_comments=_list_review_comments,
            list_pr_reviews=_list_pr_reviews,
            post_comment=_post_comment,
        ),
        sandbox=SimpleNamespace(remove_workspace=_remove, ensure_workspace=_ensure),
        git_transport=SimpleNamespace(),
        payload={
            "repository": {"full_name": repo.full_name},
            "issue": {"number": 7},
            "_omp_maintainer_directive": {"body": "continue", "author": "maintainer"},
        },
        delivery_id="finalized-pr-directive",
    )

    assert calls == ["remove", "ensure"]
    assert captured["task_kind"] == "handle_comment"
    assert isinstance(captured["inputs"], TaskInputs)
    assert captured["inputs"].policy is expected_policy
    assert captured["directive"].body == "continue"
    assert db.get_issue("octo/widget#1").state == "reproducing"
    assert mutations == []


async def test_release_uses_refreshed_default_branch_everywhere(db, settings, monkeypatch, tmp_path):
    head_sha = "a" * 40
    refreshed_repo = RepoInfo(
        full_name="octo/widget",
        default_branch="trunk",
        clone_url="https://api.example/octo/widget.git",
        private=False,
    )
    captured: dict[str, object] = {}
    calls: list[tuple[str, ...]] = []

    async def _get_repo(repo: str) -> RepoInfo:
        calls.append(("get_repo", repo))
        return refreshed_repo

    async def _get_tag_sha(repo: str, tag: str) -> str:
        calls.append(("get_tag_sha", repo, tag))
        return head_sha

    def _workspace_root(*_args: object) -> object:
        return tmp_path

    def _ensure_release_workspace(**kwargs: object) -> SimpleNamespace:
        captured["workspace"] = kwargs
        return SimpleNamespace(repo_dir=tmp_path)

    expected_policy = _release_policy(max_rounds=2)

    def _load_repo_policy(*, repo_dir, default_branch):
        captured["policy_default_branch"] = default_branch
        return expected_policy

    async def _failure_dossier(*_args: object, **_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return "failure", ()

    async def _run_task(**kwargs: object) -> None:
        captured["inputs"] = kwargs["inputs"]

    monkeypatch.setattr(tasks, "load_repo_policy", _load_repo_policy)
    monkeypatch.setattr(tasks, "rev_parse_head", lambda *_args, **_kwargs: head_sha)
    monkeypatch.setattr(tasks, "_release_failure_dossier", _failure_dossier)
    monkeypatch.setattr(tasks, "run_task", _run_task)
    github: Any = SimpleNamespace(get_repo=_get_repo, get_tag_sha=_get_tag_sha)
    sandbox: Any = SimpleNamespace(
        workspace_root=_workspace_root,
        ensure_release_workspace=_ensure_release_workspace,
    )
    git_transport: Any = SimpleNamespace()

    await tasks.handle_release_ci(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=git_transport,
        payload={
            "repository": {
                "full_name": "octo/widget",
                "default_branch": "main",
                "clone_url": "https://webhook.example/octo/widget.git",
                "private": False,
            },
            "workflow_run": {
                "name": "CI",
                "head_sha": head_sha,
                "head_branch": "trunk",
                "html_url": "https://example/runs/1",
                "conclusion": "failure",
                "head_commit": {"message": f"{settings.release_commit_prefix} 1.2.3"},
            },
        },
        delivery_id="release-default-branch",
    )

    assert calls == [
        ("get_repo", "octo/widget"),
        ("get_tag_sha", "octo/widget", "v1.2.3"),
    ]
    workspace = captured["workspace"]
    assert isinstance(workspace, dict)
    assert workspace["clone_url"] == "https://webhook.example/octo/widget.git"
    assert workspace["default_branch"] == "trunk"
    inputs = captured["inputs"]
    assert isinstance(inputs, TaskInputs)
    assert inputs.repo.default_branch == "trunk"
    assert inputs.release is not None
    assert inputs.release.default_branch == "trunk"
    assert inputs.release.max_rounds == 2
    assert captured["policy_default_branch"] == "trunk"


@pytest.mark.parametrize("refusal", ("disabled", "commit_prefix", "tag_pattern", "round_cap"))
async def test_release_policy_refusals_do_not_bump_or_invoke_agent(db, settings, monkeypatch, tmp_path, refusal: str):
    head_sha = "b" * 40
    tag = "v1.2.3"
    policy = _release_policy()
    starting_rounds = 0
    if refusal == "disabled":
        policy = _release_policy(enabled=False)
    elif refusal == "commit_prefix":
        policy = _release_policy(commit_prefix="release: ")
    elif refusal == "tag_pattern":
        policy = _release_policy(allowed_tag_pattern=r"^v9\..*$")
    elif refusal == "round_cap":
        policy = _release_policy(max_rounds=1)
        db.upsert_release(
            repo="octo/widget",
            tag=tag,
            version="1.2.3",
            current_sha=head_sha,
            session_dir=str(tmp_path / "release"),
        )
        db.bump_release_round(f"octo/widget#{tag}", failed_sha=head_sha)
        starting_rounds = 1

    calls = {"agent": 0, "dossier": 0}

    async def _get_repo(_repo: str) -> RepoInfo:
        return RepoInfo(
            full_name="octo/widget",
            default_branch="main",
            clone_url="https://api.example/octo/widget.git",
            private=False,
        )

    async def _get_tag_sha(_repo: str, _tag: str) -> str:
        return head_sha

    def _workspace_root(*_args: object) -> object:
        return tmp_path

    def _ensure_release_workspace(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(repo_dir=tmp_path)

    def _load_repo_policy(*, repo_dir, default_branch):
        assert repo_dir == tmp_path
        assert default_branch == "main"
        return policy

    async def _failure_dossier(*_args: object, **_kwargs: object) -> tuple[str, tuple[str, ...]]:
        calls["dossier"] += 1
        return "failure", ()

    async def _run_task(**_kwargs: object) -> None:
        calls["agent"] += 1

    monkeypatch.setattr(tasks, "load_repo_policy", _load_repo_policy)
    monkeypatch.setattr(tasks, "rev_parse_head", lambda *_args, **_kwargs: head_sha)
    monkeypatch.setattr(tasks, "_release_failure_dossier", _failure_dossier)
    monkeypatch.setattr(tasks, "run_task", _run_task)
    github: Any = SimpleNamespace(get_repo=_get_repo, get_tag_sha=_get_tag_sha)
    sandbox: Any = SimpleNamespace(
        workspace_root=_workspace_root,
        ensure_release_workspace=_ensure_release_workspace,
    )
    git_transport: Any = SimpleNamespace()

    await tasks.handle_release_ci(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=git_transport,
        payload={
            "repository": {
                "full_name": "octo/widget",
                "default_branch": "main",
                "clone_url": "https://webhook.example/octo/widget.git",
                "private": False,
            },
            "workflow_run": {
                "name": "CI",
                "head_sha": head_sha,
                "head_branch": "main",
                "html_url": "https://example/runs/1",
                "conclusion": "failure",
                "head_commit": {"message": f"{settings.release_commit_prefix} 1.2.3"},
            },
        },
        delivery_id=f"release-policy-{refusal}",
    )

    row = db.get_release(f"octo/widget#{tag}")
    assert row is not None
    assert row.rounds == starting_rounds
    assert calls == {"agent": 0, "dossier": 0}
    if refusal == "round_cap":
        assert row.state == "failed"
        assert row.last_error == "round cap 1 reached; last: CI https://example/runs/1"
    else:
        assert row.state == "awaiting_ci"


async def test_release_rejects_stale_webhook_head_branch(db, settings):
    head_sha = "c" * 40
    calls: list[str] = []

    async def _get_repo(_repo: str) -> RepoInfo:
        calls.append("get_repo")
        return RepoInfo(
            full_name="octo/widget",
            default_branch="trunk",
            clone_url="https://api.example/octo/widget.git",
            private=False,
        )

    async def _get_tag_sha(_repo: str, _tag: str) -> str:
        calls.append("get_tag_sha")
        return head_sha

    github: Any = SimpleNamespace(get_repo=_get_repo, get_tag_sha=_get_tag_sha)
    sandbox: Any = SimpleNamespace()
    git_transport: Any = SimpleNamespace()

    await tasks.handle_release_ci(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=git_transport,
        payload={
            "repository": {
                "full_name": "octo/widget",
                "default_branch": "main",
                "clone_url": "https://webhook.example/octo/widget.git",
                "private": False,
            },
            "workflow_run": {
                "head_sha": head_sha,
                "head_branch": "main",
                "head_commit": {"message": f"{settings.release_commit_prefix} 1.2.3"},
            },
        },
        delivery_id="release-stale-webhook-branch",
    )

    assert calls == ["get_repo"]
    assert db.get_release("octo/widget#v1.2.3") is None
