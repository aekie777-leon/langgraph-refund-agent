"""Startup-loaded trusted inbound provider webhook connections."""

import json
import os
from collections.abc import Mapping

from pydantic import TypeAdapter, ValidationError

from agent.integrations.models import ProviderWebhookConnection
from agent.integrations.provider import (
    ProviderConnectionNotFoundError,
    ProviderWebhookConnectionResolver,
)

_ENV = "PROVIDER_WEBHOOK_CONNECTIONS_JSON"
_ADAPTER = TypeAdapter(tuple[ProviderWebhookConnection, ...])


class EnvironmentProviderWebhookConnectionResolver(ProviderWebhookConnectionResolver):
    """Resolve immutable inbound signing configuration loaded at startup."""

    def __init__(self, connections: tuple[ProviderWebhookConnection, ...]) -> None:
        """Index validated configurations by their trusted connection ID."""
        self._connections = {item.connection_id: item for item in connections}
        if len(self._connections) != len(connections):
            raise ValueError("duplicate provider webhook connection")

    @classmethod
    def from_environment(
        cls, *, environment: Mapping[str, str] | None = None
    ) -> "EnvironmentProviderWebhookConnectionResolver":
        """Parse the complete webhook connection configuration once."""
        raw = (os.environ if environment is None else environment).get(_ENV)
        if raw is None or not raw.strip():
            return cls(())
        try:
            connections = _ADAPTER.validate_python(json.loads(raw))
        except (json.JSONDecodeError, ValidationError):
            raise ValueError(
                "provider webhook connection configuration is invalid"
            ) from None
        return cls(connections)

    async def resolve_webhook(
        self, *, provider_connection_id: str
    ) -> ProviderWebhookConnection:
        """Return the trusted inbound configuration for a path connection ID."""
        try:
            return self._connections[provider_connection_id]
        except KeyError:
            raise ProviderConnectionNotFoundError(
                "no trusted provider webhook connection"
            ) from None
