"""Unit tests for the deterministic demo identity provider."""

import json

import pytest

from agent.auth.demo_provider import DemoIdentityProvider
from agent.auth.provider import UnauthenticatedError

_CUSTOMER_TOKEN = "demo-token-customer-a"


def _provider() -> DemoIdentityProvider:
    return DemoIdentityProvider(
        {
            _CUSTOMER_TOKEN: {
                "user_id": "customer-a",
                "tenant_id": "tenant-demo",
                "role": "customer",
            },
            "demo-token-agent-7": {
                "user_id": "agent-7",
                "tenant_id": "tenant-demo",
                "role": "support_agent",
            },
        }
    )


pytestmark = pytest.mark.anyio


async def test_resolve_derives_permissions_from_role() -> None:
    identity = await _provider().resolve(
        authorization_header=f"Bearer {_CUSTOMER_TOKEN}"
    )

    assert identity.user_id == "customer-a"
    assert identity.tenant_id == "tenant-demo"
    assert identity.role == "customer"
    assert identity.permissions == frozenset(
        {"orders:read:own", "orders:operate:own", "cases:read:own"}
    )


async def test_resolve_rejects_unknown_token() -> None:
    with pytest.raises(UnauthenticatedError):
        await _provider().resolve(authorization_header="Bearer unknown-token")


async def test_resolve_rejects_missing_header() -> None:
    with pytest.raises(UnauthenticatedError):
        await _provider().resolve(authorization_header=None)


async def test_resolve_rejects_non_bearer_scheme() -> None:
    with pytest.raises(UnauthenticatedError):
        await _provider().resolve(authorization_header=f"Basic {_CUSTOMER_TOKEN}")


async def test_from_env_defaults_to_empty_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEMO_IDENTITY_TOKENS", raising=False)

    provider = DemoIdentityProvider.from_env()

    with pytest.raises(UnauthenticatedError):
        await provider.resolve(authorization_header="Bearer anything")


def test_from_env_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_IDENTITY_TOKENS", "{not json")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        DemoIdentityProvider.from_env()


def test_from_env_rejects_non_object_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_IDENTITY_TOKENS", json.dumps(["a", "b"]))

    with pytest.raises(RuntimeError, match="JSON object"):
        DemoIdentityProvider.from_env()


async def test_provider_has_no_implicit_tokens() -> None:
    provider = DemoIdentityProvider({})

    with pytest.raises(UnauthenticatedError):
        await provider.resolve(authorization_header="Bearer anything")
