"""Unit tests for startup-loaded outbound connection configuration."""

import pytest

from agent.integrations.connection_resolver import EnvironmentProviderConnectionResolver
from agent.integrations.provider import ProviderConnectionNotFoundError

pytestmark = pytest.mark.anyio


def _config(*, connection_id: str = "provider-demo", capability: str = "order_operation") -> str:
    return (
        "[{"
        f'"connection_id":"{connection_id}","tenant_id":"tenant-demo",'
        f'"capability":"{capability}","base_url":"https://provider.example.test",'
        '"endpoint":"/v1/commands","authentication":{"scheme":"bearer",'
        '"credential":"test-secret"}}]'
    )


async def test_resolves_by_selection_and_pinned_connection_id() -> None:
    resolver = EnvironmentProviderConnectionResolver.from_environment(
        environment={"PROVIDER_CONNECTIONS_JSON": _config()}
    )

    selected = await resolver.resolve(tenant_id="tenant-demo", capability="order_operation")
    pinned = await resolver.resolve_by_connection_id(
        connection_id="provider-demo", capability="order_operation"
    )

    assert selected is pinned
    assert selected.authentication.model_dump()["credential"] is None


async def test_rejects_duplicate_tenant_capability_and_missing_connection() -> None:
    duplicate = "[" + _config()[1:-1] + "," + _config()[1:-1] + "]"
    with pytest.raises(ValueError, match="duplicate provider connection"):
        EnvironmentProviderConnectionResolver.from_environment(
            environment={"PROVIDER_CONNECTIONS_JSON": duplicate}
        )
    resolver = EnvironmentProviderConnectionResolver.from_environment(
        environment={"PROVIDER_CONNECTIONS_JSON": _config()}
    )
    with pytest.raises(ProviderConnectionNotFoundError):
        await resolver.resolve_by_connection_id(
            connection_id="missing", capability="order_operation"
        )


def test_rejects_http_without_explicit_development_switch_and_invalid_json() -> None:
    insecure = _config().replace("https://", "http://")
    with pytest.raises(ValueError, match="https"):
        EnvironmentProviderConnectionResolver.from_environment(
            environment={"PROVIDER_CONNECTIONS_JSON": insecure}
        )
    resolver = EnvironmentProviderConnectionResolver.from_environment(
        environment={"PROVIDER_CONNECTIONS_JSON": insecure},
        allow_insecure_http=True,
    )
    assert resolver is not None
    with pytest.raises(ValueError, match="invalid"):
        EnvironmentProviderConnectionResolver.from_environment(
            environment={"PROVIDER_CONNECTIONS_JSON": "not-json"}
        )
