"""Provider connection resolution boundaries.

Outbound connections (API reads and commands) and inbound webhook connections
(signing secrets) are resolved by separate resolvers. Implementations receive
their configuration through the constructor and must not read environment
variables, Vault, or databases during resolution.
"""

from typing import Protocol

from agent.integrations.models import (
    ProviderCapability,
    ProviderCommandEnvelope,
    ProviderCommandResult,
    ProviderConnection,
    ProviderWebhookConnection,
)


class ProviderCommandTransport(Protocol):
    """Submit one canonical command through a resolved provider connection."""

    async def send_command(
        self,
        *,
        connection: ProviderConnection,
        command: ProviderCommandEnvelope,
    ) -> ProviderCommandResult:
        """Return the provider's immediate accepted/rejected result."""
        ...


class ProviderConnectionNotFoundError(LookupError):
    """Report that no connection exists for a tenant and capability."""


class ProviderConnectionResolver(Protocol):
    """Resolve an outbound provider connection for a tenant and capability."""

    async def resolve(
        self,
        *,
        tenant_id: str,
        capability: ProviderCapability,
    ) -> ProviderConnection:
        """Return the connection or raise ProviderConnectionNotFoundError."""
        ...


class ProviderConnectionLookup(Protocol):
    """Resolve the exact connection pinned by a persisted command."""

    async def resolve_by_connection_id(
        self,
        *,
        connection_id: str,
        capability: ProviderCapability,
    ) -> ProviderConnection:
        """Return one connection or raise ProviderConnectionNotFoundError."""
        ...


class ProviderWebhookConnectionResolver(Protocol):
    """Resolve the trusted inbound webhook configuration by connection id."""

    async def resolve_webhook(
        self,
        *,
        provider_connection_id: str,
    ) -> ProviderWebhookConnection:
        """Return the trusted tenant and signing secret.

        The tenant is decided by this resolver from ``provider_connection_id``
        only; it must never be taken from the webhook request body.
        """
        ...
