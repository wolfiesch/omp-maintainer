"""Command-line interface."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

import click
import httpx
import uvicorn
from dotenv import dotenv_values

from omp_maintainer.config import Settings, get_settings
from omp_maintainer.db import INACTIVE_EVENT_STATES, get_database
from omp_maintainer.logging_config import configure_logging
from omp_maintainer.manual_triage import (
    InvalidIssueRef,
    ManualTriageError,
    ManualTriageTimeout,
    await_terminal_state,
    enqueue_manual_triage,
    parse_issue_ref,
)
from omp_maintainer.proxy_client import GitHubProxyClient
from omp_maintainer.sandbox import SandboxManager
from omp_maintainer.server import create_app


def _settings_or_die() -> Settings:
    try:
        return get_settings()
    except Exception as exc:
        click.echo(f"configuration error: {exc}", err=True)
        sys.exit(2)


def _require_proxy_mode(cfg: Settings) -> tuple[str, bytes]:
    if cfg.gh_proxy_url is None or cfg.gh_proxy_hmac_key is None:
        raise SystemExit(
            "OMP Maintainer orchestrator requires OMP_MAINTAINER_GH_PROXY_URL and "
            "OMP_MAINTAINER_GH_PROXY_HMAC_KEY (run github-broker in a sibling container)."
        )
    return cfg.gh_proxy_url, cfg.gh_proxy_hmac_key.get_secret_value().encode("utf-8")


def _build_github(cfg: Settings) -> GitHubProxyClient:
    base_url, key = _require_proxy_mode(cfg)
    return GitHubProxyClient(base_url=base_url, hmac_key=key)


def _default_wait_timeout(cfg: Settings) -> float:
    return cfg.task_timeout_seconds + cfg.task_timeout_hard_grace_seconds + 30.0


@click.group(name="omp-maintainer")
def main() -> None:
    """OMP Maintainer control surface."""


def _env_assignment(name: str, value: str) -> str:
    """Return an unambiguous dotenv assignment without interpolating input."""
    return f"{name}={json.dumps(value)}"


def _write_private_env_file(path: Path, contents: str) -> None:
    """Create a new owner-only dotenv file without following an existing link."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise click.ClickException("refusing to overwrite an existing configuration file") from exc
    except OSError as exc:
        raise click.ClickException("could not create the configuration file") from exc
    try:
        os.fchmod(fd, 0o600)
    except OSError as exc:
        os.close(fd)
        raise click.ClickException("could not write the configuration file") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as env_file:
            env_file.write(contents)
    except OSError as exc:
        raise click.ClickException("could not write the configuration file") from exc


def _doctor_check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _check_writable_directory(path: Path) -> bool:
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        return False
    fd, probe_path = tempfile.mkstemp(prefix=".omp-maintainer-doctor-", dir=path)
    try:
        os.close(fd)
    finally:
        try:
            os.unlink(probe_path)
        except FileNotFoundError:
            pass
    return True


def _check_writable_state_path(path: Path) -> bool:
    if not _check_writable_directory(path.parent):
        return False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    return True


def _doctor_path_check(name: str, path: Path, *, state: bool = False) -> dict[str, str]:
    try:
        writable = _check_writable_state_path(path) if state else _check_writable_directory(path)
    except (OSError, ValueError):
        writable = False
    if writable:
        return _doctor_check(name, "ok", "writable")
    return _doctor_check(name, "error", "not writable")


def _doctor_broker_check(cfg: Settings) -> dict[str, str]:
    assert cfg.gh_proxy_url is not None
    health_url = f"{cfg.gh_proxy_url.rstrip('/')}/healthz"
    timeout = cfg.request_timeout_seconds if 0 < cfg.request_timeout_seconds <= 10.0 else 10.0
    try:
        response = httpx.get(health_url, timeout=timeout)
    except (httpx.HTTPError, ValueError):
        return _doctor_check("broker", "error", "health endpoint unreachable")
    if response.status_code != 200:
        return _doctor_check("broker", "error", f"health endpoint returned HTTP {response.status_code}")
    try:
        healthy = response.json() == {"status": "ok"}
    except ValueError:
        healthy = False
    if not healthy:
        return _doctor_check("broker", "error", "health endpoint returned an unexpected response")
    return _doctor_check("broker", "ok", "healthy")


@main.command()
@click.option(
    "--env-file",
    type=click.Path(path_type=Path, dir_okay=False, writable=True),
    default=Path(".env"),
    show_default=True,
    help="New local dotenv file to create. The command refuses to overwrite it.",
)
@click.option("--github-app-id", type=click.IntRange(min=1), required=True, help="GitHub App numeric ID.")
@click.option(
    "--github-app-private-key-path",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
    help="Host path to the GitHub App private key. Its contents are never read.",
)
@click.option("--bot-login", required=True, help="GitHub App bot login.")
@click.option("--git-author-email", required=True, help="Commit author email address.")
@click.option(
    "--repo", "repos", multiple=True, required=True, help="Repository to authorize, such as owner/repository."
)
@click.option("--model", default="anthropic/claude-sonnet-4-6", show_default=True, help="OMP model identifier.")
@click.option("--provider", default=None, help="Optional OMP provider identifier.")
def setup(
    env_file: Path,
    github_app_id: int,
    github_app_private_key_path: Path,
    bot_login: str,
    git_author_email: str,
    repos: tuple[str, ...],
    model: str,
    provider: str | None,
) -> None:
    """Create a new private local configuration file."""
    values = [
        ("GITHUB_APP_ID", str(github_app_id)),
        ("GITHUB_APP_PRIVATE_KEY_PATH", str(github_app_private_key_path)),
        ("OMP_MAINTAINER_GH_PROXY_URL", "http://github-broker:8081"),
        ("OMP_MAINTAINER_GH_PROXY_HMAC_KEY", secrets.token_urlsafe(32)),
        ("GITHUB_WEBHOOK_SECRET", secrets.token_urlsafe(32)),
        ("OMP_MAINTAINER_REPLAY_TOKEN", secrets.token_urlsafe(32)),
        ("OMP_MAINTAINER_BOT_LOGIN", bot_login),
        ("OMP_MAINTAINER_GIT_AUTHOR_EMAIL", git_author_email),
        ("OMP_MAINTAINER_REPO_ALLOWLIST", ",".join(repos)),
        ("OMP_MAINTAINER_MODEL", model),
    ]
    if provider is not None:
        values.append(("OMP_MAINTAINER_PROVIDER", provider))
    contents = "\n".join(_env_assignment(name, value) for name, value in values) + "\n"
    _write_private_env_file(env_file, contents)
    click.echo("Created private local configuration.")
    click.echo("Remaining GitHub App and webhook steps:")
    click.echo("1. Configure the App webhook URL as your public service URL plus /webhook/github.")
    click.echo("2. Copy the GITHUB_WEBHOOK_SECRET value from the new file into the App webhook secret field.")
    click.echo(
        "3. Grant repository permissions: Actions read-only, Contents read and write, Issues read and write, and Pull requests read and write."
    )
    click.echo(
        "4. Subscribe to Issues, Issue comments, Pull request, Pull request review comments, and Workflow run events."
    )
    click.echo("5. Install the App only on the repositories supplied with --repo, then start the services.")
    click.echo("6. Keep the private key at the supplied path. The compose service supplies it only to github-broker.")


@main.command()
@click.option(
    "--env-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Explicit dotenv file to validate. When omitted, validate the process environment.",
)
@click.option("--offline", is_flag=True, help="Skip the broker health request for local-only validation.")
def doctor(env_file: Path | None, offline: bool) -> None:
    """Emit a machine-readable readiness report without exposing secrets."""
    checks: list[dict[str, str]] = []
    try:
        if env_file is None:
            cfg = Settings()  # type: ignore[call-arg]
        else:
            if not env_file.is_file():
                raise OSError("configuration file is unavailable")
            values = {name: value for name, value in dotenv_values(env_file).items() if value is not None}
            cfg = Settings.model_validate(values)
    except Exception:
        checks.append(_doctor_check("config", "error", "could not parse configuration"))
        for name in ("broker", "state", "workspace", "log", "omp"):
            checks.append(_doctor_check(name, "skipped", "configuration unavailable"))
    else:
        checks.append(_doctor_check("config", "ok", "parsed"))
        if offline:
            checks.append(_doctor_check("broker", "skipped", "offline mode"))
        else:
            checks.append(_doctor_broker_check(cfg))
        checks.append(_doctor_path_check("state", cfg.sqlite_path, state=True))
        checks.append(_doctor_path_check("workspace", cfg.workspace_root))
        checks.append(_doctor_path_check("log", cfg.log_dir))
        try:
            omp_available = shutil.which(cfg.omp_command) is not None
        except ValueError:
            omp_available = False
        checks.append(
            _doctor_check("omp", "ok", "available")
            if omp_available
            else _doctor_check("omp", "error", "configured executable unavailable")
        )
    report = {
        "checks": checks,
        "offline": offline,
        "ok": all(check["status"] != "error" for check in checks),
    }
    click.echo(json.dumps(report, sort_keys=True, separators=(",", ":")))
    if not report["ok"]:
        raise click.exceptions.Exit(1)


@main.command()
def serve() -> None:
    """Run the webhook receiver + worker pool."""
    cfg = _settings_or_die()
    configure_logging(cfg.log_dir)
    cfg.ensure_paths()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.bind_host, port=cfg.bind_port, log_config=None)


@main.command()
@click.argument("issue_ref")
@click.option(
    "--wait-timeout",
    type=click.FloatRange(min=0.1),
    default=None,
    help="Seconds to wait for a terminal state before returning non-zero (default: task timeout + hard grace + 30).",
)
def triage(issue_ref: str, wait_timeout: float | None) -> None:
    """Fetch a live issue and queue it as if a webhook arrived.

    ISSUE_REF is `owner/repo#NN`.
    """
    cfg = _settings_or_die()
    configure_logging(cfg.log_dir)
    cfg.ensure_paths()
    try:
        repo_full, number = parse_issue_ref(issue_ref)
    except InvalidIssueRef as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)
    if not cfg.allows(repo_full):
        click.echo(f"refusing: {repo_full} not in OMP_MAINTAINER_REPO_ALLOWLIST", err=True)
        sys.exit(2)

    async def _go() -> None:
        github = _build_github(cfg)
        db = get_database(cfg.sqlite_path)
        try:
            delivery = await enqueue_manual_triage(
                db=db,
                github=github,
                repo_full=repo_full,
                number=number,
            )
        except ManualTriageError as exc:
            click.echo(f"refusing: {exc}", err=True)
            sys.exit(2)
        # The dispatcher loop lives in the long-running `serve` process; we
        # only watch the row land in a terminal state. Wake latency is
        # bounded by `WorkerPool._dispatch_loop`'s 10s `_wakeup.wait()` fallback.
        click.echo(json.dumps({"delivery": delivery, "state": "queued"}, indent=2))
        timeout = wait_timeout if wait_timeout is not None else _default_wait_timeout(cfg)
        try:
            final = await await_terminal_state(db, delivery, timeout=timeout)
        except ManualTriageTimeout as exc:
            click.echo(
                json.dumps(
                    {"delivery": delivery, "state": exc.state, "timed_out": True, "error": str(exc)},
                    indent=2,
                ),
                err=True,
            )
            sys.exit(1)
        if final is None:
            click.echo(json.dumps({"delivery": delivery, "state": "missing"}, indent=2))
            return
        click.echo(
            json.dumps(
                {"delivery": delivery, "state": final.state, "error": final.last_error},
                indent=2,
            )
        )

    asyncio.run(_go())


@main.command()
@click.argument("delivery_id")
@click.option(
    "--wait-timeout",
    type=click.FloatRange(min=0.1),
    default=None,
    help="Seconds to wait for a terminal state before returning non-zero (default: task timeout + hard grace + 30).",
)
def replay(delivery_id: str, wait_timeout: float | None) -> None:
    """Re-enqueue a stored event so the running `serve` pool can pick it up."""
    cfg = _settings_or_die()
    configure_logging(cfg.log_dir)
    cfg.ensure_paths()
    db = get_database(cfg.sqlite_path)
    row = db.get_event(delivery_id)
    if row is None:
        click.echo(f"unknown delivery: {delivery_id}", err=True)
        sys.exit(2)
    if not db.requeue_event(delivery_id, from_states=INACTIVE_EVENT_STATES):
        click.echo(
            f"delivery {delivery_id} is {row.state}; only inactive events can be replayed",
            err=True,
        )
        sys.exit(2)

    async def _wait() -> None:
        timeout = wait_timeout if wait_timeout is not None else _default_wait_timeout(cfg)
        try:
            final = await await_terminal_state(db, delivery_id, timeout=timeout)
        except ManualTriageTimeout as exc:
            click.echo(
                json.dumps(
                    {"delivery": delivery_id, "state": exc.state, "timed_out": True, "error": str(exc)},
                    indent=2,
                ),
                err=True,
            )
            sys.exit(1)
        if final is None:
            click.echo(json.dumps({"delivery": delivery_id, "state": "missing"}, indent=2))
            return
        click.echo(
            json.dumps(
                {"delivery": delivery_id, "state": final.state, "error": final.last_error},
                indent=2,
            )
        )

    asyncio.run(_wait())


@main.command()
def status() -> None:
    """Dump issue and release state."""
    cfg = _settings_or_die()
    cfg.ensure_paths()
    db = get_database(cfg.sqlite_path)
    issue_rows = db.list_issues()
    for row in issue_rows:
        click.echo(
            f"{row.key:<40} state={row.state:<12} pr={row.pr_number or '-'} "
            f"branch={row.branch or '-'} updated={row.updated_at}"
        )
    release_rows = db.list_releases()
    if release_rows:
        if issue_rows:
            click.echo()
        click.echo("Releases:")
    for row in release_rows:
        error = f" error={row.last_error}" if row.last_error else ""
        click.echo(
            f"{row.key:<40} state={row.state:<12} rounds={row.rounds:<2} "
            f"sha={row.current_sha[:12]} updated={row.updated_at}{error}"
        )


@main.command()
@click.option(
    "--limit",
    type=click.IntRange(min=1, max=100),
    default=50,
    show_default=True,
    help="Maximum number of most-recent events to return.",
)
def events(limit: int) -> None:
    """Emit recent durable event metadata without webhook payloads."""
    cfg = _settings_or_die()
    cfg.ensure_paths()
    rows = get_database(cfg.sqlite_path).list_events(limit=limit)
    click.echo(
        json.dumps(
            {
                "events": [
                    {
                        "delivery_id": row.delivery_id,
                        "event_type": row.event_type,
                        "repo": row.repo,
                        "issue_key": row.issue_key,
                        "received_at": row.received_at,
                        "state": row.state,
                        "attempts": row.attempts,
                        "last_error": row.last_error,
                        "cost_usd": row.cost_usd,
                        "tokens_input": row.tokens_input,
                        "tokens_output": row.tokens_output,
                        "tokens_cache_read": row.tokens_cache_read,
                        "tokens_cache_write": row.tokens_cache_write,
                        "tokens_total": row.tokens_total,
                        "tool_calls": row.tool_calls,
                    }
                    for row in rows
                ]
            },
            indent=2,
            sort_keys=True,
        )
    )


@main.command()
@click.argument("issue_key")
def cleanup(issue_key: str) -> None:
    """Force-remove the workspace for an issue (does not touch the remote)."""
    cfg = _settings_or_die()
    cfg.ensure_paths()
    db = get_database(cfg.sqlite_path)
    row = db.get_issue(issue_key)
    if row is None:
        click.echo(f"unknown issue: {issue_key}", err=True)
        sys.exit(2)
    sandbox = SandboxManager(cfg.workspace_root)
    sandbox.remove_workspace(repo=row.repo, number=row.number)
    db.set_issue_state(issue_key, "abandoned")
    click.echo(f"cleaned up {issue_key}")


if __name__ == "__main__":
    main()
