"""Unit coverage for GitHub App installation authentication."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from omp_maintainer.github_client import GitHubClient, GitHubError
from omp_maintainer.proxy.app_auth import GitHubAppAuth


@pytest.fixture
def app_private_key_pem() -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")


def _expires_in(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _auth(
    private_key_pem: str,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> GitHubAppAuth:
    return GitHubAppAuth(
        12345,
        private_key_pem,
        transport=httpx.MockTransport(handler) if handler is not None else None,
    )


async def test_app_jwt_has_signed_github_claims(app_private_key_pem: str) -> None:
    private_key = serialization.load_pem_private_key(app_private_key_pem.encode("utf-8"), password=None)
    public_key = private_key.public_key()
    assert isinstance(public_key, rsa.RSAPublicKey)
    auth = _auth(app_private_key_pem)
    before = int(time.time())
    encoded = auth._app_jwt()
    after = int(time.time())

    header = jwt.get_unverified_header(encoded)
    claims = jwt.decode(encoded, public_key, algorithms=["RS256"])

    assert header["alg"] == "RS256"
    assert claims["iss"] == "12345"
    assert before - 60 <= claims["iat"] <= after - 60
    assert before + 540 <= claims["exp"] <= after + 540
    await auth.aclose()


async def test_installation_lookup_is_cached_and_client_uses_scoped_token(app_private_key_pem: str) -> None:
    calls: dict[str, int] = defaultdict(int)

    def handler(request: httpx.Request) -> httpx.Response:
        calls[request.url.path] += 1
        assert request.headers["authorization"].startswith("Bearer ey")
        if request.url.path == "/repos/octo/widget/installation":
            return httpx.Response(200, json={"id": 91})
        if request.url.path == "/app/installations/91/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_91", "expires_at": _expires_in(900)})
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    auth = _auth(app_private_key_pem, handler)

    assert await auth.token_for_repo("octo/widget") == "ghs_installation_91"
    client = await auth.client_for_repo("octo/widget")

    assert isinstance(client, GitHubClient)
    assert client._token == "ghs_installation_91"
    assert calls == {
        "/repos/octo/widget/installation": 1,
        "/app/installations/91/access_tokens": 1,
    }
    await auth.aclose()


async def test_installation_token_cache_refreshes_at_required_ttl(app_private_key_pem: str) -> None:
    exchanges = 0
    lookups = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal exchanges, lookups
        if request.url.path == "/repos/octo/widget/installation":
            lookups += 1
            return httpx.Response(200, json={"id": 91})
        if request.url.path == "/app/installations/91/access_tokens":
            exchanges += 1
            return httpx.Response(201, json={"token": f"ghs_token_{exchanges}", "expires_at": _expires_in(600)})
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    auth = _auth(app_private_key_pem, handler)

    assert await auth.token_for_repo("octo/widget") == "ghs_token_1"
    assert await auth.token_for_repo("octo/widget") == "ghs_token_1"
    assert await auth.token_for_repo("octo/widget", min_ttl_seconds=601) == "ghs_token_2"

    assert lookups == 1
    assert exchanges == 2
    await auth.aclose()


class _YieldingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.calls: dict[str, int] = defaultdict(int)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls[request.url.path] += 1
        await asyncio.sleep(0)
        if request.url.path == "/repos/octo/widget/installation":
            return httpx.Response(200, json={"id": 91}, request=request)
        if request.url.path == "/app/installations/91/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs_single_exchange", "expires_at": _expires_in(900)},
                request=request,
            )
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    async def aclose(self) -> None:
        return None


async def test_concurrent_requests_share_installation_and_token_exchanges(app_private_key_pem: str) -> None:
    transport = _YieldingTransport()
    auth = GitHubAppAuth(12345, app_private_key_pem, transport=transport)

    tokens = await asyncio.gather(*(auth.token_for_repo("octo/widget") for _ in range(12)))

    assert tokens == ["ghs_single_exchange"] * 12
    assert transport.calls == {
        "/repos/octo/widget/installation": 1,
        "/app/installations/91/access_tokens": 1,
    }
    await auth.aclose()


async def test_authenticated_login_uses_app_jwt_and_returns_bot_login(app_private_key_pem: str) -> None:
    observed: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers["authorization"]
        assert request.url.path == "/app"
        return httpx.Response(200, json={"slug": "omp-maintainer"})

    auth = _auth(app_private_key_pem, handler)

    assert await auth.authenticated_login() == "omp-maintainer[bot]"
    assert observed["authorization"].startswith("Bearer ey")
    await auth.aclose()


async def test_missing_installation_is_a_secret_free_github_error(app_private_key_pem: str) -> None:
    secret = "ghs_disclosed_token"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/octo/widget/installation"
        return httpx.Response(404, json={"message": f"not found; {secret}; {request.headers['authorization']}"})

    auth = _auth(app_private_key_pem, handler)

    with pytest.raises(GitHubError) as raised:
        await auth.token_for_repo("octo/widget")

    assert raised.value.status == 404
    assert raised.value.message == "GitHub App is not installed on repository octo/widget"
    assert secret not in str(raised.value)
    assert "Bearer" not in str(raised.value)
    await auth.aclose()


async def test_upstream_authentication_failure_does_not_expose_credentials(app_private_key_pem: str) -> None:
    secret = "ghs_disclosed_token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": f"bad credentials {secret} {request.headers['authorization']}"})

    auth = _auth(app_private_key_pem, handler)

    with pytest.raises(GitHubError) as raised:
        await auth.authenticated_login()

    assert raised.value.status == 401
    assert raised.value.message == "GitHub App authentication failed"
    assert secret not in str(raised.value)
    assert "Bearer" not in str(raised.value)
    await auth.aclose()
