"""GitHub App installation authentication for the isolated broker."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from omp_maintainer.github_client import ACCEPT, API_VERSION, GITHUB_API, GitHubClient, GitHubError


@dataclass(frozen=True, slots=True)
class _InstallationToken:
    """An installation token with its absolute expiry time."""

    token: str
    expires_at: float


class GitHubAppAuth:
    """Resolve and cache least-privilege GitHub App installation tokens.

    The App JWT is used only to discover an installation and mint its short-lived
    access token. Repository API calls receive an installation-scoped
    :class:`GitHubClient` instead.
    """

    def __init__(
        self,
        app_id: int,
        private_key_pem: str,
        *,
        api_url: str = GITHUB_API,
        transport: httpx.AsyncBaseTransport | None = None,
        refresh_skew_seconds: float = 300.0,
    ) -> None:
        if app_id <= 0:
            raise ValueError("GitHub App ID must be positive")
        if not private_key_pem.strip():
            raise ValueError("GitHub App private key must not be blank")
        if refresh_skew_seconds < 0:
            raise ValueError("refresh_skew_seconds must not be negative")

        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._api_url = api_url.rstrip("/")
        self._transport = transport
        self._refresh_skew_seconds = refresh_skew_seconds
        self._http = httpx.AsyncClient(
            base_url=self._api_url,
            headers={
                "Accept": ACCEPT,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "omp-maintainer/0.1",
            },
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        self._installations_by_repo: dict[str, int] = {}
        self._tokens_by_installation: dict[int, _InstallationToken] = {}
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._installation_locks: dict[int, asyncio.Lock] = {}

    def _app_jwt(self) -> str:
        """Create a GitHub-compatible App JWT, with a small clock-skew buffer."""
        now = int(time.time())
        encoded = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": str(self._app_id)},
            self._private_key_pem,
            algorithm="RS256",
        )
        return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded

    @staticmethod
    def _validate_repo(repo: str) -> None:
        owner, separator, name = repo.partition("/")
        if not separator or not owner or not name or "/" in name or any(character.isspace() for character in repo):
            raise ValueError("repo must be an exact owner/repository name")

    def _repo_lock(self, repo: str) -> asyncio.Lock:
        lock = self._repo_locks.get(repo)
        if lock is None:
            lock = asyncio.Lock()
            self._repo_locks[repo] = lock
        return lock

    def _installation_lock(self, installation_id: int) -> asyncio.Lock:
        lock = self._installation_locks.get(installation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._installation_locks[installation_id] = lock
        return lock

    async def _request_as_app(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        not_installed_repo: str | None = None,
    ) -> dict[str, Any]:
        response = await self._http.request(
            method,
            path,
            json=json,
            headers={"Authorization": f"Bearer {self._app_jwt()}"},
        )
        if response.is_error:
            if response.status_code == 404 and not_installed_repo is not None:
                raise GitHubError(404, f"GitHub App is not installed on repository {not_installed_repo}")
            if response.status_code in {401, 403}:
                raise GitHubError(response.status_code, "GitHub App authentication failed")
            raise GitHubError(response.status_code, "GitHub App request failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubError(502, "GitHub App returned an invalid response") from exc
        if not isinstance(payload, dict):
            raise GitHubError(502, "GitHub App returned an invalid response")
        return payload

    async def _installation_for_repo(self, repo: str) -> int:
        installation_id = self._installations_by_repo.get(repo)
        if installation_id is not None:
            return installation_id

        async with self._repo_lock(repo):
            installation_id = self._installations_by_repo.get(repo)
            if installation_id is not None:
                return installation_id
            payload = await self._request_as_app(
                "GET",
                f"/repos/{quote(repo, safe='/')}/installation",
                not_installed_repo=repo,
            )
            installation_id = payload.get("id")
            if not isinstance(installation_id, int) or installation_id <= 0:
                raise GitHubError(502, "GitHub App returned an invalid installation")
            self._installations_by_repo[repo] = installation_id
            return installation_id

    def _token_is_fresh(self, cached: _InstallationToken, min_ttl_seconds: float) -> bool:
        return cached.expires_at - time.time() > max(self._refresh_skew_seconds, min_ttl_seconds)

    @staticmethod
    def _parse_expiry(expires_at: object) -> float:
        if not isinstance(expires_at, str):
            raise GitHubError(502, "GitHub App returned an invalid installation-token expiration")
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GitHubError(502, "GitHub App returned an invalid installation-token expiration") from exc
        if parsed.tzinfo is None:
            raise GitHubError(502, "GitHub App returned an invalid installation-token expiration")
        return parsed.astimezone(UTC).timestamp()

    async def _token_for_installation(self, installation_id: int, min_ttl_seconds: float) -> str:
        cached = self._tokens_by_installation.get(installation_id)
        if cached is not None and self._token_is_fresh(cached, min_ttl_seconds):
            return cached.token

        async with self._installation_lock(installation_id):
            cached = self._tokens_by_installation.get(installation_id)
            if cached is not None and self._token_is_fresh(cached, min_ttl_seconds):
                return cached.token

            payload = await self._request_as_app(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
            )
            token = payload.get("token")
            expires_at = self._parse_expiry(payload.get("expires_at"))
            if not isinstance(token, str) or not token or expires_at <= time.time():
                raise GitHubError(502, "GitHub App returned an invalid installation token")
            self._tokens_by_installation[installation_id] = _InstallationToken(token=token, expires_at=expires_at)
            return token

    async def token_for_repo(self, repo: str, min_ttl_seconds: float = 300.0) -> str:
        """Return a fresh-enough token scoped to exactly ``repo``."""
        self._validate_repo(repo)
        if min_ttl_seconds < 0:
            raise ValueError("min_ttl_seconds must not be negative")
        installation_id = await self._installation_for_repo(repo)
        return await self._token_for_installation(installation_id, min_ttl_seconds)

    async def client_for_repo(self, repo: str) -> GitHubClient:
        """Return the existing REST client configured with a repo installation token."""
        token = await self.token_for_repo(repo)
        return GitHubClient(token, transport=self._transport)  # type: ignore[arg-type]

    async def authenticated_login(self) -> str:
        """Return the GitHub App bot login in the same form as GitHub clients."""
        payload = await self._request_as_app("GET", "/app")
        slug = payload.get("slug")
        if not isinstance(slug, str) or not slug:
            raise GitHubError(502, "GitHub App returned an invalid app identity")
        return f"{slug}[bot]"

    def invalidate(self, repo: str) -> None:
        """Forget the repository installation and its current access token."""
        installation_id = self._installations_by_repo.pop(repo, None)
        if installation_id is not None:
            self._tokens_by_installation.pop(installation_id, None)

    async def aclose(self) -> None:
        """Close this instance's owned HTTP client."""
        await self._http.aclose()
