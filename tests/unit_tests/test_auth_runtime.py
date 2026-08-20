"""Cross-surface tests for the single process-wide identity runtime."""

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from jwt.algorithms import RSAAlgorithm

import agent.auth.langgraph_auth as langgraph_auth
from agent.auth.dependencies import require_access_scope
from agent.auth.models import AccessScope
from agent.auth.preflight import SCIM_DIRECTORY_COMPONENT
from agent.auth.provider import IdentityConfigurationError
from agent.auth.runtime import (
    configure_identity_runtime,
    create_identity_runtime,
    get_identity_runtime,
    initialize_identity_runtime,
    shutdown_identity_runtime,
)

pytestmark = pytest.mark.anyio

_ISSUER = "https://identity.example.test/"
_JWKS_URL = "https://identity.example.test/jwks.json"


def _oidc_environment(*, mode: str = "test") -> dict[str, str]:
    environment = {
        "APP_ENV": mode,
        "IDENTITY_PROVIDER": "oidc",
        "IDENTITY_DIRECTORY": "none",
        "OIDC_ISSUER": _ISSUER,
        "OIDC_AUDIENCE": "refund-agent",
        "OIDC_JWKS_URL": _JWKS_URL,
        "OIDC_ALLOWED_ALGORITHMS": "RS256",
        "OIDC_USER_ID_CLAIM": "app_user_id",
        "OIDC_TENANT_ID_CLAIM": "app_tenant_id",
        "OIDC_GROUPS_CLAIM": "app_groups",
        "OIDC_ROLE_GROUPS_JSON": json.dumps(
            {
                "customer": ["refund-customers"],
                "support_agent": ["refund-agents"],
                "supervisor": ["refund-supervisors"],
            }
        ),
    }
    if mode == "production":
        environment.update(
            {
                "IDENTITY_DIRECTORY": "scim",
                "SCIM_BASE_URL": "https://directory.example.test/scim/v2",
                "SCIM_BEARER_TOKEN": "test-secret",
                "SCIM_USER_ID_ATTRIBUTE": "externalId",
                "SCIM_TENANT_ID_ATTRIBUTE": "tenantId",
                "SCIM_ACTIVE_ATTRIBUTE": "active",
                "SCIM_ROLES_ATTRIBUTE": "roles",
                "SCIM_ROLE_MAPPING_JSON": json.dumps(
                    {
                        "support_agent": ["Refund Agent"],
                        "supervisor": ["Refund Supervisor"],
                    }
                ),
            }
        )
    return environment


def _signing_material():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    assert isinstance(jwk, dict)
    return private_key, {**jwk, "kid": "key-1", "alg": "RS256", "use": "sig"}


def _token(private_key) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": _ISSUER,
            "aud": "refund-agent",
            "iat": now - timedelta(seconds=1),
            "nbf": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "app_user_id": "agent-7",
            "app_tenant_id": "tenant-demo",
            "app_groups": ["refund-agents"],
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )


async def test_fastapi_and_langgraph_share_exact_oidc_access_scope() -> None:
    private_key, jwk = _signing_material()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = create_identity_runtime(
            _oidc_environment(),
            studio_auth_disabled=True,
            http_client=client,
        )
        configure_identity_runtime(runtime)
        app = FastAPI()

        @app.get("/scope")
        async def scope_route(
            scope: Annotated[AccessScope, Depends(require_access_scope)],
        ) -> dict[str, Any]:
            return {
                "tenant_id": scope.tenant_id,
                "user_id": scope.user_id,
                "role": scope.role,
                "permissions": sorted(scope.permissions),
            }

        token = _token(private_key)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as api_client:
                response = await api_client.get(
                    "/scope", headers={"Authorization": f"Bearer {token}"}
                )
            langgraph_claims = await langgraph_auth.authenticate(
                authorization=f"Bearer {token}"
            )
        finally:
            await shutdown_identity_runtime()

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": langgraph_claims["tenant_id"],
        "user_id": langgraph_claims["user_id"],
        "role": langgraph_claims["role"],
        "permissions": sorted(langgraph_claims["permissions"]),
    }


async def test_production_startup_preflights_oidc_and_scim() -> None:
    _private_key, jwk = _signing_material()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/jwks.json"):
            return httpx.Response(200, json={"keys": [jwk]})
        return httpx.Response(
            200,
            json={
                "schemas": [
                    "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = await initialize_identity_runtime(
            _oidc_environment(mode="production"),
            studio_auth_disabled=True,
            http_client=client,
        )
        try:
            assert runtime.config.mode == "production"
        finally:
            await shutdown_identity_runtime()

    assert paths == ["/jwks.json", "/scim/v2/ServiceProviderConfig"]


async def test_production_rejects_studio_bypass_before_network_access() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IdentityConfigurationError):
            create_identity_runtime(
                _oidc_environment(mode="production"),
                studio_auth_disabled=False,
                http_client=client,
            )

    assert requests == 0


async def test_production_jwks_preflight_outage_does_not_publish_runtime() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("must-not-leak")

    async def scim_probe() -> None:
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError) as error:
            await initialize_identity_runtime(
                _oidc_environment(mode="production"),
                studio_auth_disabled=True,
                http_client=client,
                additional_probes={SCIM_DIRECTORY_COMPONENT: scim_probe},
            )

    assert "must-not-leak" not in str(error.value)
    with pytest.raises(RuntimeError, match="startup has not completed"):
        get_identity_runtime()
