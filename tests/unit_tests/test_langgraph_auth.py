"""Unit tests for the LangGraph authentication and authorization handlers."""

import pytest
from langgraph_sdk import Auth

import agent.auth.langgraph_auth as module
from agent.auth.demo_provider import DemoIdentityProvider
from agent.auth.provider import IdentityInfrastructureUnavailableError

pytestmark = pytest.mark.anyio


class FakeUser:
    """Expose the fields the auth handlers read from ctx.user."""

    def __init__(self, *, identity: str, permissions: list[str], role: str) -> None:
        self.identity = identity
        self.permissions = permissions
        self.role = role


class FakeCtx:
    """Provide the ctx.user contract consumed by resource handlers."""

    def __init__(self, user: FakeUser) -> None:
        self.user = user


_ROLE_PERMISSIONS = {
    "customer": ["orders:read:own", "orders:operate:own", "cases:read:own"],
    "support_agent": ["cases:read:assigned", "cases:update:assigned"],
    "supervisor": [
        "cases:read:own",
        "cases:read:assigned",
        "cases:read:all",
        "cases:update:assigned",
        "cases:update:all",
        "cases:assign",
        "provider_ops:read",
        "provider_ops:redrive",
    ],
}


def _ctx(role: str = "customer", user_id: str = "customer-a") -> FakeCtx:
    return FakeCtx(
        FakeUser(
            identity=f"tenant-demo:{user_id}",
            permissions=_ROLE_PERMISSIONS[role],
            role=role,
        )
    )


async def test_authenticate_rejects_missing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module, "get_identity_provider", lambda: DemoIdentityProvider({})
    )
    with pytest.raises(Auth.exceptions.HTTPException) as error:
        await module.authenticate(authorization=None)

    assert error.value.status_code == 401


async def test_authenticate_returns_trusted_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = DemoIdentityProvider(
        {
            "demo-token-customer-a": {
                "user_id": "customer-a",
                "tenant_id": "tenant-demo",
                "role": "customer",
            }
        }
    )
    monkeypatch.setattr(module, "get_identity_provider", lambda: provider)

    claims = await module.authenticate(authorization="Bearer demo-token-customer-a")

    assert claims["identity"] == "tenant-demo:customer-a"
    assert claims["role"] == "customer"
    assert claims["user_id"] == "customer-a"
    assert claims["tenant_id"] == "tenant-demo"
    assert "orders:operate:own" in claims["permissions"]


async def test_authenticate_maps_identity_outage_to_safe_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableProvider:
        async def resolve(self, *, authorization_header: str | None):
            raise IdentityInfrastructureUnavailableError(
                "GET https://idp.invalid/?token=must-not-leak"
            )

    provider = UnavailableProvider()
    monkeypatch.setattr(module, "get_identity_provider", lambda: provider)

    with pytest.raises(Auth.exceptions.HTTPException) as error:
        await module.authenticate(authorization="Bearer opaque")

    assert error.value.status_code == 503
    assert error.value.detail == "Identity service unavailable"
    assert "must-not-leak" not in str(error.value)


async def test_threads_create_stamps_ownership_metadata() -> None:
    value: dict = {"metadata": {}}

    await module.on_threads_create(_ctx(), value)

    assert value["metadata"] == {
        "owner_user_id": "customer-a",
        "tenant_id": "tenant-demo",
        "owner_role": "customer",
    }


async def test_threads_scoped_returns_owner_filter() -> None:
    result = await module.on_threads_scoped(_ctx(), {})

    assert result == {"owner_user_id": "customer-a", "tenant_id": "tenant-demo"}


async def test_threads_mutation_is_denied() -> None:
    assert await module.on_threads_mutation(_ctx(), {}) is False


async def test_assistants_read_is_allowed() -> None:
    assert await module.on_assistants_read(_ctx(), {}) is True


async def test_assistants_write_is_denied() -> None:
    assert await module.on_assistants_write(_ctx(), {}) is False
