"""Controlled-HTTP tests for the read-only SCIM 2.0 directory adapter."""

import json

import httpx
import pytest

from agent.auth.config import ScimDirectoryConfig
from agent.auth.directory import DirectoryInfrastructureUnavailableError
from agent.auth.scim_directory import ScimIdentityDirectory

pytestmark = pytest.mark.anyio

_BASE_URL = "https://directory.example.test/scim/v2"
_SECRET = "scim-test-secret"
_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_SERVICE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"


def _config(**updates) -> ScimDirectoryConfig:
    values = {
        "base_url": _BASE_URL,
        "bearer_token": _SECRET,
        "user_id_attribute": "externalId",
        "tenant_id_attribute": "urn:example:params:scim:schemas:extension:2.0:User:tenantId",
        "active_attribute": "active",
        "roles_attribute": "roles",
        "role_mapping": {
            "support_agent": frozenset({"Refund Agent"}),
            "supervisor": frozenset({"Refund Supervisor"}),
        },
        "timeout_seconds": 5.0,
    }
    values.update(updates)
    return ScimDirectoryConfig.model_validate(values)


def _list_response(resources: list[dict]) -> dict:
    return {
        "schemas": [_LIST_SCHEMA],
        "totalResults": len(resources),
        "startIndex": 1,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def _user(
    *,
    user_id: str = "agent-7",
    tenant_id: str = "tenant-demo",
    active: bool = True,
    roles: list | None = None,
) -> dict:
    return {
        "schemas": [_USER_SCHEMA, "urn:example:params:scim:schemas:extension:2.0:User"],
        "id": "opaque-scim-id",
        "externalId": user_id,
        "active": active,
        "roles": roles if roles is not None else [{"value": "Refund Agent"}],
        "urn:example:params:scim:schemas:extension:2.0:User": {
            "tenantId": tenant_id
        },
    }


async def test_scim_query_uses_exact_filter_projection_and_maps_roles() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_list_response([_user()]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        directory = ScimIdentityDirectory(_config(), client)
        user = await directory.find_user(
            tenant_id="tenant-demo",
            user_id="agent-7",
        )

    assert user is not None
    assert user.tenant_id == "tenant-demo"
    assert user.user_id == "agent-7"
    assert user.active is True
    assert user.roles == frozenset({"support_agent"})
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/scim/v2/Users"
    assert request.url.params["filter"] == (
        'externalId eq "agent-7" and '
        'urn:example:params:scim:schemas:extension:2.0:User:tenantId '
        'eq "tenant-demo"'
    )
    assert request.url.params["count"] == "2"
    assert request.headers["accept"] == "application/scim+json"
    assert request.headers["authorization"] == f"Bearer {_SECRET}"


async def test_scim_filter_json_escapes_untrusted_identity_values() -> None:
    captured_filter = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_filter
        captured_filter = request.url.params["filter"]
        return httpx.Response(200, json=_list_response([]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        directory = ScimIdentityDirectory(_config(), client)
        assert (
            await directory.find_user(
                tenant_id="tenant-demo",
                user_id='agent" or userName pr or userName eq "x',
            )
            is None
        )

    encoded = json.dumps('agent" or userName pr or userName eq "x')
    assert captured_filter.startswith(f"externalId eq {encoded} and ")


async def test_missing_user_returns_none_without_enumeration_detail() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_list_response([]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        directory = ScimIdentityDirectory(_config(), client)
        assert (
            await directory.find_user(
                tenant_id="tenant-demo",
                user_id="missing-agent",
            )
            is None
        )


@pytest.mark.parametrize(
    "payload",
    [
        _list_response([_user(), _user(user_id="agent-8")]),
        {
            "schemas": [_LIST_SCHEMA],
            "totalResults": 1,
            "Resources": [],
        },
        _list_response([_user(roles=[{"display": "missing-value"}])]),
        {"Resources": []},
    ],
)
async def test_ambiguous_or_malformed_scim_response_fails_closed(payload: dict) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        directory = ScimIdentityDirectory(_config(), client)
        with pytest.raises(DirectoryInfrastructureUnavailableError):
            await directory.find_user(
                tenant_id="tenant-demo",
                user_id="agent-7",
            )


@pytest.mark.parametrize(
    "payload",
    [
        _list_response([_user(tenant_id="tenant-other")]),
        _list_response([_user(user_id="agent-other")]),
    ],
)
async def test_mismatched_filtered_identity_is_treated_as_not_found(
    payload: dict,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        directory = ScimIdentityDirectory(_config(), client)
        assert (
            await directory.find_user(
                tenant_id="tenant-demo",
                user_id="agent-7",
            )
            is None
        )


async def test_scim_outage_is_safe_and_does_not_expose_secret_or_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"token={_SECRET}&body=must-not-leak")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        directory = ScimIdentityDirectory(_config(), client)
        with pytest.raises(DirectoryInfrastructureUnavailableError) as error:
            await directory.find_user(
                tenant_id="tenant-demo",
                user_id="agent-7",
            )

    assert str(error.value) == "identity infrastructure is unavailable"
    assert _SECRET not in str(error.value)
    assert "must-not-leak" not in str(error.value)


async def test_scim_preflight_uses_read_only_service_provider_config() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"schemas": [_SERVICE_SCHEMA]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ScimIdentityDirectory(_config(), client).preflight()

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/scim/v2/ServiceProviderConfig")
    ]
