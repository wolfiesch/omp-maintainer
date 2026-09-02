from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import cast

import pytest
from click.testing import CliRunner
from dotenv import dotenv_values

from omp_maintainer import cli
from omp_maintainer import db as db_module
from omp_maintainer.db import get_database


def _setup_args(env_file: Path) -> list[str]:
    return [
        "setup",
        "--env-file",
        str(env_file),
        "--github-app-id",
        "123",
        "--github-app-private-key-path",
        "/keys/github-app.pem",
        "--bot-login",
        "maintainer[bot]",
        "--git-author-email",
        "maintainer@example.invalid",
        "--repo",
        "octo/widget",
        "--model",
        "provider/example",
        "--provider",
        "example-provider",
    ]


def _write_valid_env(path: Path, tmp_path: Path) -> None:
    values = {
        "GITHUB_WEBHOOK_SECRET": "webhook-secret-for-test",
        "OMP_MAINTAINER_BOT_LOGIN": "maintainer",
        "OMP_MAINTAINER_GIT_AUTHOR_EMAIL": "maintainer@example.invalid",
        "OMP_MAINTAINER_GH_PROXY_URL": "http://github-broker.invalid:8081",
        "OMP_MAINTAINER_GH_PROXY_HMAC_KEY": "hmac-secret-for-test",
        "OMP_MAINTAINER_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "OMP_MAINTAINER_SQLITE_PATH": str(tmp_path / "state" / "maintainer.sqlite"),
        "OMP_MAINTAINER_LOG_DIR": str(tmp_path / "logs"),
        "OMP_MAINTAINER_OMP_COMMAND": sys.executable,
    }
    path.write_text("\n".join(f"{name}={json.dumps(value)}" for name, value in values.items()) + "\n", encoding="utf-8")


def _checks(report: dict[str, object]) -> dict[str, dict[str, str]]:
    checks = cast(list[dict[str, str]], report["checks"])
    return {check["name"]: check for check in checks}


def test_setup_writes_owner_only_file_and_redacts_generated_secrets(tmp_path: Path) -> None:
    env_file = tmp_path / "maintainer.env"

    result = CliRunner().invoke(cli.main, _setup_args(env_file))

    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    values = dotenv_values(env_file)
    assert values["GITHUB_APP_ID"] == "123"
    assert values["GITHUB_APP_PRIVATE_KEY_PATH"] == "/keys/github-app.pem"
    assert values["OMP_MAINTAINER_MODEL"] == "provider/example"
    assert values["OMP_MAINTAINER_PROVIDER"] == "example-provider"
    for name in ("OMP_MAINTAINER_GH_PROXY_HMAC_KEY", "GITHUB_WEBHOOK_SECRET", "OMP_MAINTAINER_REPLAY_TOKEN"):
        secret = values[name]
        assert secret is not None
        assert secret not in result.output


def test_setup_refuses_to_overwrite_existing_file(tmp_path: Path) -> None:
    env_file = tmp_path / "maintainer.env"
    env_file.write_text("keep-this-file\n", encoding="utf-8")

    result = CliRunner().invoke(cli.main, _setup_args(env_file))

    assert result.exit_code != 0
    assert "refusing to overwrite" in result.output
    assert env_file.read_text(encoding="utf-8") == "keep-this-file\n"


def test_doctor_reports_valid_offline_local_configuration(tmp_path: Path) -> None:
    env_file = tmp_path / "maintainer.env"
    _write_valid_env(env_file, tmp_path)

    result = CliRunner().invoke(cli.main, ["doctor", "--env-file", str(env_file), "--offline"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    checks = _checks(report)
    assert report["ok"] is True
    assert report["offline"] is True
    assert checks["config"]["status"] == "ok"
    assert checks["broker"] == {"name": "broker", "status": "skipped", "detail": "offline mode"}
    assert {checks[name]["status"] for name in ("state", "workspace", "log", "omp")} == {"ok"}


def test_doctor_uses_process_environment_when_env_file_is_omitted(tmp_path: Path) -> None:
    env_file = tmp_path / "maintainer.env"
    _write_valid_env(env_file, tmp_path)
    process_env = {name: value for name, value in dotenv_values(env_file).items() if value is not None}

    result = CliRunner().invoke(cli.main, ["doctor", "--offline"], env=process_env)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is True
    assert _checks(report)["config"]["status"] == "ok"


def test_doctor_reports_healthy_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / "maintainer.env"
    _write_valid_env(env_file, tmp_path)

    class HealthyResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, str]:
            return {"status": "ok"}

    def healthy_response(*_args: object, **_kwargs: object) -> HealthyResponse:
        return HealthyResponse()

    monkeypatch.setattr(cli.httpx, "get", healthy_response)
    result = CliRunner().invoke(cli.main, ["doctor", "--env-file", str(env_file)])

    assert result.exit_code == 0, result.output
    assert _checks(json.loads(result.output))["broker"]["status"] == "ok"


def test_doctor_emits_safe_json_for_invalid_configuration(tmp_path: Path) -> None:
    env_file = tmp_path / "invalid.env"
    secret = "must-not-appear-in-doctor-output"
    env_file.write_text(f"GITHUB_WEBHOOK_SECRET={json.dumps(secret)}\n", encoding="utf-8")

    result = CliRunner().invoke(cli.main, ["doctor", "--env-file", str(env_file), "--offline"])

    assert result.exit_code == 1
    assert secret not in result.output
    report = json.loads(result.output)
    checks = _checks(report)
    assert report["ok"] is False
    assert checks["config"] == {"name": "config", "status": "error", "detail": "could not parse configuration"}
    assert {checks[name]["status"] for name in ("broker", "state", "workspace", "log", "omp")} == {"skipped"}


def test_events_emits_bounded_safe_json_in_newest_order(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = get_database(Path(env["OMP_MAINTAINER_SQLITE_PATH"]))
    timestamps = iter(
        (
            "2026-09-01T00:00:00.000000Z",
            "2026-09-01T00:00:01.000000Z",
            "2026-09-01T00:00:02.000000Z",
            "2026-09-01T00:00:03.000000Z",
        )
    )
    monkeypatch.setattr(db_module, "_utcnow", lambda: next(timestamps))
    payload_secret = "must-not-appear-in-events-output"

    assert database.record_event(
        delivery_id="older",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#1",
        payload={"token": payload_secret},
    )
    claimed = database.claim_next_event()
    assert claimed is not None and claimed.delivery_id == "older"
    database.record_event_usage(
        "older",
        cost_usd=1.25,
        tokens_input=200,
        tokens_output=50,
        tokens_cache_read=25,
        tokens_cache_write=5,
        tokens_total=280,
        tool_calls=4,
    )
    database.mark_event("older", "failed", error="worker request failed")
    assert database.record_event(
        delivery_id="newer",
        event_type="issue_comment",
        repo="octo/widget",
        issue_key="octo/widget#2",
        payload={"authorization": payload_secret},
    )

    result = CliRunner().invoke(cli.main, ["events", "--limit", "2"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert list(report) == ["events"]
    assert [row["delivery_id"] for row in report["events"]] == ["newer", "older"]
    assert report["events"][1] == {
        "attempts": 1,
        "cost_usd": 1.25,
        "delivery_id": "older",
        "event_type": "issues",
        "issue_key": "octo/widget#1",
        "last_error": "worker request failed",
        "received_at": "2026-09-01T00:00:00.000000Z",
        "repo": "octo/widget",
        "state": "failed",
        "tokens_cache_read": 25,
        "tokens_cache_write": 5,
        "tokens_input": 200,
        "tokens_output": 50,
        "tokens_total": 280,
        "tool_calls": 4,
    }
    assert payload_secret not in result.output
    assert "payload" not in result.output

    limited = CliRunner().invoke(cli.main, ["events", "--limit", "1"])

    assert limited.exit_code == 0, limited.output
    assert [row["delivery_id"] for row in json.loads(limited.output)["events"]] == ["newer"]


@pytest.mark.parametrize("limit", ("0", "101"))
def test_events_rejects_out_of_range_limit(limit: str) -> None:
    result = CliRunner().invoke(cli.main, ["events", "--limit", limit])

    assert result.exit_code == 2
    assert "Invalid value for '--limit'" in result.output
