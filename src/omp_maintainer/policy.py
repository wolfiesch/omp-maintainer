"""Repository-scoped, fail-closed automation policy.

A repository policy is authoritative only when it is read from the fetched
``origin/<default-branch>`` ref.  The prepared worktree is deliberately never a
policy source: it can contain untrusted changes made by the task being run.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from omp_maintainer.git_ops import _git_subprocess_env

_POLICY_PATH = ".github/omp-maintainer.yml"
_POLICY_VERSION = 1
_MISSING_POLICY_PATH = f"path '{_POLICY_PATH}' does not exist in "
_REF_DISALLOWED = re.compile(r"[\x00-\x20~^:?*\[\\]")


def _yaml_sequence_as_tuple(value: Any) -> Any:
    """Accept YAML sequences while retaining immutable, strict model fields."""
    if isinstance(value, list):
        return tuple(value)
    return value


def _valid_default_branch(value: object) -> bool:
    """Validate a short branch name before placing it in a git revision."""
    if not isinstance(value, str) or not value or value == "@" or "@{" in value:
        return False
    if value.startswith("/") or value.endswith("/") or "//" in value or ".." in value:
        return False
    if _REF_DISALLOWED.search(value):
        return False

    return all(
        component not in {"", ".", ".."}
        and not component.startswith(".")
        and not component.endswith(".")
        and not component.endswith(".lock")
        for component in value.split("/")
    )


class _StrictPolicyModel(BaseModel):
    """Base for policy documents: immutable and hostile to unrecognised input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class IssueAuthorizers(_StrictPolicyModel):
    roles: tuple[str, ...] = ("OWNER",)
    users: tuple[str, ...] = ()

    _coerce_yaml_sequences = field_validator("roles", "users", mode="before")(_yaml_sequence_as_tuple)


class IssuePolicy(_StrictPolicyModel):
    enabled: bool = True
    auto_implement: tuple[str, ...] = ("bug", "documentation")
    require_authorization: tuple[str, ...] = ("enhancement", "proposal", "feature")
    authorizers: IssueAuthorizers = Field(default_factory=IssueAuthorizers)

    _coerce_yaml_sequences = field_validator("auto_implement", "require_authorization", mode="before")(
        _yaml_sequence_as_tuple
    )


class PullRequestPolicy(_StrictPolicyModel):
    review_enabled: bool = True
    fix_comments_enabled: bool = True
    allowed_reviewer_bots: tuple[str, ...] = ("chatgpt-codex-connector",)
    steer_roles: tuple[str, ...] = ("OWNER", "MEMBER", "COLLABORATOR")

    _coerce_yaml_sequences = field_validator("allowed_reviewer_bots", "steer_roles", mode="before")(
        _yaml_sequence_as_tuple
    )


class LabelPolicy(_StrictPolicyModel):
    allowed_primaries: tuple[str, ...] = (
        "bug",
        "enhancement",
        "question",
        "proposal",
        "documentation",
        "wontfix",
        "invalid",
        "duplicate",
    )
    allowed_priorities: tuple[str, ...] = ("prio:p0", "prio:p1", "prio:p2", "prio:p3")
    allowed_functional: tuple[str, ...] = (
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
    allowed_platforms: tuple[str, ...] = (
        "platform:linux",
        "platform:macos",
        "platform:windows",
        "platform:wsl",
    )
    allowed_provider_prefix: str = "provider:"
    system_labels: tuple[str, ...] = ("triaged", "needs-info")

    _coerce_yaml_sequences = field_validator(
        "allowed_primaries",
        "allowed_priorities",
        "allowed_functional",
        "allowed_platforms",
        "system_labels",
        mode="before",
    )(_yaml_sequence_as_tuple)


class BranchPolicy(_StrictPolicyModel):
    allowed_prefixes: tuple[str, ...] = ("farm/",)
    review_prefix: str = "review/pr-"
    prohibit_default_branch_push: bool = True
    prohibit_tag_push: bool = True

    _coerce_yaml_sequences = field_validator("allowed_prefixes", mode="before")(_yaml_sequence_as_tuple)


class ReleaseSentinelPolicy(_StrictPolicyModel):
    enabled: bool = False
    max_rounds: int = Field(5, ge=1)
    commit_prefix: str = "chore: bump version to "
    allowed_tag_pattern: str = r"^v[0-9]+\.[0-9]+\.[0-9]+.*$"

    @field_validator("commit_prefix")
    @classmethod
    def _require_commit_prefix(cls, value: str) -> str:
        if not value:
            raise ValueError("commit_prefix must not be empty")
        return value

    @field_validator("allowed_tag_pattern")
    @classmethod
    def _require_valid_tag_pattern(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError("allowed_tag_pattern must be a valid regular expression") from exc
        return value


class CiRepairPolicy(_StrictPolicyModel):
    release_sentinel: ReleaseSentinelPolicy = Field(default_factory=lambda: ReleaseSentinelPolicy())  # type: ignore[call-arg]


class MergePolicy(_StrictPolicyModel):
    allow_merge: bool = False


class RepoPolicy(_StrictPolicyModel):
    """Versioned repository policy plus loader status for consumers to gate on."""

    version: Literal[1] = _POLICY_VERSION
    issues: IssuePolicy = Field(default_factory=IssuePolicy)
    pull_requests: PullRequestPolicy = Field(default_factory=PullRequestPolicy)
    labels: LabelPolicy = Field(default_factory=LabelPolicy)
    branches: BranchPolicy = Field(default_factory=BranchPolicy)
    ci_repair: CiRepairPolicy = Field(default_factory=CiRepairPolicy)
    merge: MergePolicy = Field(default_factory=MergePolicy)
    mutations_enabled: bool = Field(True, exclude=True)
    load_error: str | None = Field(None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_loader_metadata(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and {"mutations_enabled", "load_error"} & value.keys():
            raise ValueError("loader metadata cannot be configured in repository policy")
        return value


def default_repo_policy() -> RepoPolicy:
    """Return the conservative baseline used when a policy file is absent."""
    return RepoPolicy()  # type: ignore[call-arg]


def _fail_closed(load_error: str) -> RepoPolicy:
    return default_repo_policy().model_copy(update={"mutations_enabled": False, "load_error": load_error})


def _is_missing_policy_error(stderr: str) -> bool:
    normalized = stderr.casefold()
    return (
        _MISSING_POLICY_PATH in normalized or "exists on disk, but not in" in normalized and _POLICY_PATH in normalized
    )


def load_repo_policy(repo_dir: Path, default_branch: str) -> RepoPolicy:
    """Load the authoritative policy from ``origin/<default_branch>``.

    A missing policy file intentionally selects the strict built-in defaults. Any
    other git, YAML, or schema failure disables all mutations so callers can
    safely continue to inspect and report the repository without publishing.
    """
    if not _valid_default_branch(default_branch):
        return _fail_closed("invalid default branch")

    revision = f"refs/remotes/origin/{default_branch}:{_POLICY_PATH}"
    env = _git_subprocess_env()
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(repo_dir),
        }
    )
    try:
        result = subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(repo_dir), "show", revision],
            capture_output=True,
            check=False,
            text=True,
            env=env,
        )
    except (OSError, UnicodeError):
        return _fail_closed("unable to read policy from origin ref")

    if result.returncode != 0:
        if _is_missing_policy_error(result.stderr):
            return default_repo_policy()
        return _fail_closed("unable to read policy from origin ref")

    try:
        document = yaml.safe_load(result.stdout)
    except yaml.YAMLError:
        return _fail_closed("policy YAML is malformed")

    if not isinstance(document, Mapping):
        return _fail_closed("policy document must be a mapping")

    try:
        return RepoPolicy.model_validate(document)
    except (ValidationError, TypeError, ValueError, RecursionError):
        return _fail_closed("policy schema is invalid")
