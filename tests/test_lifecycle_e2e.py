from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omp_maintainer import host_tools, tasks
from omp_maintainer.config import Settings
from omp_maintainer.db import Database, issue_key
from omp_maintainer.github_client import IssueInfo, PullRequestInfo, ReleaseInfo, RepoInfo, WorkflowRunInfo
from omp_maintainer.github_events import route
from omp_maintainer.host_tools import AbortController, ReleaseToolContext, ToolBindings
from omp_maintainer.sandbox import SandboxManager
from omp_maintainer.worker import TaskInputs
from omp_rpc import HostToolContext

_REPO = "octo/widget"
_ISSUE_NUMBER = 7
_TAG = "v1.2.3"
_VERSION = "1.2.3"
_CHECK = "check"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "GIT_AUTHOR_NAME": "omp-maintainer-bot",
            "GIT_AUTHOR_EMAIL": "omp-maintainer-bot@example.invalid",
            "GIT_COMMITTER_NAME": "omp-maintainer-bot",
            "GIT_COMMITTER_EMAIL": "omp-maintainer-bot@example.invalid",
        },
    )
    return proc.stdout.strip()


def _check_exit_code(repo_dir: Path) -> int:
    return subprocess.run(
        [str(repo_dir / _CHECK)],
        cwd=repo_dir,
        check=False,
        capture_output=True,
        text=True,
    ).returncode


def _write_check_and_commit(repo_dir: Path, *, message: str, review_note: str = "") -> None:
    (repo_dir / _CHECK).write_text(f"#!/bin/sh\n{review_note}exit 0\n", encoding="utf-8")
    (repo_dir / _CHECK).chmod(0o755)
    _git(repo_dir, "add", _CHECK)
    _git(repo_dir, "commit", "-m", message)


class FakeGitHubBackend:
    """In-memory GitHub boundary used by the local lifecycle proof."""

    def __init__(self, *, repo: RepoInfo, issue: IssueInfo, tag_sha: str) -> None:
        self.repo = repo
        self.issue = issue
        self.tag_sha = tag_sha
        self.labels: tuple[str, ...] = ()
        self.pull_requests: dict[int, PullRequestInfo] = {}

    async def get_repo(self, repo: str) -> RepoInfo:
        assert repo == self.repo.full_name
        return self.repo

    async def get_issue(self, repo: str, number: int) -> IssueInfo:
        assert (repo, number) == (self.repo.full_name, self.issue.number)
        return self.issue

    async def list_closing_pull_requests(self, repo: str, number: int) -> tuple[int, ...]:
        assert (repo, number) == (self.repo.full_name, self.issue.number)
        return ()

    async def add_issue_labels(self, repo: str, number: int, labels: list[str]) -> tuple[str, ...]:
        assert (repo, number) == (self.repo.full_name, self.issue.number)
        self.labels = tuple(dict.fromkeys((*self.labels, *labels)))
        return self.labels

    async def open_pull_request(
        self,
        *,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = False,
        maintainer_can_modify: bool = True,
    ) -> PullRequestInfo:
        del draft, maintainer_can_modify
        assert repo == self.repo.full_name
        number = len(self.pull_requests) + 1
        pull_request = PullRequestInfo(
            repo=repo,
            number=number,
            html_url=f"https://example.invalid/{repo}/pull/{number}",
            head_ref=head,
            base_ref=base,
            state="open",
            author="omp-maintainer-bot",
            head_repo=repo,
            title=title,
            body=body,
        )
        self.pull_requests[number] = pull_request
        return pull_request

    async def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
        assert repo == self.repo.full_name
        return self.pull_requests[number]

    async def list_workflow_runs(self, repo: str, *, head_sha: str) -> list[WorkflowRunInfo]:
        assert repo == self.repo.full_name
        return [
            WorkflowRunInfo(
                id=1,
                name="CI",
                event="push",
                status="completed",
                conclusion="failure",
                head_branch="main",
                head_sha=head_sha,
                html_url="https://example.invalid/actions/runs/1",
                run_attempt=1,
            )
        ]

    async def list_workflow_jobs(self, repo: str, run_id: int) -> list[Any]:
        assert (repo, run_id) == (self.repo.full_name, 1)
        return []

    async def get_job_log_tail(self, repo: str, job_id: int, *, tail_lines: int = 200) -> str:
        assert repo == self.repo.full_name
        del job_id, tail_lines
        return "local check failed"

    async def get_tag_sha(self, repo: str, tag: str) -> str | None:
        assert (repo, tag) == (self.repo.full_name, _TAG)
        return self.tag_sha

    async def get_release_by_tag(self, repo: str, tag: str) -> ReleaseInfo | None:
        assert (repo, tag) == (self.repo.full_name, _TAG)
        return None


@dataclass(frozen=True)
class Lifecycle:
    origin: Path
    seed: Path
    db_path: Path
    repo: RepoInfo
    issue: IssueInfo
    backend: FakeGitHubBackend
    sandbox: SandboxManager
    initial_sha: str


@pytest.fixture
def lifecycle(tmp_path: Path, settings: Settings) -> Lifecycle:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "init", "--initial-branch=main", str(seed))

    policy_dir = seed / ".github"
    policy_dir.mkdir()
    (policy_dir / "omp-maintainer.yml").write_text(
        "version: 1\n"
        "ci_repair:\n"
        "  release_sentinel:\n"
        "    enabled: true\n"
        "    max_rounds: 1\n"
        '    commit_prefix: "chore: bump version to "\n'
        "    allowed_tag_pattern: '^v[0-9]+\\.[0-9]+\\.[0-9]+$'\n",
        encoding="utf-8",
    )
    (seed / _CHECK).write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (seed / _CHECK).chmod(0o755)
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", f"chore: bump version to {_VERSION}")
    _git(seed, "tag", _TAG)
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "--set-upstream", "origin", "main")
    _git(seed, "push", "origin", _TAG)

    initial_sha = _git(seed, "rev-parse", "HEAD")
    repo = RepoInfo(full_name=_REPO, default_branch="main", clone_url=str(origin), private=False)
    issue = IssueInfo(
        repo=_REPO,
        number=_ISSUE_NUMBER,
        title="Fix failing check",
        body="The repository check always exits non-zero.",
        state="open",
        author="reporter",
        labels=(),
        is_pull_request=False,
    )
    backend = FakeGitHubBackend(repo=repo, issue=issue, tag_sha=initial_sha)
    return Lifecycle(
        origin=origin,
        seed=seed,
        db_path=tmp_path / "lifecycle.sqlite",
        repo=repo,
        issue=issue,
        backend=backend,
        sandbox=SandboxManager(settings.workspace_root),
        initial_sha=initial_sha,
    )


class FakeTaskDriver:
    """Deterministic provider substitute that invokes the production host tools."""

    def __init__(self, backend: FakeGitHubBackend) -> None:
        self.backend = backend
        self.review_branches: list[str] = []
        self.review_sessions: list[Path] = []
        self.release_rounds: list[int] = []
        self.release_sha: str | None = None

    async def __call__(
        self,
        *,
        task_kind: str,
        inputs: TaskInputs,
        pr_number: int | None = None,
        review_payload: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> None:
        bindings = self._bindings(inputs, pr_number=pr_number)
        if task_kind == "triage_issue":
            await self._execute(
                bindings,
                "classify_issue",
                {"primary": "bug", "priority": "prio:p1", "rationale": "the executable check exits non-zero"},
            )
            _write_check_and_commit(
                inputs.workspace.repo_dir,
                message="fix: make the repository check pass",
            )
            (inputs.workspace.session_dir / "triage.jsonl").write_text("{}\n", encoding="utf-8")
            await self._execute(
                bindings,
                "gh_open_pr",
                {
                    "title": "fix: make the repository check pass",
                    "body": (
                        "## Repro\n"
                        "`./check` exits non-zero on main.\n\n"
                        "## Cause\n"
                        "The check unconditionally exits 1.\n\n"
                        "## Fix\n"
                        "Return success once the check is repaired.\n\n"
                        "## Verification\n"
                        "`./check` passes.\n\n"
                        f"Fixes #{inputs.issue.number if inputs.issue else _ISSUE_NUMBER}\n"
                    ),
                    "skip_checks": True,
                },
            )
            return

        if task_kind == "handle_review":
            assert review_payload is not None
            assert review_payload["author"] == "trusted-reviewer[bot]"
            assert (inputs.workspace.session_dir / "triage.jsonl").exists()
            self.review_branches.append(inputs.workspace.branch)
            self.review_sessions.append(inputs.workspace.session_dir)
            _write_check_and_commit(
                inputs.workspace.repo_dir,
                message="fix: retain passing check after review",
                review_note="# authoritative review feedback applied\n",
            )
            await self._execute(bindings, "gh_push_branch", {"skip_checks": True})
            return

        if task_kind == "handle_release_ci":
            assert inputs.release is not None
            self.release_rounds.append(inputs.release.round)
            _write_check_and_commit(
                inputs.workspace.repo_dir,
                message=f"chore: bump version to {_VERSION} repair failing check",
            )
            result = await self._execute(
                bindings,
                "release_retag",
                {"summary": "repair the local failing check", "skip_checks": True},
            )
            assert isinstance(result, dict)
            self.release_sha = str(result["pushed"])
            self.backend.tag_sha = self.release_sha
            return

        raise AssertionError(f"unexpected task kind: {task_kind}")

    @staticmethod
    def _bindings(inputs: TaskInputs, *, pr_number: int | None) -> ToolBindings:
        release = None
        if inputs.release is not None:
            key = f"{inputs.repo.full_name}#{inputs.release.tag}"
            row = inputs.db.get_release(key)
            assert row is not None
            release = ReleaseToolContext(
                repo=inputs.repo.full_name,
                tag=inputs.release.tag,
                version=inputs.release.version,
                key=key,
                expected_sha=row.current_sha,
                default_branch=inputs.release.default_branch,
            )
        return ToolBindings(
            db=inputs.db,
            github=inputs.github,
            git_transport=inputs.git_transport,
            repo=inputs.repo,
            issue=inputs.issue,
            workspace=inputs.workspace,
            loop=asyncio.get_running_loop(),
            author_name=inputs.settings.resolved_author_name,
            author_email=inputs.settings.git_author_email,
            policy=inputs.policy,
            settings=inputs.settings,
            inbound_thread_number=pr_number,
            inbound_is_pr=pr_number is not None,
            abort=AbortController(),
            release=release,
        )

    @staticmethod
    async def _execute(bindings: ToolBindings, name: str, args: dict[str, Any]) -> Any:
        tool = next(tool for tool in host_tools.build(bindings) if tool.name == name)
        context = HostToolContext[Any](
            tool_call_id=f"lifecycle-{name}",
            _cancel_event=threading.Event(),
            _send_update=lambda _payload: None,
        )
        return await asyncio.to_thread(tool.execute, args, context)


def _repository_payload(lifecycle: Lifecycle) -> dict[str, object]:
    return {
        "full_name": lifecycle.repo.full_name,
        "default_branch": lifecycle.repo.default_branch,
        "clone_url": lifecycle.repo.clone_url,
        "private": lifecycle.repo.private,
        "owner": {"login": "octo", "type": "Organization"},
    }


def _issue_payload(lifecycle: Lifecycle) -> dict[str, object]:
    return {
        "repository": _repository_payload(lifecycle),
        "issue": {
            "number": lifecycle.issue.number,
            "title": lifecycle.issue.title,
            "body": lifecycle.issue.body,
            "state": lifecycle.issue.state,
            "user": {"login": lifecycle.issue.author},
        },
    }


def _release_payload(lifecycle: Lifecycle, head_sha: str, *, subject: str) -> dict[str, object]:
    return {
        "action": "completed",
        "repository": _repository_payload(lifecycle),
        "workflow_run": {
            "id": 1,
            "name": "CI",
            "head_branch": "main",
            "head_sha": head_sha,
            "html_url": "https://example.invalid/actions/runs/1",
            "conclusion": "failure",
            "head_commit": {"message": subject},
        },
    }


@pytest.mark.asyncio
async def test_local_lifecycle_preserves_bot_work_and_caps_release_repair(
    lifecycle: Lifecycle,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(lifecycle.db_path)
    driver = FakeTaskDriver(lifecycle.backend)
    monkeypatch.setattr(tasks, "run_task", driver)
    key = issue_key(_REPO, _ISSUE_NUMBER)

    try:
        assert _check_exit_code(lifecycle.seed) == 1

        await tasks.triage_issue(
            settings=settings,
            db=database,
            github=lifecycle.backend,
            sandbox=lifecycle.sandbox,
            git_transport=lifecycle.sandbox.transport,
            payload=_issue_payload(lifecycle),
            delivery_id="triage-1",
        )

        triaged = database.get_issue(key)
        assert triaged is not None
        assert triaged.classification == "bug"
        assert triaged.branch is not None and triaged.branch.startswith("farm/")
        assert triaged.pr_number == 1
        assert triaged.session_dir is not None
        branch = triaged.branch
        session_dir = Path(triaged.session_dir)
        repo_dir = lifecycle.sandbox.workspace_root(_REPO, _ISSUE_NUMBER) / "repo"
        assert _check_exit_code(repo_dir) == 0
        _git(lifecycle.origin, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
        assert lifecycle.backend.pull_requests[1].head_ref == branch
        assert (session_dir / "triage.jsonl").exists()

        database.close()
        database = Database(lifecycle.db_path)
        restored = database.get_issue(key)
        assert restored is not None
        assert (restored.branch, restored.session_dir, restored.pr_number) == (branch, str(session_dir), 1)

        review_event = {
            "action": "created",
            "repository": _repository_payload(lifecycle),
            "pull_request": {"number": 1, "user": {"login": settings.bot_login}},
            "comment": {
                "user": {"login": "trusted-reviewer[bot]", "type": "Bot"},
                "author_association": "NONE",
                "body": "Keep the repaired check executable and passing.",
                "path": _CHECK,
                "line": 3,
            },
        }
        decision = route(
            "pull_request_review_comment",
            review_event,
            allowlist=frozenset({_REPO}),
            bot_login=settings.bot_login,
            reviewer_bots=frozenset({"trusted-reviewer"}),
            resolve_issue_from_pr=lambda repo, number: key if (repo, number) == (_REPO, 1) else None,
        )
        assert decision.should_queue and decision.task == "handle_review" and decision.directive

        remote_before_review = _git(lifecycle.origin, "rev-parse", f"refs/heads/{branch}")
        await tasks.handle_review(
            settings=settings,
            db=database,
            github=lifecycle.backend,
            sandbox=lifecycle.sandbox,
            git_transport=lifecycle.sandbox.transport,
            payload=review_event,
            delivery_id="review-1",
        )
        remote_after_review = _git(lifecycle.origin, "rev-parse", f"refs/heads/{branch}")
        assert remote_after_review != remote_before_review
        assert driver.review_branches == [branch]
        assert driver.review_sessions == [session_dir]
        assert _check_exit_code(repo_dir) == 0

        first_failure = _release_payload(
            lifecycle,
            lifecycle.initial_sha,
            subject=f"chore: bump version to {_VERSION}",
        )
        release_decision = route(
            "workflow_run",
            first_failure,
            allowlist=frozenset({_REPO}),
            bot_login=settings.bot_login,
            release_sentinel_enabled=True,
        )
        assert release_decision.should_queue and release_decision.task == "handle_release_ci"
        await tasks.handle_release_ci(
            settings=settings,
            db=database,
            github=lifecycle.backend,
            sandbox=lifecycle.sandbox,
            git_transport=lifecycle.sandbox.transport,
            payload=first_failure,
            delivery_id="release-1",
        )

        first_round = database.get_release(f"{_REPO}#{_TAG}")
        assert first_round is not None
        assert driver.release_rounds == [1]
        assert driver.release_sha is not None
        assert first_round.rounds == 1
        assert first_round.state == "awaiting_ci"
        assert first_round.current_sha == driver.release_sha == lifecycle.backend.tag_sha
        assert _git(lifecycle.origin, "rev-parse", "refs/heads/main") == driver.release_sha
        assert _git(lifecycle.origin, "rev-parse", f"refs/tags/{_TAG}") == driver.release_sha
        release_repo = lifecycle.sandbox.workspace_root(_REPO, "release") / "repo"
        assert _check_exit_code(release_repo) == 0

        second_failure = _release_payload(
            lifecycle,
            driver.release_sha,
            subject=f"chore: bump version to {_VERSION} repair failing check",
        )
        await tasks.handle_release_ci(
            settings=settings,
            db=database,
            github=lifecycle.backend,
            sandbox=lifecycle.sandbox,
            git_transport=lifecycle.sandbox.transport,
            payload=second_failure,
            delivery_id="release-2",
        )
        capped = database.get_release(f"{_REPO}#{_TAG}")
        assert capped is not None
        assert capped.rounds == 1
        assert capped.state == "failed"
        assert capped.last_error is not None and capped.last_error.startswith("round cap 1 reached")
        assert driver.release_rounds == [1]
    finally:
        database.close()
