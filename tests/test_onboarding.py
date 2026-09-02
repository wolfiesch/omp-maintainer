from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import cast

import pytest
from click.testing import CliRunner
from dotenv import dotenv_values

from omp_maintainer.cli import main
from omp_maintainer.policy import load_repo_policy


def _git(directory: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        check=True,
        text=True,
    )


def _local_repository_with_example_policy(workdir: Path, example_policy: Path) -> Path:
    origin = workdir / "origin.git"
    repository = workdir / "repository"
    _git(workdir, "init", "--bare", str(origin))
    _git(workdir, "clone", str(origin), str(repository))
    _git(repository, "config", "user.name", "Onboarding Test")
    _git(repository, "config", "user.email", "onboarding@example.invalid")

    policy_path = repository / ".github" / "omp-maintainer.yml"
    policy_path.parent.mkdir()
    policy_path.write_text(example_policy.read_text(encoding="utf-8"), encoding="utf-8")
    _git(repository, "add", ".github/omp-maintainer.yml")
    _git(repository, "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}", "commit", "-m", "Add policy")
    _git(repository, "push", "origin", "HEAD:main")
    _git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    return repository


def test_temporary_directory_onboarding_smoke_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = Path(__file__).resolve().parents[1]
    example_policy = project_root / "examples" / "omp-maintainer.yml"
    runner = CliRunner()

    for name in ("OMP_GITHUB_APP_ID", "OMP_GITHUB_APP_PRIVATE_KEY", "OMP_GITHUB_APP_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(name, raising=False)

    with runner.isolated_filesystem(temp_dir=tmp_path) as isolated_directory:
        workdir = Path(isolated_directory)
        repository = _local_repository_with_example_policy(workdir, example_policy)
        key_file = workdir / "github-app.pem"
        key_file.write_text("placeholder key material\n", encoding="utf-8")
        env_file = workdir / "maintainer.env"

        setup_result = runner.invoke(
            main,
            [
                "setup",
                "--env-file",
                str(env_file),
                "--github-app-id",
                "123",
                "--github-app-private-key-path",
                str(key_file),
                "--bot-login",
                "maintainer[bot]",
                "--git-author-email",
                "maintainer@example.invalid",
                "--repo",
                "octo/onboarding",
            ],
        )

        assert setup_result.exit_code == 0, setup_result.output
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        setup_environment = {name: value for name, value in dotenv_values(env_file).items() if value is not None}
        assert setup_environment["GITHUB_APP_PRIVATE_KEY_PATH"] == str(key_file)
        for name in ("OMP_MAINTAINER_GH_PROXY_HMAC_KEY", "GITHUB_WEBHOOK_SECRET", "OMP_MAINTAINER_REPLAY_TOKEN"):
            assert setup_environment[name] not in setup_result.output

        fake_bin = workdir / "bin"
        fake_bin.mkdir()
        fake_omp = fake_bin / "omp"
        fake_omp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_omp.chmod(0o755)
        process_environment = {
            **setup_environment,
            "OMP_MAINTAINER_OMP_COMMAND": "omp",
            "PATH": str(fake_bin),
            "XDG_STATE_HOME": str(workdir / "state"),
        }
        doctor_result = runner.invoke(main, ["doctor", "--offline"], env=process_environment)

        assert doctor_result.exit_code == 0, doctor_result.output
        assert all(secret not in doctor_result.output for secret in setup_environment.values())
        report = json.loads(doctor_result.output)
        assert report["ok"] is True
        assert report["offline"] is True
        checks = {check["name"]: check for check in cast(list[dict[str, str]], report["checks"])}
        assert checks["broker"] == {"name": "broker", "status": "skipped", "detail": "offline mode"}
        assert {checks[name]["status"] for name in ("config", "state", "workspace", "log", "omp")} == {"ok"}

        policy = load_repo_policy(repository, "main")
        assert policy.mutations_enabled
        assert policy.load_error is None
        assert not policy.merge.allow_merge
        assert policy.branches.prohibit_default_branch_push
        assert policy.branches.prohibit_tag_push
        assert not policy.ci_repair.release_sentinel.enabled

    help_result = runner.invoke(main, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "Usage: omp-maintainer" in help_result.output
