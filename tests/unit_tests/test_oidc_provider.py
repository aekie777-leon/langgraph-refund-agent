"""Crypto and controlled-HTTP tests for the OIDC/JWKS identity provider."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from agent.auth.config import ClaimMappingConfig, OidcVerifierConfig
from agent.auth.jwks import AsyncJwksCache
from agent.auth.oidc_provider import OidcJwtIdentityProvider
from agent.auth.provider import (
    IdentityInfrastructureUnavailableError,
    UnauthenticatedError,
)

pytestmark = pytest.mark.anyio

_ISSUER = "https://identity.example.test/"
_AUDIENCE = "refund-agent"
_JWKS_URL = "https://identity.example.test/.well-known/jwks.json"


def _private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(private_key, kid: str) -> dict[str, Any]:
    value = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    assert isinstance(value, dict)
    return {**value, "kid": kid, "alg": "RS256", "use": "sig"}


def _config(*, ttl_seconds: int = 300) -> OidcVerifierConfig:
    return OidcVerifierConfig(
        issuer=_ISSUER,
        audience=_AUDIENCE,
        jwks_url=_JWKS_URL,
        algorithms=frozenset({"RS256"}),
        claims=ClaimMappingConfig(
            user_id_claim="app_user_id",
            tenant_id_claim="app_tenant_id",
            groups_claim="app_groups",
            role_groups={
                "customer": frozenset({"refund-customers"}),
                "support_agent": frozenset({"refund-agents"}),
                "supervisor": frozenset({"refund-supervisors"}),
            },
        ),
        jwks_cache_ttl_seconds=ttl_seconds,
        clock_skew_seconds=5,
    )


def _claims(**updates: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "iat": now - timedelta(seconds=1),
        "nbf": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
        "app_user_id": "customer-a",
        "app_tenant_id": "tenant-demo",
        "app_groups": ["refund-customers"],
    }
    claims.update(updates)
    return claims


def _token(private_key, kid: str, **claim_updates: Any) -> str:
    return jwt.encode(
        _claims(**claim_updates),
        private_key,
        algorithm="RS256",
        headers={"kid": kid, "typ": "at+jwt"},
    )


def _provider(
    *,
    client: httpx.AsyncClient,
    clock,
    ttl_seconds: int = 300,
) -> OidcJwtIdentityProvider:
    config = _config(ttl_seconds=ttl_seconds)
    cache = AsyncJwksCache(
        client=client,
        jwks_url=config.jwks_url,
        allowed_algorithms=frozenset(config.algorithms),
        ttl_seconds=config.jwks_cache_ttl_seconds,
        clock=clock,
    )
    return OidcJwtIdentityProvider(config, cache)


async def test_real_signature_and_jwks_http_produce_server_owned_scope() -> None:
    private_key = _private_key()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [_jwk(private_key, "key-1")]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client=client, clock=lambda: 1.0)
        identity = await provider.resolve(
            authorization_header=f"Bearer {_token(private_key, 'key-1')}"
        )

    assert identity.identity_key == "tenant-demo:customer-a"
    assert identity.role == "customer"
    assert identity.permissions == frozenset(
        {"orders:read:own", "orders:operate:own", "cases:read:own"}
    )


@pytest.mark.parametrize(
    "claim_updates",
    [
        {"iss": "https://attacker.example/"},
        {"aud": "another-service"},
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
        {"nbf": datetime.now(UTC) + timedelta(minutes=1)},
        {"iat": datetime.now(UTC) + timedelta(minutes=1)},
        {"iat": "not-a-numeric-date"},
    ],
)
async def test_invalid_issuer_audience_and_time_claims_return_invalid_credentials(
    claim_updates: dict[str, Any],
) -> None:
    private_key = _private_key()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [_jwk(private_key, "key-1")]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client=client, clock=lambda: 1.0)
        token = _token(private_key, "key-1", **claim_updates)
        with pytest.raises(UnauthenticatedError):
            await provider.resolve(authorization_header=f"Bearer {token}")


async def test_missing_required_time_claim_and_token_permissions_fail_closed() -> None:
    private_key = _private_key()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [_jwk(private_key, "key-1")]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client=client, clock=lambda: 1.0)
        claims = _claims(permissions=["provider_ops:redrive"])
        del claims["nbf"]
        token = jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": "key-1"},
        )
        with pytest.raises(UnauthenticatedError):
            await provider.resolve(authorization_header=f"Bearer {token}")


async def test_algorithm_header_is_rejected_before_jwks_fetch() -> None:
    requests = 0
    token = jwt.encode(
        _claims(),
        "long-enough-test-secret-that-is-not-trusted",
        algorithm="HS256",
        headers={"kid": "key-1"},
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client=client, clock=lambda: 1.0)
        with pytest.raises(UnauthenticatedError):
            await provider.resolve(authorization_header=f"Bearer {token}")

    assert requests == 0


async def test_unknown_kid_forces_one_refresh_and_accepts_rotated_key() -> None:
    old_key = _private_key()
    new_key = _private_key()
    responses = [
        {"keys": [_jwk(old_key, "old-key")]},
        {"keys": [_jwk(old_key, "old-key"), _jwk(new_key, "new-key")]},
    ]
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        payload = responses[min(requests, len(responses) - 1)]
        requests += 1
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client=client, clock=lambda: 1.0)
        await provider.resolve(
            authorization_header=f"Bearer {_token(old_key, 'old-key')}"
        )
        identity = await provider.resolve(
            authorization_header=f"Bearer {_token(new_key, 'new-key')}"
        )

    assert identity.user_id == "customer-a"
    assert requests == 2


async def test_concurrent_unknown_kid_requests_coalesce_forced_refresh() -> None:
    old_key = _private_key()
    new_key = _private_key()
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0)
        keys = [_jwk(old_key, "old-key")]
        if requests >= 2:
            keys.append(_jwk(new_key, "new-key"))
        return httpx.Response(200, json={"keys": keys})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client=client, clock=lambda: 1.0)
        await provider.resolve(
            authorization_header=f"Bearer {_token(old_key, 'old-key')}"
        )
        authorization = f"Bearer {_token(new_key, 'new-key')}"
        identities = await asyncio.gather(
            *(
                provider.resolve(authorization_header=authorization)
                for _ in range(10)
            )
        )

    assert {identity.user_id for identity in identities} == {"customer-a"}
    assert requests == 2


async def test_known_key_survives_outage_within_ttl_then_fails_closed() -> None:
    private_key = _private_key()
    now = [1.0]
    unavailable = False

    def handler(_request: httpx.Request) -> httpx.Response:
        if unavailable:
            raise httpx.ConnectError("issuer is offline")
        return httpx.Response(200, json={"keys": [_jwk(private_key, "key-1")]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client=client, clock=lambda: now[0], ttl_seconds=10)
        authorization = f"Bearer {_token(private_key, 'key-1')}"
        await provider.resolve(authorization_header=authorization)
        unavailable = True
        now[0] = 10.9
        assert (
            await provider.resolve(authorization_header=authorization)
        ).user_id == "customer-a"
        now[0] = 11.0
        with pytest.raises(IdentityInfrastructureUnavailableError):
            await provider.resolve(authorization_header=authorization)


async def test_unknown_kid_refresh_outage_is_infrastructure_unavailable() -> None:
    private_key = _private_key()
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                200, json={"keys": [_jwk(private_key, "known-key")]}
            )
        raise httpx.ConnectError("must-not-leak")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client=client, clock=lambda: 1.0)
        await provider.resolve(
            authorization_header=f"Bearer {_token(private_key, 'known-key')}"
        )
        with pytest.raises(IdentityInfrastructureUnavailableError) as error:
            await provider.resolve(
                authorization_header=f"Bearer {_token(private_key, 'unknown-key')}"
            )

    assert str(error.value) == "identity infrastructure is unavailable"
    assert "must-not-leak" not in str(error.value)
    assert requests == 2


async def test_unknown_kid_absent_after_successful_refresh_is_invalid_credentials() -> (
    None
):
    private_key = _private_key()
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200, json={"keys": [_jwk(private_key, "known-key")]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client=client, clock=lambda: 1.0)
        await provider.resolve(
            authorization_header=f"Bearer {_token(private_key, 'known-key')}"
        )
        with pytest.raises(UnauthenticatedError):
            await provider.resolve(
                authorization_header=f"Bearer {_token(private_key, 'unknown-key')}"
            )

    assert requests == 2


async def test_concurrent_refresh_outage_is_coalesced_and_remains_503() -> None:
    private_key = _private_key()
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0)
        if requests == 1:
            return httpx.Response(
                200, json={"keys": [_jwk(private_key, "known-key")]}
            )
        raise httpx.ConnectError("issuer is offline")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client=client, clock=lambda: 1.0)
        await provider.resolve(
            authorization_header=f"Bearer {_token(private_key, 'known-key')}"
        )
        authorization = f"Bearer {_token(private_key, 'unknown-key')}"
        results = await asyncio.gather(
            *(
                provider.resolve(authorization_header=authorization)
                for _ in range(10)
            ),
            return_exceptions=True,
        )

    assert requests == 2
    assert all(
        isinstance(result, IdentityInfrastructureUnavailableError)
        for result in results
    )


async def test_missing_kid_and_wrong_signature_are_invalid_credentials() -> None:
    trusted_key = _private_key()
    attacker_key = _private_key()
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"keys": [_jwk(trusted_key, "key-1")]})

    token_without_kid = jwt.encode(
        _claims(), trusted_key, algorithm="RS256", headers={"typ": "at+jwt"}
    )
    wrong_signature = _token(attacker_key, "key-1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(client=client, clock=lambda: 1.0)
        with pytest.raises(UnauthenticatedError):
            await provider.resolve(
                authorization_header=f"Bearer {token_without_kid}"
            )
        with pytest.raises(UnauthenticatedError):
            await provider.resolve(
                authorization_header=f"Bearer {wrong_signature}"
            )

    assert requests == 1
