"""Cross-component OIDC, SCIM, FastAPI, LangGraph, and PostgreSQL acceptance."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from jwt.algorithms import RSAAlgorithm
from psycopg_pool import AsyncConnectionPool

import agent.auth.langgraph_auth as langgraph_auth
from agent.auth.dependencies import require_access_scope
from agent.auth.models import AccessScope
from agent.auth.runtime import initialize_identity_runtime, shutdown_identity_runtime
from agent.cases.api import router as case_router
from agent.cases.api_errors import register_case_exception_handlers
from agent.cases.models import CaseTrigger, HandoffPolicyInput
from agent.cases.policy import determine_handoff_policy
from agent.cases.postgres_repository import PostgresCaseRepository
from agent.cases.runtime import get_case_service
from agent.cases.service import CaseService
from agent.database import create_async_connection_pool
from agent.migrations import apply_migrations
from tests.fakes.identity import make_scope

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]

_ISSUER = "https://identity.example.test/"
_JWKS_URL = "https://identity.example.test/jwks.json"
_SCIM_BASE_URL = "https://directory.example.test/scim/v2"
_SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_SCIM_SERVICE_SCHEMA = (
    "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
)
_NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
_CUSTOMER_SCOPE = make_scope("customer")


@pytest.fixture
def anyio_backend() -> str | tuple[str, dict[str, object]]:
    if os.name == "nt":
        return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}
    return "asyncio"


@pytest.fixture
async def postgres_context() -> AsyncIterator[tuple[AsyncConnectionPool, str]]:
    conninfo = os.getenv("CASE_TEST_POSTGRES_URI")
    if not conninfo:
        pytest.skip("CASE_TEST_POSTGRES_URI is not configured")

    apply_migrations(conninfo)
    pool = create_async_connection_pool(conninfo, min_size=1, max_size=4)
    await pool.open()
    await pool.wait(timeout=10)
    thread_id = f"identity-e2e-{uuid4()}"
    try:
        yield pool, thread_id
    finally:
        async with pool.connection() as connection:
            await connection.execute(
                """
                DELETE FROM case_management.support_case_events AS events
                USING case_management.support_cases AS cases
                WHERE events.case_id = cases.case_id
                  AND cases.thread_id = %s
                """,
                (thread_id,),
            )
            await connection.execute(
                """
                DELETE FROM case_management.support_cases
                WHERE thread_id = %s
                """,
                (thread_id,),
            )
        await pool.close()


def _production_environment() -> dict[str, str]:
    return {
        "APP_ENV": "production",
        "IDENTITY_PROVIDER": "oidc",
        "IDENTITY_DIRECTORY": "scim",
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
        "SCIM_BASE_URL": _SCIM_BASE_URL,
        "SCIM_BEARER_TOKEN": "scim-e2e-secret",
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


def _signing_material() -> tuple[Any, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    assert isinstance(jwk, dict)
    return private_key, {**jwk, "kid": "e2e-key", "alg": "RS256", "use": "sig"}


def _supervisor_token(private_key: Any) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": _ISSUER,
            "aud": "refund-agent",
            "iat": now - timedelta(seconds=1),
            "nbf": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "app_user_id": "sup-1",
            "app_tenant_id": "tenant-demo",
            "app_groups": ["refund-supervisors"],
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "e2e-key"},
    )


def _handoff_trigger(thread_id: str) -> CaseTrigger:
    return CaseTrigger(
        thread_id=thread_id,
        source_message_id="identity-e2e-message",
        order_id="ORD-10001",
        risk_level="medium",
        risk_categories=("self_harm",),
        triggering_message_excerpt="Please connect me to support.",
    )


async def test_production_identity_assignment_cross_component_e2e(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    private_key, jwk = _signing_material()
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/jwks.json":
            return httpx.Response(200, json={"keys": [jwk]})
        if request.url.path == "/scim/v2/ServiceProviderConfig":
            return httpx.Response(200, json={"schemas": [_SCIM_SERVICE_SCHEMA]})
        if request.url.path == "/scim/v2/Users":
            assert request.headers["authorization"] == "Bearer scim-e2e-secret"
            return httpx.Response(
                200,
                json={
                    "schemas": [_SCIM_LIST_SCHEMA],
                    "totalResults": 1,
                    "Resources": [
                        {
                            "schemas": [_SCIM_USER_SCHEMA],
                            "externalId": "agent-7",
                            "tenantId": "tenant-demo",
                            "active": True,
                            "roles": [{"value": "Refund Agent"}],
                        }
                    ],
                },
            )
        return httpx.Response(404)

    repository = PostgresCaseRepository(pool)
    created = await CaseService(repository, clock=lambda: _NOW).record_handoff(
        _CUSTOMER_SCOPE,
        trigger=_handoff_trigger(thread_id),
        decision=determine_handoff_policy(
            HandoffPolicyInput(
                semantic_risk_level="medium",
                semantic_risk_categories=("self_harm",),
            )
        ),
    )
    assert created.case is not None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = await initialize_identity_runtime(
            _production_environment(),
            studio_auth_disabled=True,
            http_client=client,
        )
        assert runtime.directory is not None
        service = CaseService(
            repository,
            identity_directory=runtime.directory,
            clock=lambda: _NOW,
        )
        app = FastAPI()
        app.include_router(case_router)
        register_case_exception_handlers(app)
        app.dependency_overrides[get_case_service] = lambda: service

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

        token = _supervisor_token(private_key)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            langgraph_claims = await langgraph_auth.authenticate(
                authorization=headers["Authorization"]
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as api_client:
                scope_response = await api_client.get("/scope", headers=headers)
                forwarded_only = await api_client.get(
                    "/scope",
                    headers={
                        "X-Forwarded-User": "sup-1",
                        "X-Forwarded-Tenant": "tenant-demo",
                    },
                )
                assignment = await api_client.post(
                    f"/internal/support-cases/{created.case.case_id}/assign",
                    headers=headers,
                    json={
                        "agent_id": "agent-7",
                        "request_id": "identity-e2e-assignment",
                    },
                )
        finally:
            await shutdown_identity_runtime()

    assert scope_response.status_code == 200
    assert scope_response.json() == {
        "tenant_id": langgraph_claims["tenant_id"],
        "user_id": langgraph_claims["user_id"],
        "role": langgraph_claims["role"],
        "permissions": sorted(langgraph_claims["permissions"]),
    }
    assert forwarded_only.status_code == 401
    assert assignment.status_code == 200
    assert assignment.json()["action"] == "assigned"
    assert assignment.json()["event"]["actor"] == "tenant-demo:sup-1"
    assert assignment.json()["case"]["assigned_agent_id"] == "agent-7"
    assert requests == [
        ("GET", "/jwks.json"),
        ("GET", "/scim/v2/ServiceProviderConfig"),
        ("GET", "/scim/v2/Users"),
    ]
