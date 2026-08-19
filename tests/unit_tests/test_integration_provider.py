"""Unit tests for provider connection resolution boundaries."""

import pytest

from agent.integrations.models import (
    ProviderAuthentication,
    ProviderCapability,
    ProviderConnection,
    ProviderWebhookConnection,
)
from agent.integrations.provider import (
    ProviderConnectionNotFoundError,
    ProviderConnectionResolver,
    ProviderWebhookConnectionResolver,
)

pytestmark = pytest.mark.anyio


def _connection(tenant_id: str, capability: str) -> ProviderConnection:
    return ProviderConnection(
        connection_id=f"conn-{capability}",
        tenant_id=tenant_id,
        capability=capability,
        base_url="https://provider.example.com:8443",
        endpoint="/orders",
        authentication=ProviderAuthentication(scheme="bearer", credential="hunter2"),
    )


class StaticResolver:
    """Deterministic outbound resolver backed by a static mapping (test double)."""

    def __init__(
        self,
        connections: dict[tuple[str, str], ProviderConnection],
    ) -> None:
        self._connections = connections

    async def resolve(
        self,
        *,
        tenant_id: str,
        capability: ProviderCapability,
    ) -> ProviderConnection:
        try:
            return self._connections[(tenant_id, capability)]
        except KeyError:
            raise ProviderConnectionNotFoundError(
                f"no provider connection for {tenant_id}/{capability}"
            ) from None


class StaticWebhookResolver:
    """Deterministic inbound resolver keyed by connection id (test double)."""

    def __init__(self, connections: dict[str, ProviderWebhookConnection]) -> None:
        self._connections = connections

    async def resolve_webhook(
        self,
        *,
        provider_connection_id: str,
    ) -> ProviderWebhookConnection:
        try:
            return self._connections[provider_connection_id]
        except KeyError:
            raise ProviderConnectionNotFoundError(
                f"no webhook connection for {provider_connection_id}"
            ) from None


async def test_resolver_returns_connection_for_tenant_and_capability() -> None:
    expected = _connection("tenant-demo", "order_query")
    resolver = StaticResolver({("tenant-demo", "order_query"): expected})

    resolved = await resolver.resolve(tenant_id="tenant-demo", capability="order_query")

    assert resolved.connection_id == expected.connection_id
    assert resolved.tenant_id == "tenant-demo"


async def test_resolver_raises_domain_error_when_connection_missing() -> None:
    resolver = StaticResolver({})

    with pytest.raises(ProviderConnectionNotFoundError):
        await resolver.resolve(tenant_id="tenant-demo", capability="inventory_query")


async def test_webhook_resolver_returns_trusted_tenant_and_secret() -> None:
    webhook = ProviderWebhookConnection(
        connection_id="wc-1",
        tenant_id="tenant-demo",
        signing_secret="wh-secret",
    )
    resolver = StaticWebhookResolver({"wc-1": webhook})

    resolved = await resolver.resolve_webhook(provider_connection_id="wc-1")

    assert resolved.connection_id == "wc-1"
    assert resolved.tenant_id == "tenant-demo"
    assert resolved.signing_secret.get_secret_value() == "wh-secret"


async def test_webhook_resolver_raises_when_connection_missing() -> None:
    resolver = StaticWebhookResolver({})

    with pytest.raises(ProviderConnectionNotFoundError):
        await resolver.resolve_webhook(provider_connection_id="missing")


async def test_resolvers_satisfy_the_protocols() -> None:
    async def consume(
        resolver: ProviderConnectionResolver,
        webhook_resolver: ProviderWebhookConnectionResolver,
    ) -> None:
        assert resolver is not None
        assert webhook_resolver is not None

    await consume(StaticResolver({}), StaticWebhookResolver({}))
