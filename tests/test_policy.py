from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

import omp_maintainer.policy as policy_module
from omp_maintainer.policy import default_repo_policy, load_repo_policy


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    ).stdout


def _repository_with_origin_policy(tmp_path: Path, policy: str | None) -> Path:
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Policy Test")
    _git(repo, "config", "user.email", "policy-test@example.invalid")

    (repo / "README").write_text("fixture\n", encoding="utf-8")
    if policy is not None:
        policy_path = repo / ".github" / "omp-maintainer.yml"
        policy_path.parent.mkdir()
        policy_path.write_text(policy, encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial policy")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def test_default_policy_is_conservative() -> None:
    policy = default_repo_policy()

    assert policy.version == 1
    assert policy.mutations_enabled
    assert policy.load_error is None
    assert policy.issues.enabled
    assert policy.issues.auto_implement == ("bug", "documentation")
    assert policy.issues.require_authorization == ("enhancement", "proposal", "feature")
    assert policy.issues.authorizers.roles == ("OWNER",)
    assert policy.pull_requests.review_enabled
    assert policy.pull_requests.fix_comments_enabled
    assert policy.pull_requests.allowed_reviewer_bots == ("chatgpt-codex-connector",)
    assert policy.pull_requests.steer_roles == ("OWNER", "MEMBER", "COLLABORATOR")
    assert policy.labels.allowed_primaries == (
        "bug",
        "enhancement",
        "question",
        "proposal",
        "documentation",
        "wontfix",
        "invalid",
        "duplicate",
    )
    assert policy.labels.allowed_priorities == ("prio:p0", "prio:p1", "prio:p2", "prio:p3")
    assert policy.labels.allowed_functional == (
        "agent",
        "tool",
        "tui",
        "cli",
        "prompting",
        "sdk",
        "auth",
        "setup",
        "ux",
        "providers",
    )
    assert policy.labels.allowed_platforms == (
        "platform:linux",
        "platform:macos",
        "platform:windows",
        "platform:wsl",
    )
    assert policy.labels.allowed_provider_prefix == "provider:"
    assert policy.labels.system_labels == ("triaged", "needs-info")
    assert policy.branches.allowed_prefixes == ("farm/",)
    assert policy.branches.review_prefix == "review/pr-"
    assert policy.branches.prohibit_default_branch_push
    assert policy.branches.prohibit_tag_push
    assert not policy.ci_repair.release_sentinel.enabled
    assert policy.ci_repair.release_sentinel.max_rounds == 5
    assert policy.ci_repair.release_sentinel.commit_prefix == "chore: bump version to "
    assert policy.ci_repair.release_sentinel.allowed_tag_pattern == r"^v[0-9]+\.[0-9]+\.[0-9]+.*$"
    assert not policy.merge.allow_merge


def test_missing_origin_policy_uses_defaults(tmp_path: Path) -> None:
    repo = _repository_with_origin_policy(tmp_path, None)

    assert load_repo_policy(repo, "main") == default_repo_policy()


def test_policy_is_loaded_from_origin_ref_not_malicious_worktree(tmp_path: Path) -> None:
    repo = _repository_with_origin_policy(
        tmp_path,
        """\
version: 1
issues:
  enabled: false
""",
    )
    policy_path = repo / ".github" / "omp-maintainer.yml"
    policy_path.parent.mkdir(exist_ok=True)
    policy_path.write_text("version: 2\n", encoding="utf-8")

    policy = load_repo_policy(repo, "main")

    assert not policy.issues.enabled
    assert policy.mutations_enabled
    assert policy.load_error is None


@pytest.mark.parametrize(
    "document",
    [
        "version: 1\nunexpected: true\n",
        "version: 1\nissues:\n  enabled: 'false'\n",
    ],
)
def test_unknown_keys_and_schema_errors_fail_closed(tmp_path: Path, document: str) -> None:
    repo = _repository_with_origin_policy(tmp_path, document)

    policy = load_repo_policy(repo, "main")

    assert not policy.mutations_enabled
    assert policy.load_error == "policy schema is invalid"


def test_unsupported_policy_version_fails_closed(tmp_path: Path) -> None:
    repo = _repository_with_origin_policy(tmp_path, "version: 2\n")

    policy = load_repo_policy(repo, "main")

    assert not policy.mutations_enabled
    assert policy.load_error == "policy schema is invalid"


def test_invalid_default_branch_is_refused_without_invoking_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unexpected_git(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("git must not be invoked for an invalid branch")

    monkeypatch.setattr(policy_module.subprocess, "run", unexpected_git)

    policy = load_repo_policy(tmp_path, "main:refs/heads/protected")

    assert not policy.mutations_enabled
    assert policy.load_error == "invalid default branch"


def test_malformed_policy_yaml_fails_closed(tmp_path: Path) -> None:
    repo = _repository_with_origin_policy(tmp_path, "version: [\n")

    policy = load_repo_policy(repo, "main")

    assert not policy.mutations_enabled
    assert policy.load_error == "policy YAML is malformed"


def test_policy_loader_scrubs_service_secrets_and_disables_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omp_maintainer.git_ops import AUTH_ENV_VAR

    secret_keys = (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_WEBHOOK_SECRET",
        "OMP_MAINTAINER_REPLAY_TOKEN",
        "OMP_MAINTAINER_GH_PROXY_HMAC_KEY",
        "OMP_GITHUB_APP_PRIVATE_KEY",
        "OMP_GITHUB_APP_PRIVATE_KEY_PATH",
        AUTH_ENV_VAR,
    )
    for key in secret_keys:
        monkeypatch.setenv(key, f"parent-{key}")

    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 1, "", "fatal: unable to read policy")

    monkeypatch.setattr(policy_module.subprocess, "run", fake_run)

    loaded = load_repo_policy(tmp_path, "main")

    env = captured["env"]
    cmd = captured["cmd"]
    assert isinstance(env, dict)
    assert isinstance(cmd, list)
    assert all(key not in env for key in secret_keys)
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == str(tmp_path)
    assert "core.hooksPath=/dev/null" in cmd
    assert not loaded.mutations_enabled
