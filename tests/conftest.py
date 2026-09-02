"""Common pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from omp_maintainer.config import Settings, reset_settings_cache
from omp_maintainer.dashboard import reset_index_cache, static_dir
from omp_maintainer.db import Database, close_database

# Minimum HTML the dashboard handler needs to render: `<title>` plus a script
# block carrying the `__OMP_MAINTAINER_CONFIG__` sentinel. The real Vite-built bundle
# adds JS/CSS asset links; tests only care about the rendering contract.
_PLACEHOLDER_INDEX_HTML = (
    "<!doctype html>\n"
    '<html lang="en">\n'
    '  <head><meta charset="utf-8"><title>OMP Maintainer</title></head>\n'
    "  <body>\n"
    '    <div id="app"></div>\n'
    '    <script id="omp-maintainer-config" type="application/json">__OMP_MAINTAINER_CONFIG__</script>\n'
    "  </body>\n"
    "</html>\n"
)


@pytest.fixture(autouse=True, scope="session")
def _ensure_dashboard_bundle() -> None:
    """Guarantee a renderable dashboard bundle for the whole session.

    The real bundle is produced by `bun --cwd web run build`; CI and fresh clones
    might not have run it yet. We only synthesise an `index.html` when one
    isn't already present, so a developer's locally-built bundle isn't
    clobbered by the test run.
    """
    directory = static_dir()
    index = directory / "index.html"
    if not index.exists():
        index.write_text(_PLACEHOLDER_INDEX_HTML, encoding="utf-8")
    reset_index_cache()


@pytest.fixture(autouse=True)
def _open_tmp_path_for_slot_traversal(tmp_path: Path) -> None:
    """Grant traverse (`+x`) on tmp_path's root-owned ancestors so slot
    subprocesses can reach the workspace.

    pytest's default ``tmp_path`` lives under ``/tmp/pytest-of-<user>/`` with
    mode ``0700``. On macOS dev that's irrelevant (no slot subprocess ever
    drops uid). On Linux+root the slot UID (e.g. 2001) is non-zero and
    every directory between ``/`` and the workspace needs at least the
    `o+x` bit or the slot's stat fails with EACCES. Adds `o+x` (NOT `o+r`)
    so directory contents stay private; only path-traversal is allowed.
    """
    import os
    import platform
    import stat

    if platform.system() != "Linux" or os.geteuid() != 0:
        return
    cursor = tmp_path.resolve()
    while cursor != cursor.parent:
        try:
            st = cursor.stat()
        except FileNotFoundError:
            break
        if not stat.S_ISDIR(st.st_mode):
            break
        if not (st.st_mode & 0o001):
            try:
                cursor.chmod(st.st_mode | 0o001)
            except PermissionError:
                break
        cursor = cursor.parent


def _baseline_env(tmp_path: Path) -> dict[str, str]:
    return {
        # Orchestrator mode: no PAT in this container; talk to github-broker instead.
        "OMP_MAINTAINER_GH_PROXY_URL": "http://github-broker.invalid:8081",
        "OMP_MAINTAINER_GH_PROXY_HMAC_KEY": "test-hmac-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "GITHUB_WEBHOOK_SECRET": "test-webhook-secret",
        "OMP_MAINTAINER_BOT_LOGIN": "omp-maintainer-bot",
        "OMP_MAINTAINER_GIT_AUTHOR_NAME": "omp-maintainer-bot",
        "OMP_MAINTAINER_GIT_AUTHOR_EMAIL": "omp-maintainer-bot@example.invalid",
        "OMP_MAINTAINER_REPO_ALLOWLIST": "octo/widget",
        "OMP_MAINTAINER_MODEL": "anthropic/claude-sonnet-4-5",
        "OMP_MAINTAINER_THINKING": "high",
        "OMP_MAINTAINER_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "OMP_MAINTAINER_SQLITE_PATH": str(tmp_path / "maintainer.sqlite"),
        "OMP_MAINTAINER_LOG_DIR": str(tmp_path / "logs"),
        # Same reasoning for the issue-index reconciler: its first tick would
        # spin connect-retries against the .invalid proxy URL inside server
        # tests. Tests that want it construct IssueIndexSync directly.
        "OMP_MAINTAINER_ISSUE_INDEX_SYNC_SECONDS": "0",
    }


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    env = _baseline_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Defensive: a stray `.env` or shell export must not flip us into PAT mode.
    # `monkeypatch.delenv` would let pydantic_settings fall back to the .env
    # file; setenv("") is what actually shadows the file value, and the
    # `_blank_token_disables` validator treats empty strings as unset.
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.delenv("OMP_MAINTAINER_PROVIDER", raising=False)
    monkeypatch.setenv("OMP_MAINTAINER_REPLAY_TOKEN", "")
    reset_settings_cache()
    yield env
    reset_settings_cache()
    close_database()


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """Environment consumed by the standalone github-broker process."""
    broker_env = {
        "GITHUB_TOKEN": "ghp_test_token_value_xxxxxxxxxxxxxxxx",
        "OMP_MAINTAINER_GH_PROXY_HMAC_KEY": "test-hmac-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "OMP_MAINTAINER_GH_PROXY_BIND_HOST": "127.0.0.1",
        "OMP_MAINTAINER_GH_PROXY_BIND_PORT": "9081",
        "OMP_MAINTAINER_WORKSPACE_ROOT": str(tmp_path / "broker-workspaces"),
        "OMP_MAINTAINER_LOG_DIR": str(tmp_path / "broker-logs"),
        "OMP_MAINTAINER_GH_PROXY_MAX_BODY_BYTES": "262144",
        "OMP_MAINTAINER_GH_PROXY_GIT_TIMEOUT_SECONDS": "17.5",
    }
    for key, value in broker_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OMP_MAINTAINER_GH_PROXY_URL", raising=False)
    reset_settings_cache()
    yield broker_env
    reset_settings_cache()
    close_database()


@pytest.fixture
def settings(env: dict[str, str]) -> Settings:
    cfg = Settings()  # type: ignore[call-arg]
    cfg.ensure_paths()
    return cfg


@pytest.fixture
def db(tmp_path: Path) -> Database:
    path = tmp_path / "test.sqlite"
    database = Database(path)
    yield database
    database.close()
