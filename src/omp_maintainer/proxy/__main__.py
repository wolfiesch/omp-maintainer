"""`python -m omp_maintainer.proxy serve` — run the github-broker FastAPI app."""

from __future__ import annotations

import sys

import click
import uvicorn

from omp_maintainer.config import Settings, load_proxy_settings
from omp_maintainer.logging_config import configure_logging
from omp_maintainer.proxy.server import create_proxy_app


def _settings_or_die() -> Settings:
    """Load proxy-only settings, surfacing config errors as exit code 2.

    Routes through `load_proxy_settings` (NOT the orchestrator `Settings()`
    ctor) so the github-broker container needs only GitHub App credentials +
    `OMP_MAINTAINER_GH_PROXY_HMAC_KEY` — the orchestrator's webhook secret,
    bot_login, and proxy-URL fields are irrelevant here.
    """
    try:
        return load_proxy_settings()
    except Exception as exc:
        click.echo(f"github-broker configuration error: {exc}", err=True)
        sys.exit(2)


@click.group()
def main() -> None:
    """github-broker control surface."""


@main.command()
def serve() -> None:
    """Run the HMAC-authenticated GitHub broker."""
    cfg = _settings_or_die()
    configure_logging(cfg.log_dir)
    cfg.ensure_paths()
    # `load_proxy_settings` already rejects invalid App credentials, but stay
    # defensive in case a caller constructs the Settings by hand.
    if not isinstance(cfg.github_app_id, int) or cfg.github_app_id <= 0:
        click.echo("github-broker: OMP_GITHUB_APP_ID must be a positive integer in broker mode", err=True)
        sys.exit(2)
    if cfg.github_app_private_key is None:
        click.echo("github-broker: GitHub App private key is required in broker mode", err=True)
        sys.exit(2)
    if cfg.gh_proxy_hmac_key is None:
        click.echo("github-broker: OMP_MAINTAINER_GH_PROXY_HMAC_KEY is required in broker mode", err=True)
        sys.exit(2)
    app = create_proxy_app(cfg)
    uvicorn.run(
        app,
        host=cfg.gh_proxy_bind_host,
        port=cfg.gh_proxy_bind_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
