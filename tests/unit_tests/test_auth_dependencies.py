"""Unit tests for the internal-API identity dependency."""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from agent.auth.demo_provider import DemoIdentityProvider
from agent.auth.dependencies import parse_access_scope, require_access_scope
from agent.auth.models import AccessScope
from agent.auth.provider import UnauthenticatedError


def _provider() -> DemoIdentityProvider:
    return DemoIdentityProvider(
        {
            "demo-token-customer-a": {
                "user_id": "customer-a",
                "tenant_id": "tenant-demo",
                "role": "customer",
            }
        }
    )


def test_parse_access_scope_resolves_a_trusted_scope() -> None:
    scope = parse_access_scope(
        authorization_header="Bearer demo-token-customer-a",
        provider=_provider(),
    )

    assert scope.user_id == "customer-a"
    assert scope.tenant_id == "tenant-demo"
    assert scope.role == "customer"


def test_parse_access_scope_rejects_unknown_tokens() -> None:
    with pytest.raises(UnauthenticatedError):
        parse_access_scope(
            authorization_header="Bearer unknown",
            provider=_provider(),
        )


@pytest.mark.anyio
async def test_fastapi_dependency_maps_missing_credentials_to_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEMO_IDENTITY_TOKENS", raising=False)
    app = FastAPI()

    @app.get("/protected")
    async def protected_route(
        scope: Annotated[AccessScope, Depends(require_access_scope)],
    ) -> dict[str, str]:
        return {"user_id": scope.user_id}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/protected")

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"
