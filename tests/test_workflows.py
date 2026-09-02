from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_WORKFLOW_DIRECTORY = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _workflow(filename: str) -> dict[str, Any]:
    document = yaml.safe_load((_WORKFLOW_DIRECTORY / filename).read_text(encoding="utf-8"))
    assert isinstance(document, dict)

    # PyYAML follows YAML 1.1 and resolves GitHub Actions' `on` key as True.
    if True in document:
        assert "on" not in document
        document["on"] = document.pop(True)

    assert isinstance(document.get("on"), dict)
    assert isinstance(document.get("jobs"), dict)
    return document


def _job(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs[name]
    assert isinstance(job, dict)
    return job


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _action_step(job: dict[str, Any], action: str) -> dict[str, Any]:
    return next(step for step in _steps(job) if step.get("uses", "").startswith(f"{action}@"))


def _commands(job: dict[str, Any]) -> set[str]:
    return {step["run"] for step in _steps(job) if isinstance(step.get("run"), str)}


def test_workflows_are_valid_yaml_documents_with_expected_triggers() -> None:
    ci = _workflow("ci.yml")
    release = _workflow("release-image.yml")

    assert ci["on"] == {"pull_request": None, "push": {"branches": ["main"]}}
    assert "pull_request_target" not in ci["on"]
    assert release["on"] == {"push": {"tags": ["v*"]}, "workflow_dispatch": None}
    assert "pull_request" not in release["on"]
    assert "pull_request_target" not in release["on"]


def test_ci_uses_read_only_permissions_and_is_safe_for_forks() -> None:
    ci = _workflow("ci.yml")

    assert ci["permissions"] == {"contents": "read"}
    assert ci["concurrency"] == {
        "group": "ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": True,
    }
    assert "secrets" not in (_WORKFLOW_DIRECTORY / "ci.yml").read_text(encoding="utf-8").lower()

    for job in ci["jobs"].values():
        assert isinstance(job, dict)
        assert "permissions" not in job
        assert isinstance(job.get("timeout-minutes"), int)
        assert job["timeout-minutes"] > 0
        checkout = _action_step(job, "actions/checkout")
        assert checkout["with"]["persist-credentials"] is False


def test_ci_uses_locked_dependency_installs_and_a_build_only_container() -> None:
    ci = _workflow("ci.yml")
    python = _job(ci, "python")
    dashboard = _job(ci, "dashboard")
    container = _job(ci, "container")

    assert "uv sync --extra dev --locked" in _commands(python)
    setup_uv = _action_step(python, "astral-sh/setup-uv")
    assert setup_uv["with"] == {
        "version": "0.8.15",
        "enable-cache": True,
        "cache-dependency-glob": "uv.lock",
    }
    assert "bun install --frozen-lockfile" in _commands(dashboard)

    assert not any(step.get("uses", "").startswith("docker/login-action@") for step in _steps(container))
    build = _action_step(container, "docker/build-push-action")
    assert build["with"]["push"] is False
    assert build["with"]["cache-from"] == "type=gha,scope=ci-container"
    assert build["with"]["cache-to"] == "type=gha,mode=max,scope=ci-container"


def test_release_publishing_is_serialized_tag_only_and_immutable() -> None:
    release = _workflow("release-image.yml")
    publish = _job(release, "publish")

    assert release["permissions"] == {"contents": "read"}
    assert release["concurrency"] == {
        "group": "release-image-${{ github.ref_name }}",
        "cancel-in-progress": False,
    }
    assert publish["if"] == "github.ref_type == 'tag' && startsWith(github.ref_name, 'v')"
    assert publish["timeout-minutes"] == 45
    assert publish["permissions"] == {
        "contents": "read",
        "packages": "write",
        "attestations": "write",
        "id-token": "write",
    }

    checkout = _action_step(publish, "actions/checkout")
    assert checkout["with"] == {"ref": "${{ github.sha }}", "persist-credentials": False}
    metadata = _action_step(publish, "docker/metadata-action")
    assert metadata["with"]["images"] == "ghcr.io/${{ github.repository }}"
    assert metadata["with"]["tags"].splitlines() == [
        "type=ref,event=tag",
        "type=sha,format=long,prefix=sha-",
    ]


def test_release_pushes_multi_architecture_ghcr_image_with_linked_provenance() -> None:
    release = _workflow("release-image.yml")
    publish = _job(release, "publish")

    qemu = _action_step(publish, "docker/setup-qemu-action")
    assert qemu["with"] == {"platforms": "arm64"}
    login = _action_step(publish, "docker/login-action")
    assert login["with"] == {
        "registry": "ghcr.io",
        "username": "${{ github.actor }}",
        "password": "${{ secrets.GITHUB_TOKEN }}",
    }
    build = _action_step(publish, "docker/build-push-action")
    assert build["id"] == "build"
    assert build["with"]["push"] is True
    assert build["with"]["tags"] == "${{ steps.metadata.outputs.tags }}"
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert build["with"]["cache-from"] == "type=gha,scope=release-image"
    assert build["with"]["cache-to"] == "type=gha,mode=max,scope=release-image"

    attestation = _action_step(publish, "actions/attest-build-provenance")
    assert attestation["with"] == {
        "subject-name": "ghcr.io/${{ github.repository }}",
        "subject-digest": "${{ steps.build.outputs.digest }}",
        "push-to-registry": True,
    }
