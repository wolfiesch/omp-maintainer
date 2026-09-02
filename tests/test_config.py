from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omp_maintainer.config import Settings, load_proxy_settings, reset_settings_cache


def test_settings_load_from_env(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.bot_login == "omp-maintainer-bot"
    assert cfg.repo_allowlist == frozenset({"octo/widget"})
    assert cfg.allows("octo/widget")
    assert cfg.allows("Octo/Widget")
    assert not cfg.allows("other/widget")
    assert cfg.task_max_cost_usd == 5.0
    assert cfg.release_max_cost_usd == 10.0
    assert cfg.task_max_tokens == 500_000


def test_settings_loads_direct_provider_credentials_and_preserves_provider_selection(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_PROVIDER", "openrouter")
    monkeypatch.setenv("OMP_MAINTAINER_MODEL", "openrouter/anthropic/claude-sonnet-4-6")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-api-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-api-key")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.internal:11434")
    reset_settings_cache()

    cfg = Settings()  # type: ignore[call-arg]

    assert cfg.provider == "openrouter"
    assert cfg.model == "openrouter/anthropic/claude-sonnet-4-6"
    assert cfg.anthropic_api_key is not None
    assert cfg.anthropic_api_key.get_secret_value() == "test-anthropic-api-key"
    assert cfg.openrouter_api_key is not None
    assert cfg.openrouter_api_key.get_secret_value() == "test-openrouter-api-key"
    assert cfg.ollama_base_url == "http://ollama.internal:11434"


def test_blank_direct_provider_environment_is_treated_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", " ")
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    reset_settings_cache()

    cfg = Settings()  # type: ignore[call-arg]

    assert cfg.openai_api_key is None
    assert cfg.openai_base_url is None


def test_usage_limits_describe_post_turn_sessionstats_cutoffs() -> None:
    expected_descriptions = {
        "task_max_cost_usd": (
            "Post-turn cumulative local SessionStats cost cutoff for a task. "
            "It is an inter-turn threshold, so one turn may overshoot before execution stops; "
            "it is not a hard or provider billing cap."
        ),
        "release_max_cost_usd": (
            "Post-turn cumulative local SessionStats cost cutoff for a release task. "
            "It is an inter-turn threshold, so one turn may overshoot before execution stops; "
            "it is not a hard or provider billing cap."
        ),
        "task_max_tokens": (
            "Post-turn cumulative local SessionStats token cutoff for a task. "
            "It is an inter-turn threshold, so one turn may overshoot before execution stops; "
            "it is not a hard provider token cap."
        ),
    }

    assert {name: Settings.model_fields[name].description for name in expected_descriptions} == expected_descriptions


def test_settings_requires_proxy_credentials(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_GH_PROXY_URL", "")
    monkeypatch.setenv("OMP_MAINTAINER_GH_PROXY_HMAC_KEY", "")
    reset_settings_cache()
    with pytest.raises(ValidationError, match="orchestrator requires"):
        Settings()  # type: ignore[call-arg]


def test_orchestrator_mode_loads_proxy_config(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.github_app_id is None
    assert cfg.github_app_private_key is None
    assert cfg.github_app_private_key_path is None
    assert cfg.gh_proxy_url == "http://github-broker.invalid:8081"
    assert cfg.gh_proxy_hmac_key is not None
    assert cfg.gh_proxy_hmac_key.get_secret_value().startswith("test-hmac-key")


def test_orchestrator_default_state_paths_use_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str]
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OMP_MAINTAINER_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OMP_MAINTAINER_SQLITE_PATH", raising=False)
    monkeypatch.delenv("OMP_MAINTAINER_LOG_DIR", raising=False)

    cfg = Settings()  # type: ignore[call-arg]

    state_root = tmp_path / "omp-maintainer"
    assert cfg.workspace_root == state_root / "workspaces"
    assert cfg.sqlite_path == state_root / "maintainer.sqlite"
    assert cfg.log_dir == state_root / "logs"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OMP_GITHUB_APP_ID", "123"),
        ("OMP_GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----"),
        ("OMP_GITHUB_APP_PRIVATE_KEY_PATH", "/run/secrets/github_app_private_key"),
    ],
)
def test_orchestrator_rejects_app_credentials(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    reset_settings_cache()
    with pytest.raises(ValidationError, match="GitHub App credentials are broker-only"):
        Settings()  # type: ignore[call-arg]


def test_rejects_proxy_url_without_key(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_GH_PROXY_HMAC_KEY", "")
    reset_settings_cache()
    with pytest.raises(ValidationError, match="must both be set"):
        Settings()  # type: ignore[call-arg]


def _configure_broker_app(monkeypatch: pytest.MonkeyPatch, *, private_key: str = "test-app-private-key") -> None:
    monkeypatch.setenv("OMP_GITHUB_APP_ID", "123")
    monkeypatch.setenv("OMP_GITHUB_APP_PRIVATE_KEY", private_key)
    monkeypatch.delenv("OMP_GITHUB_APP_PRIVATE_KEY_PATH", raising=False)


def test_broker_settings_load_from_env(proxy_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_broker_app(monkeypatch)
    cfg = load_proxy_settings()
    assert cfg.github_app_id == 123
    assert cfg.github_app_private_key is not None
    assert cfg.github_app_private_key.get_secret_value() == "test-app-private-key"
    assert cfg.github_app_private_key_path is None
    assert cfg.gh_proxy_hmac_key is not None
    assert cfg.gh_proxy_hmac_key.get_secret_value() == proxy_env["OMP_MAINTAINER_GH_PROXY_HMAC_KEY"]
    assert cfg.gh_proxy_bind_host == proxy_env["OMP_MAINTAINER_GH_PROXY_BIND_HOST"]
    assert cfg.gh_proxy_bind_port == int(proxy_env["OMP_MAINTAINER_GH_PROXY_BIND_PORT"])
    assert str(cfg.workspace_root) == proxy_env["OMP_MAINTAINER_WORKSPACE_ROOT"]
    assert str(cfg.log_dir) == proxy_env["OMP_MAINTAINER_LOG_DIR"]
    assert cfg.gh_proxy_max_body_bytes == int(proxy_env["OMP_MAINTAINER_GH_PROXY_MAX_BODY_BYTES"])
    assert cfg.gh_proxy_git_timeout_seconds == float(proxy_env["OMP_MAINTAINER_GH_PROXY_GIT_TIMEOUT_SECONDS"])


def test_broker_settings_exclude_maintainer_provider_configuration(
    proxy_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_broker_app(monkeypatch)
    monkeypatch.setenv("OMP_MAINTAINER_PROVIDER", "openai")
    monkeypatch.setenv("OMP_MAINTAINER_MODEL", "openai/gpt-5.4")

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-api-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example.invalid/v1")

    cfg = load_proxy_settings()

    assert cfg.provider is None
    assert cfg.model == "anthropic/claude-sonnet-4-6"
    assert cfg.openai_api_key is None
    assert cfg.openai_base_url is None


def test_broker_default_state_paths_use_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, proxy_env: dict[str, str]
) -> None:
    _configure_broker_app(monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OMP_MAINTAINER_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OMP_MAINTAINER_SQLITE_PATH", raising=False)
    monkeypatch.delenv("OMP_MAINTAINER_LOG_DIR", raising=False)

    cfg = load_proxy_settings()

    state_root = tmp_path / "omp-maintainer"
    assert cfg.workspace_root == state_root / "workspaces"
    assert cfg.sqlite_path == state_root / "maintainer.sqlite"
    assert cfg.log_dir == state_root / "logs"


@pytest.mark.parametrize(
    ("private_key", "private_key_path"),
    [
        (None, None),
        ("", None),
        ("test-app-private-key", "/run/secrets/github_app_private_key"),
    ],
)
def test_broker_requires_exactly_one_private_key_source(
    monkeypatch: pytest.MonkeyPatch,
    proxy_env: dict[str, str],
    private_key: str | None,
    private_key_path: str | None,
) -> None:
    monkeypatch.setenv("OMP_GITHUB_APP_ID", "123")
    if private_key is None:
        monkeypatch.delenv("OMP_GITHUB_APP_PRIVATE_KEY", raising=False)
    else:
        monkeypatch.setenv("OMP_GITHUB_APP_PRIVATE_KEY", private_key)
    if private_key_path is None:
        monkeypatch.delenv("OMP_GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    else:
        monkeypatch.setenv("OMP_GITHUB_APP_PRIVATE_KEY_PATH", private_key_path)

    with pytest.raises(ValidationError, match="exactly one"):
        load_proxy_settings()


def test_broker_requires_positive_app_id(proxy_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_broker_app(monkeypatch)
    monkeypatch.setenv("OMP_GITHUB_APP_ID", "0")

    with pytest.raises(ValidationError, match="greater than 0"):
        load_proxy_settings()


def test_broker_requires_hmac(proxy_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_broker_app(monkeypatch)
    monkeypatch.setenv("OMP_MAINTAINER_GH_PROXY_HMAC_KEY", "")

    with pytest.raises(ValidationError, match="non-empty"):
        load_proxy_settings()


def test_broker_loads_private_key_from_path(
    proxy_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private_key_path = tmp_path / "github-app.pem"
    private_key_path.write_text("test-app-private-key", encoding="utf-8")
    monkeypatch.setenv("OMP_GITHUB_APP_ID", "123")
    monkeypatch.delenv("OMP_GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("OMP_GITHUB_APP_PRIVATE_KEY_PATH", str(private_key_path))

    cfg = load_proxy_settings()

    assert cfg.github_app_private_key is not None
    assert cfg.github_app_private_key.get_secret_value() == "test-app-private-key"
    assert cfg.github_app_private_key_path is None


def test_broker_rejects_unreadable_private_key_path(
    proxy_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMP_GITHUB_APP_ID", "123")
    monkeypatch.delenv("OMP_GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("OMP_GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "missing-private-key.pem"))

    with pytest.raises(ValueError, match="readable private-key file"):
        load_proxy_settings()


def test_allowlist_csv_parsing(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_REPO_ALLOWLIST", "  alpha/one ,beta/two, ,gamma/three ")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.repo_allowlist == frozenset({"alpha/one", "beta/two", "gamma/three"})


def test_blank_replay_token_treated_as_disabled(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_REPLAY_TOKEN", "")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.replay_token is None


def test_whitespace_replay_token_treated_as_disabled(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_REPLAY_TOKEN", "   ")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.replay_token is None


def test_real_replay_token_preserved(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_REPLAY_TOKEN", "abc")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.replay_token is not None
    assert cfg.replay_token.get_secret_value() == "abc"


def test_blank_bot_login_rejected(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_BOT_LOGIN", "   ")
    reset_settings_cache()
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "raw_login",
    [
        "omp-maintainer",
        " @omp-maintainer ",
        " @OMP-MAINTAINER ",
        "omp-maintainer[bot]",
        "@omp-maintainer[bot]",
        " @OMP-MAINTAINER[BOT] ",
    ],
)
def test_bot_login_normalizes_mention_case_and_app_suffix(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], raw_login: str
) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_BOT_LOGIN", raw_login)
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.bot_login == "omp-maintainer"


def test_maintainer_logins_normalize_csv_entries(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_MAINTAINER_LOGINS", " can1357, @OMP-MAINTAINER , @Alice[bot] ,, ")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.maintainer_logins == frozenset({"can1357", "omp-maintainer", "alice"})


@pytest.mark.parametrize(
    ("raw_login", "expected"),
    [
        ("omp-maintainer", "omp-maintainer"),
        (" @omp-maintainer ", "omp-maintainer"),
        (" @OMP-MAINTAINER ", "omp-maintainer"),
        ("omp-maintainer[bot]", "omp-maintainer"),
        ("@omp-maintainer[bot]", "omp-maintainer"),
        (" @OMP-MAINTAINER[BOT] ", "omp-maintainer"),
    ],
)
def test_maintainer_logins_common_entry_forms(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], raw_login: str, expected: str
) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_MAINTAINER_LOGINS", raw_login)
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.maintainer_logins == frozenset({expected})


def test_model_pool_single(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.model_pool == (cfg.model,)
    assert cfg.pick_model() == cfg.model


def test_model_pool_csv_parses(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv(
        "OMP_MAINTAINER_MODEL",
        " codex/gpt-5.4 , anthropic/claude-sonnet-4-6 ,, anthropic/claude-opus-4-7 ",
    )
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.model_pool == (
        "codex/gpt-5.4",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-opus-4-7",
    )


def test_pick_model_covers_full_pool(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    """With a 3-item pool and 500 picks, each option appears at least once."""
    monkeypatch.setenv("OMP_MAINTAINER_MODEL", "a,b,c")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    seen = {cfg.pick_model() for _ in range(500)}
    assert seen == {"a", "b", "c"}


def test_release_model_falls_back_to_general_pool(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_MODEL", "a")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.release_model_pool == ("a",)
    assert cfg.pick_release_model() == "a"


def test_release_model_pool_csv_parses(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_MODEL", "fallback")
    monkeypatch.setenv("OMP_MAINTAINER_RELEASE_MODEL", " release-a, release-b ,, ")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.release_model_pool == ("release-a", "release-b")


def test_max_concurrency_default_is_8(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.max_concurrency == 8


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OMP_MAINTAINER_TASK_MAX_COST_USD", "0"),
        ("OMP_MAINTAINER_TASK_MAX_COST_USD", "-1"),
        ("OMP_MAINTAINER_RELEASE_MAX_COST_USD", "0"),
        ("OMP_MAINTAINER_RELEASE_MAX_COST_USD", "-1"),
        ("OMP_MAINTAINER_TASK_MAX_TOKENS", "0"),
        ("OMP_MAINTAINER_TASK_MAX_TOKENS", "-1"),
    ],
)
def test_budget_limits_must_be_strictly_positive(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    reset_settings_cache()
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_task_timeout_hard_grace_env_parses(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("OMP_MAINTAINER_TASK_TIMEOUT_HARD_GRACE_SECONDS", "12.5")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.task_timeout_hard_grace_seconds == 12.5
