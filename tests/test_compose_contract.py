from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_COMPOSE_PATH = Path(__file__).resolve().parents[1] / "compose.yaml"
_DIRECT_PROVIDER_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_CODEX_OAUTH_TOKEN",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "CEREBRAS_API_KEY",
        "DEEPSEEK_API_KEY",
        "TOGETHER_API_KEY",
        "OPENROUTER_API_KEY",
        "MISTRAL_API_KEY",
        "XAI_API_KEY",
        "OPENAI_BASE_URL",
        "OLLAMA_BASE_URL",
        "LM_STUDIO_BASE_URL",
        "LLAMA_CPP_BASE_URL",
    }
)


def _compose() -> dict[str, Any]:
    document = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _service(compose: dict[str, Any], name: str) -> dict[str, Any]:
    services = compose["services"]
    assert isinstance(services, dict)
    service = services[name]
    assert isinstance(service, dict)
    return service


def _environment(service: dict[str, Any]) -> dict[str, str]:
    environment = service["environment"]
    assert isinstance(environment, dict)
    assert all(isinstance(name, str) and isinstance(value, str) for name, value in environment.items())
    return environment


def test_compose_passes_direct_omp_provider_configuration_only_to_maintainer() -> None:
    compose = _compose()
    maintainer_environment = _environment(_service(compose, "maintainer"))
    broker_environment = _environment(_service(compose, "github-broker"))

    assert maintainer_environment["OMP_MAINTAINER_PROVIDER"] == "${OMP_MAINTAINER_PROVIDER:-}"
    assert maintainer_environment["OMP_MAINTAINER_MODEL"] == "${OMP_MAINTAINER_MODEL:-anthropic/claude-sonnet-4-6}"
    for name in _DIRECT_PROVIDER_ENV:
        assert maintainer_environment[name] == f"${{{name}:-}}"
        assert name not in broker_environment
    assert "OMP_MAINTAINER_PROVIDER" not in broker_environment
    assert "OMP_MAINTAINER_MODEL" not in broker_environment
    assert not any("LITELLM" in name for name in maintainer_environment | broker_environment)


def test_compose_keeps_github_app_credentials_broker_only() -> None:
    compose = _compose()
    maintainer = _service(compose, "maintainer")
    broker = _service(compose, "github-broker")
    maintainer_environment = _environment(maintainer)
    broker_environment = _environment(broker)

    assert "OMP_GITHUB_APP_ID" not in maintainer_environment
    assert "OMP_GITHUB_APP_PRIVATE_KEY" not in maintainer_environment
    assert "OMP_GITHUB_APP_PRIVATE_KEY_PATH" not in maintainer_environment
    assert "secrets" not in maintainer
    assert broker_environment["OMP_GITHUB_APP_ID"] == "${GITHUB_APP_ID:?GITHUB_APP_ID must be set}"
    assert broker_environment["OMP_GITHUB_APP_PRIVATE_KEY_PATH"] == "/run/secrets/github_app_private_key"
    assert broker["secrets"] == [
        {"source": "github_app_private_key", "target": "github_app_private_key", "mode": 0o400}
    ]
    assert compose["secrets"] == {
        "github_app_private_key": {"file": "${GITHUB_APP_PRIVATE_KEY_PATH:?GITHUB_APP_PRIVATE_KEY_PATH must be set}"}
    }


def test_compose_has_deterministic_startup_recovery_and_persistent_state() -> None:
    compose = _compose()
    maintainer = _service(compose, "maintainer")
    broker = _service(compose, "github-broker")

    for service, port in ((maintainer, 8080), (broker, 8081)):
        assert service["restart"] == "unless-stopped"
        assert service["stop_signal"] == "SIGTERM"
        assert service["stop_grace_period"] == "30s"
        assert service["healthcheck"] == {
            "test": ["CMD-SHELL", f"curl --fail --silent --show-error http://127.0.0.1:{port}/healthz || exit 1"],
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "10s",
        }

    assert maintainer["depends_on"] == {"github-broker": {"condition": "service_healthy"}}
    assert maintainer["volumes"] == ["maintainer_data:/data"]
    assert broker["volumes"] == ["maintainer_data:/data"]
    assert compose["volumes"] == {"maintainer_data": {}}

    maintainer_environment = _environment(maintainer)
    broker_environment = _environment(broker)
    assert maintainer_environment["OMP_MAINTAINER_WORKSPACE_ROOT"] == "/data/workspaces"
    assert maintainer_environment["OMP_MAINTAINER_SQLITE_PATH"] == "/data/maintainer.sqlite"
    assert maintainer_environment["OMP_MAINTAINER_LOG_DIR"] == "/data/logs"
    assert broker_environment["OMP_MAINTAINER_WORKSPACE_ROOT"] == "/data/workspaces"
    assert broker_environment["OMP_MAINTAINER_LOG_DIR"] == "/data/logs"


def test_compose_requires_no_host_checkout_or_models_file_mount() -> None:
    compose = _compose()

    for service_name in ("maintainer", "github-broker"):
        volumes = _service(compose, service_name)["volumes"]
        assert isinstance(volumes, list)
        assert volumes == ["maintainer_data:/data"]

    rendered = yaml.safe_dump(compose)
    assert "models.yml" not in rendered
    assert "AGENTS.md" not in rendered
    assert "type: bind" not in rendered
