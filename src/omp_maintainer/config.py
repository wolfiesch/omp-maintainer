"""Env-driven configuration for OMP Maintainer."""

from __future__ import annotations

import os
import random
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ThinkingLevel = Literal["off", "low", "medium", "high", "xhigh", "max"]


def _default_state_root() -> Path:
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "omp-maintainer"
    return Path.home() / ".local" / "state" / "omp-maintainer"


class Settings(BaseSettings):
    """Strongly-typed runtime configuration.

    Loaded from process env, optionally pre-populated by `.env`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # GitHub App credentials are broker-only. `Settings()` rejects them for
    # the orchestrator; `load_proxy_settings()` resolves them inside the
    # broker and uses `model_construct` to keep private material out of the
    # orchestrator's validation path.
    github_app_id: int | None = Field(None, alias="OMP_GITHUB_APP_ID")
    github_app_private_key: SecretStr | None = Field(None, alias="OMP_GITHUB_APP_PRIVATE_KEY")
    github_app_private_key_path: Path | None = Field(None, alias="OMP_GITHUB_APP_PRIVATE_KEY_PATH")
    github_webhook_secret: SecretStr = Field(..., alias="GITHUB_WEBHOOK_SECRET")
    bot_login: str = Field(..., alias="OMP_MAINTAINER_BOT_LOGIN")
    git_author_name: str | None = Field(None, alias="OMP_MAINTAINER_GIT_AUTHOR_NAME")
    git_author_email: str = Field(..., alias="OMP_MAINTAINER_GIT_AUTHOR_EMAIL")
    repo_allowlist_raw: str = Field("", alias="OMP_MAINTAINER_REPO_ALLOWLIST")
    pr_review_enabled: bool = Field(True, alias="OMP_MAINTAINER_PR_REVIEW_ENABLED")

    # Release sentinel
    release_sentinel_enabled: bool = Field(False, alias="OMP_MAINTAINER_RELEASE_SENTINEL_ENABLED")
    release_commit_prefix: str = Field("chore: bump version to ", alias="OMP_MAINTAINER_RELEASE_COMMIT_PREFIX")
    release_max_rounds: int = Field(5, alias="OMP_MAINTAINER_RELEASE_MAX_ROUNDS")
    release_max_cost_usd: float = Field(
        10.0,
        gt=0,
        alias="OMP_MAINTAINER_RELEASE_MAX_COST_USD",
        description=(
            "Post-turn cumulative local SessionStats cost cutoff for a release task. "
            "It is an inter-turn threshold, so one turn may overshoot before execution stops; "
            "it is not a hard or provider billing cap."
        ),
    )
    release_task_timeout_seconds: float = Field(3600.0, alias="OMP_MAINTAINER_RELEASE_TASK_TIMEOUT_SECONDS")
    release_model: str | None = Field(None, alias="OMP_MAINTAINER_RELEASE_MODEL")

    # GitHub broker. The orchestrator always routes GitHub through this
    # authenticated broker and never receives App credentials.
    gh_proxy_url: str | None = Field(None, alias="OMP_MAINTAINER_GH_PROXY_URL")
    gh_proxy_hmac_key: SecretStr | None = Field(None, alias="OMP_MAINTAINER_GH_PROXY_HMAC_KEY")
    # Bind address for `python -m omp_maintainer.proxy serve`. Internal-only by
    # default; github-broker never exposes a host port.
    gh_proxy_bind_host: str = Field("0.0.0.0", alias="OMP_MAINTAINER_GH_PROXY_BIND_HOST")
    gh_proxy_bind_port: int = Field(8081, alias="OMP_MAINTAINER_GH_PROXY_BIND_PORT")

    # GitHub broker maximum request body size (bytes). Bodies larger than this
    # are rejected with 413 BEFORE the broker reads them into memory. Tight by
    # design — every typed endpoint payload fits in a few KB.
    gh_proxy_max_body_bytes: int = Field(1 << 20, alias="OMP_MAINTAINER_GH_PROXY_MAX_BODY_BYTES")
    # Hard wall-clock budget (seconds) for a single git subprocess invoked by
    # github-broker. Bounds how long a hung git can pin a request handler.
    gh_proxy_git_timeout_seconds: float = Field(60.0, alias="OMP_MAINTAINER_GH_PROXY_GIT_TIMEOUT_SECONDS")

    # Model selection
    model: str = Field("anthropic/claude-sonnet-4-6", alias="OMP_MAINTAINER_MODEL")
    provider: str | None = Field(None, alias="OMP_MAINTAINER_PROVIDER")
    # Optional direct OMP provider credentials and endpoint configuration.
    # These values are inherited by OMP task processes, so only configure them
    # for repositories and tasks trusted to access the selected provider.
    anthropic_api_key: SecretStr | None = Field(None, alias="ANTHROPIC_API_KEY")
    anthropic_oauth_token: SecretStr | None = Field(None, alias="ANTHROPIC_OAUTH_TOKEN")
    openai_api_key: SecretStr | None = Field(None, alias="OPENAI_API_KEY")
    openai_codex_oauth_token: SecretStr | None = Field(None, alias="OPENAI_CODEX_OAUTH_TOKEN")
    gemini_api_key: SecretStr | None = Field(None, alias="GEMINI_API_KEY")
    groq_api_key: SecretStr | None = Field(None, alias="GROQ_API_KEY")
    cerebras_api_key: SecretStr | None = Field(None, alias="CEREBRAS_API_KEY")
    deepseek_api_key: SecretStr | None = Field(None, alias="DEEPSEEK_API_KEY")
    together_api_key: SecretStr | None = Field(None, alias="TOGETHER_API_KEY")
    openrouter_api_key: SecretStr | None = Field(None, alias="OPENROUTER_API_KEY")
    mistral_api_key: SecretStr | None = Field(None, alias="MISTRAL_API_KEY")
    xai_api_key: SecretStr | None = Field(None, alias="XAI_API_KEY")
    openai_base_url: str | None = Field(None, alias="OPENAI_BASE_URL")
    ollama_base_url: str | None = Field(None, alias="OLLAMA_BASE_URL")
    lm_studio_base_url: str | None = Field(None, alias="LM_STUDIO_BASE_URL")
    llama_cpp_base_url: str | None = Field(None, alias="LLAMA_CPP_BASE_URL")

    thinking_level: ThinkingLevel = Field("high", alias="OMP_MAINTAINER_THINKING")

    # Runtime
    max_concurrency: int = Field(8, alias="OMP_MAINTAINER_MAX_CONCURRENCY")
    task_timeout_seconds: float = Field(2400.0, alias="OMP_MAINTAINER_TASK_TIMEOUT_SECONDS")
    task_timeout_hard_grace_seconds: float = Field(60.0, alias="OMP_MAINTAINER_TASK_TIMEOUT_HARD_GRACE_SECONDS")
    request_timeout_seconds: float = Field(120.0, alias="OMP_MAINTAINER_REQUEST_TIMEOUT_SECONDS")
    task_max_cost_usd: float = Field(
        5.0,
        gt=0,
        alias="OMP_MAINTAINER_TASK_MAX_COST_USD",
        description=(
            "Post-turn cumulative local SessionStats cost cutoff for a task. "
            "It is an inter-turn threshold, so one turn may overshoot before execution stops; "
            "it is not a hard or provider billing cap."
        ),
    )
    task_max_tokens: int = Field(
        500_000,
        gt=0,
        alias="OMP_MAINTAINER_TASK_MAX_TOKENS",
        description=(
            "Post-turn cumulative local SessionStats token cutoff for a task. "
            "It is an inter-turn threshold, so one turn may overshoot before execution stops; "
            "it is not a hard provider token cap."
        ),
    )

    # Automatic retry of transiently-failed events. When an event handler
    # raises (and it isn't an operator cancel or a shutdown interrupt), the
    # dispatcher re-queues the delivery with escalating backoff instead of
    # giving up, so ephemeral failures (git fetch timeouts, upstream 5xx/429,
    # flaky RPC startup) self-heal. After `event_max_retries` retries the row
    # stays `failed`. `event_retry_delays_seconds` is a comma-separated backoff
    # schedule: the Nth retry waits the Nth value (last value repeats), jittered.
    # Set `event_max_retries=0` to restore fail-fast behavior.
    event_max_retries: int = Field(3, alias="OMP_MAINTAINER_EVENT_MAX_RETRIES")
    event_retry_delays_raw: str = Field("30,120,600", alias="OMP_MAINTAINER_EVENT_RETRY_DELAYS_SECONDS")
    # Premature-end reminder. When a `triage_issue` turn ends without the
    # agent having reached a terminal tool (`gh_open_pr`,
    # `mark_unable_to_reproduce`, `abort_task`) for a `bug`/`documentation`
    # classification, the driver sends up to this many "you stopped before
    # opening a PR — continue" reminder prompts into the same omp session.
    # Set to 0 to disable.
    task_completion_max_reminders: int = Field(2, alias="OMP_MAINTAINER_TASK_COMPLETION_MAX_REMINDERS")
    omp_command: str = Field("omp", alias="OMP_MAINTAINER_OMP_COMMAND")

    # Graceful shutdown (Phase B). On SIGTERM the dispatcher stops claiming
    # new work, then waits up to `drain` seconds for in-flight events to
    # complete cleanly; any still running after that get their omp
    # subprocess killed and the row left in `running` so it requeues on
    # next start. Sum of both MUST stay below the compose `stop_grace_period`.
    shutdown_drain_timeout_seconds: float = Field(25.0, alias="OMP_MAINTAINER_SHUTDOWN_DRAIN_TIMEOUT_SECONDS")
    shutdown_kill_timeout_seconds: float = Field(5.0, alias="OMP_MAINTAINER_SHUTDOWN_KILL_TIMEOUT_SECONDS")

    # Paths
    workspace_root: Path = Field(
        default_factory=lambda: _default_state_root() / "workspaces",
        alias="OMP_MAINTAINER_WORKSPACE_ROOT",
    )
    sqlite_path: Path = Field(
        default_factory=lambda: _default_state_root() / "maintainer.sqlite",
        alias="OMP_MAINTAINER_SQLITE_PATH",
    )
    log_dir: Path = Field(default_factory=lambda: _default_state_root() / "logs", alias="OMP_MAINTAINER_LOG_DIR")

    # Server
    bind_host: str = Field("0.0.0.0", alias="OMP_MAINTAINER_BIND_HOST")
    bind_port: int = Field(8080, alias="OMP_MAINTAINER_BIND_PORT")

    # Dev-only replay header value; if empty, /replay is disabled
    replay_token: SecretStr | None = Field(None, alias="OMP_MAINTAINER_REPLAY_TOKEN")

    # Per-submitter rate limiting. `window_seconds` defines the rolling window;
    # `default` is the per-window cap for unknown/first-time submitters;
    # `contributor` is the cap for accounts whose GitHub author_association is
    # `CONTRIBUTOR` (i.e. already has a merged PR). `unlimited_raw` is a
    # comma-separated allowlist of logins that bypass the limiter entirely;
    # accounts with author_association OWNER/MEMBER/COLLABORATOR also bypass.
    rate_limit_window_seconds: float = Field(3600.0, alias="OMP_MAINTAINER_RATE_LIMIT_WINDOW_SECONDS")
    rate_limit_default: int = Field(3, alias="OMP_MAINTAINER_RATE_LIMIT_DEFAULT")
    rate_limit_contributor: int = Field(10, alias="OMP_MAINTAINER_RATE_LIMIT_CONTRIBUTOR")
    rate_limit_unlimited_raw: str = Field("", alias="OMP_MAINTAINER_RATE_LIMIT_UNLIMITED")
    # Logins (comma-separated, `@` prefix optional, case-insensitive) whose `@bot_login`
    # mentions are treated as authoritative directives. These accounts also
    # bypass rate limiting regardless of `author_association`.
    maintainer_logins_raw: str = Field("", alias="OMP_MAINTAINER_MAINTAINER_LOGINS")
    # Bot logins (e.g. chatgpt-codex-connector) whose comments/reviews are
    # treated as authoritative directives without requiring an `@bot` mention.
    # Comma-separated; `@` prefix optional.
    reviewer_bots_raw: str = Field("", alias="OMP_MAINTAINER_REVIEWER_BOTS")

    # Question auto-close. When the bot answers an issue classified as
    # `question`, the comment is suffixed with a 👎-to-keep-open prompt and a
    # row is scheduled in `pending_closures`. The scheduler closes the issue
    # after `question_autoclose_hours` unless the issue author downvoted the
    # comment, a human follow-up arrived, or the issue was closed externally.
    # Set `question_autoclose_enabled=False` (or hours <= 0) to disable.
    question_autoclose_enabled: bool = Field(True, alias="OMP_MAINTAINER_QUESTION_AUTOCLOSE_ENABLED")
    question_autoclose_hours: float = Field(4.0, alias="OMP_MAINTAINER_QUESTION_AUTOCLOSE_HOURS")
    question_autoclose_scan_seconds: float = Field(60.0, alias="OMP_MAINTAINER_QUESTION_AUTOCLOSE_SCAN_SECONDS")
    # Local issue/PR search index. Webhooks keep it fresh in real time; this
    # interval controls the periodic GitHub reconcile (first pass = full
    # backfill of every allowlisted repo). <= 0 disables the reconciler —
    # `gh_search_issues` then falls back to the remote search API until the
    # repo has a sync watermark.
    issue_index_sync_seconds: float = Field(900.0, alias="OMP_MAINTAINER_ISSUE_INDEX_SYNC_SECONDS")

    # Post-run workspace cache reclamation. Dependency directories such as
    # `node_modules` and workspace-private Bun caches are dead weight between
    # runs and can consume multiple GB per issue. When enabled, the worker
    # strips them after every event and WorkerPool.start() sweeps all
    # workspaces once at boot.
    reclaim_workspace_caches: bool = Field(True, alias="OMP_MAINTAINER_RECLAIM_WORKSPACE_CACHES")

    @field_validator(
        "anthropic_api_key",
        "anthropic_oauth_token",
        "openai_api_key",
        "openai_codex_oauth_token",
        "gemini_api_key",
        "groq_api_key",
        "cerebras_api_key",
        "deepseek_api_key",
        "together_api_key",
        "openrouter_api_key",
        "mistral_api_key",
        "xai_api_key",
        "openai_base_url",
        "ollama_base_url",
        "lm_studio_base_url",
        "llama_cpp_base_url",
        mode="before",
    )
    @classmethod
    def _blank_provider_value_is_missing(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("bot_login", mode="after")
    @classmethod
    def _require_bot_login(cls, value: str) -> str:
        cleaned = value.strip().removeprefix("@").lower()
        if cleaned.endswith("[bot]"):
            cleaned = cleaned[:-5]
        if not cleaned:
            raise ValueError("OMP_MAINTAINER_BOT_LOGIN must be a non-empty GitHub login")
        return cleaned

    @field_validator("replay_token", mode="before")
    @classmethod
    def _blank_replay_disables(cls, value: object) -> object:
        # Treat empty/whitespace strings as 'disabled'. Without this, an empty
        # OMP_MAINTAINER_REPLAY_TOKEN becomes SecretStr("") which the server would
        # happily compare against an empty X-OMP-Maintainer-Replay-Token header.
        if isinstance(value, str) and not value.strip():
            return None
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                return None
        return value

    @field_validator("gh_proxy_url", mode="before")
    @classmethod
    def _blank_proxy_url_disables(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("gh_proxy_hmac_key", mode="before")
    @classmethod
    def _blank_proxy_key_disables(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                return None
        return value

    @model_validator(mode="after")
    def _validate_orchestrator_proxy(self) -> Settings:
        """Require authenticated broker access and reject broker-only secrets."""
        if any(
            value is not None
            for value in (self.github_app_id, self.github_app_private_key, self.github_app_private_key_path)
        ):
            raise ValueError(
                "GitHub App credentials are broker-only; remove OMP_GITHUB_APP_ID, "
                "OMP_GITHUB_APP_PRIVATE_KEY, and OMP_GITHUB_APP_PRIVATE_KEY_PATH from the orchestrator."
            )
        has_url = bool(self.gh_proxy_url)
        has_key = self.gh_proxy_hmac_key is not None
        if has_url != has_key:
            raise ValueError("OMP_MAINTAINER_GH_PROXY_URL and OMP_MAINTAINER_GH_PROXY_HMAC_KEY must both be set.")
        if not has_url:
            raise ValueError(
                "OMP Maintainer orchestrator requires OMP_MAINTAINER_GH_PROXY_URL and OMP_MAINTAINER_GH_PROXY_HMAC_KEY."
            )
        return self

    @field_validator("repo_allowlist_raw", mode="before")
    @classmethod
    def _coerce_allowlist(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @property
    def repo_allowlist(self) -> frozenset[str]:
        items = [piece.strip().lower() for piece in self.repo_allowlist_raw.split(",")]
        return frozenset(item for item in items if item)

    @field_validator("rate_limit_unlimited_raw", mode="before")
    @classmethod
    def _coerce_unlimited(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @property
    def rate_limit_unlimited(self) -> frozenset[str]:
        items = [piece.strip().lstrip("@").lower() for piece in self.rate_limit_unlimited_raw.split(",")]
        return frozenset(item for item in items if item)

    @field_validator("maintainer_logins_raw", mode="before")
    @classmethod
    def _coerce_maintainers(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @field_validator("reviewer_bots_raw", mode="before")
    @classmethod
    def _coerce_reviewer_bots(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @property
    def reviewer_bots(self) -> frozenset[str]:
        items = [piece.strip().lstrip("@").lower() for piece in self.reviewer_bots_raw.split(",")]
        return frozenset(item for item in items if item)

    @property
    def maintainer_logins(self) -> frozenset[str]:
        items = [
            piece.strip().lstrip("@").lower().removesuffix("[bot]") for piece in self.maintainer_logins_raw.split(",")
        ]
        return frozenset(item for item in items if item)

    def allows(self, full_name: str) -> bool:
        return full_name.lower() in self.repo_allowlist

    @property
    def model_pool(self) -> tuple[str, ...]:
        """OMP_MAINTAINER_MODEL may be a single id or a comma-separated list; this
        returns the parsed pool (always non-empty)."""
        items = [piece.strip() for piece in self.model.split(",") if piece.strip()]
        return tuple(items) or (self.model,)

    def pick_model(self) -> str:
        """Random selection from the pool (uniform). One-element pools return that one."""
        return random.choice(self.model_pool)

    @property
    def release_model_pool(self) -> tuple[str, ...]:
        """Release-specific model pool, falling back to the general pool."""
        items = [piece.strip() for piece in (self.release_model or "").split(",") if piece.strip()]
        return tuple(items) or self.model_pool

    def pick_release_model(self) -> str:
        """Select a release model, falling back to the general selector."""
        if not self.release_model or not self.release_model.strip():
            return self.pick_model()
        return random.choice(self.release_model_pool)

    @field_validator("event_retry_delays_raw", mode="before")
    @classmethod
    def _coerce_retry_delays(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @property
    def event_retry_delays(self) -> tuple[float, ...]:
        """Parsed backoff schedule in seconds; always non-empty."""
        vals: list[float] = []
        for piece in self.event_retry_delays_raw.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                seconds = float(piece)
            except ValueError:
                continue
            if seconds >= 0:
                vals.append(seconds)
        return tuple(vals) or (30.0,)

    def retry_delay_seconds(self, retry_index: int) -> float:
        """Backoff before the `retry_index`-th retry (1-based), with jitter.

        Clamps to the last configured delay; applies ±20% jitter so a
        fleet-wide outage doesn't replay every event in lockstep.
        """
        delays = self.event_retry_delays
        idx = min(max(retry_index, 1), len(delays)) - 1
        return delays[idx] * (0.8 + random.random() * 0.4)

    @property
    def resolved_author_name(self) -> str:
        """Falls back to bot_login if OMP_MAINTAINER_GIT_AUTHOR_NAME isn't set."""
        return (self.git_author_name or self.bot_login).strip()

    def ensure_paths(self) -> None:
        for path in (self.workspace_root, self.sqlite_path.parent, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


@cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    """Invalidate the cached settings (tests)."""
    get_settings.cache_clear()


class _ProxyEnvLoader(BaseSettings):
    """Minimal env loader for `python -m omp_maintainer.proxy serve`.

    Reads only broker settings. Its key path is deliberately resolved by
    `load_proxy_settings()` in the broker process, never in the orchestrator.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    github_app_id: int = Field(..., gt=0, alias="OMP_GITHUB_APP_ID")
    github_app_private_key: SecretStr | None = Field(None, alias="OMP_GITHUB_APP_PRIVATE_KEY")
    github_app_private_key_path: Path | None = Field(None, alias="OMP_GITHUB_APP_PRIVATE_KEY_PATH")
    gh_proxy_hmac_key: SecretStr = Field(..., alias="OMP_MAINTAINER_GH_PROXY_HMAC_KEY")
    gh_proxy_bind_host: str = Field("0.0.0.0", alias="OMP_MAINTAINER_GH_PROXY_BIND_HOST")
    gh_proxy_bind_port: int = Field(8081, alias="OMP_MAINTAINER_GH_PROXY_BIND_PORT")
    workspace_root: Path = Field(
        default_factory=lambda: _default_state_root() / "workspaces",
        alias="OMP_MAINTAINER_WORKSPACE_ROOT",
    )
    log_dir: Path = Field(default_factory=lambda: _default_state_root() / "logs", alias="OMP_MAINTAINER_LOG_DIR")
    gh_proxy_max_body_bytes: int = Field(1 << 20, alias="OMP_MAINTAINER_GH_PROXY_MAX_BODY_BYTES")
    gh_proxy_git_timeout_seconds: float = Field(60.0, alias="OMP_MAINTAINER_GH_PROXY_GIT_TIMEOUT_SECONDS")

    @field_validator("github_app_private_key", mode="before")
    @classmethod
    def _blank_private_key_is_missing(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                return None
        return value

    @field_validator("github_app_private_key_path", mode="before")
    @classmethod
    def _blank_private_key_path_is_missing(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("gh_proxy_hmac_key", mode="before")
    @classmethod
    def _reject_blank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must be a non-empty string")
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def _require_exactly_one_private_key_source(self) -> _ProxyEnvLoader:
        if (self.github_app_private_key is None) == (self.github_app_private_key_path is None):
            raise ValueError("set exactly one of OMP_GITHUB_APP_PRIVATE_KEY or OMP_GITHUB_APP_PRIVATE_KEY_PATH")
        return self


def load_proxy_settings() -> Settings:
    """Build a `Settings` instance suitable for the github-broker process.

    Only the env vars the broker actually consumes are required; the
    orchestrator-only fields (webhook secret, bot_login, …) are set to inert
    placeholders since `proxy.server` never reads them. Skips the `Settings()`
    cross-field validator (which presumes orchestrator semantics) by routing
    through `model_construct`.
    """
    loader = _ProxyEnvLoader()  # type: ignore[call-arg]
    if loader.github_app_private_key is not None:
        private_key_pem = loader.github_app_private_key.get_secret_value()
    else:
        assert loader.github_app_private_key_path is not None
        try:
            private_key_pem = loader.github_app_private_key_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError("OMP_GITHUB_APP_PRIVATE_KEY_PATH must name a readable private-key file") from exc
    if not private_key_pem.strip():
        raise ValueError("GitHub App private key must be a non-empty PEM string")

    return Settings.model_construct(
        github_app_id=loader.github_app_id,
        github_app_private_key=SecretStr(private_key_pem),
        github_app_private_key_path=None,
        github_webhook_secret=SecretStr(""),
        bot_login="github-broker",
        git_author_email="github-broker@invalid",
        gh_proxy_url=None,
        gh_proxy_hmac_key=loader.gh_proxy_hmac_key,
        gh_proxy_bind_host=loader.gh_proxy_bind_host,
        gh_proxy_bind_port=loader.gh_proxy_bind_port,
        workspace_root=loader.workspace_root,
        log_dir=loader.log_dir,
        gh_proxy_max_body_bytes=loader.gh_proxy_max_body_bytes,
        gh_proxy_git_timeout_seconds=loader.gh_proxy_git_timeout_seconds,
    )
