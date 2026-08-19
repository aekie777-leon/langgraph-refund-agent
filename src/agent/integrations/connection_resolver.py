"""Immutable provider-connection configuration for outbound dispatch."""

import json
import os
from collections.abc import Mapping

from pydantic import TypeAdapter, ValidationError

from agent.integrations.models import ProviderCapability, ProviderConnection
from agent.integrations.provider import (
    ProviderConnectionLookup,
    ProviderConnectionNotFoundError,
    ProviderConnectionResolver,
)

_CONNECTIONS_ENVIRONMENT_VARIABLE = "PROVIDER_CONNECTIONS_JSON"
_CONNECTIONS_ADAPTER = TypeAdapter(tuple[ProviderConnection, ...])


class EnvironmentProviderConnectionResolver(
    ProviderConnectionResolver,
    ProviderConnectionLookup,
):
    """Resolve validated, startup-loaded outbound provider connections.

    The resolver intentionally reads the environment only in ``from_environment``.
    Command dispatch can therefore never silently switch connection because a
    process environment variable changed while an outbox row was waiting.
    """

    def __init__(
        self,
        connections: tuple[ProviderConnection, ...],
        *,
        allow_insecure_http: bool = False,
    ) -> None:
        """Validate and index the immutable connection collection."""
        by_selection: dict[tuple[str, ProviderCapability], ProviderConnection] = {}
        by_id_and_capability: dict[tuple[str, ProviderCapability], ProviderConnection] = {}
        for connection in connections:
            if connection.base_url.scheme != "https" and not allow_insecure_http:
                raise ValueError(
                    "provider connections must use https outside explicit development mode"
                )
            selection = (connection.tenant_id, connection.capability)
            if selection in by_selection:
                raise ValueError(
                    "duplicate provider connection for tenant/capability: "
                    f"{connection.tenant_id}/{connection.capability}"
                )
            existing = next(
                (
                    configured
                    for (configured_id, _), configured in by_id_and_capability.items()
                    if configured_id == connection.connection_id
                ),
                None,
            )
            if existing is not None and _connection_identity(existing) != _connection_identity(connection):
                raise ValueError(
                    "connections sharing connection_id must have identical transport settings"
                )
            by_selection[selection] = connection
            by_id_and_capability[(connection.connection_id, connection.capability)] = connection
        self._by_selection = by_selection
        self._by_id_and_capability = by_id_and_capability

    @classmethod
    def from_environment(
        cls,
        *,
        environment: Mapping[str, str] | None = None,
        allow_insecure_http: bool = False,
    ) -> "EnvironmentProviderConnectionResolver":
        """Load the complete connection list once from PROVIDER_CONNECTIONS_JSON."""
        source = os.environ if environment is None else environment
        raw = source.get(_CONNECTIONS_ENVIRONMENT_VARIABLE)
        if raw is None or not raw.strip():
            raise ValueError(f"{_CONNECTIONS_ENVIRONMENT_VARIABLE} must be configured")
        try:
            decoded = json.loads(raw)
            connections = _CONNECTIONS_ADAPTER.validate_python(decoded)
        except (json.JSONDecodeError, ValidationError):
            # ValidationError can retain the untrusted raw configuration in its
            # context, including credentials.  Do not retain it as a cause.
            raise ValueError("provider connection configuration is invalid") from None
        if not connections:
            raise ValueError("provider connection configuration must not be empty")
        return cls(connections, allow_insecure_http=allow_insecure_http)

    async def resolve(
        self,
        *,
        tenant_id: str,
        capability: ProviderCapability,
    ) -> ProviderConnection:
        """Return the configured connection for a tenant/capability pair."""
        try:
            return self._by_selection[(tenant_id, capability)]
        except KeyError as error:
            raise ProviderConnectionNotFoundError(
                f"no provider connection for {tenant_id}/{capability}"
            ) from error

    async def resolve_by_connection_id(
        self,
        *,
        connection_id: str,
        capability: ProviderCapability,
    ) -> ProviderConnection:
        """Return the exact connection pinned in a persisted outbox envelope."""
        try:
            return self._by_id_and_capability[(connection_id, capability)]
        except KeyError as error:
            raise ProviderConnectionNotFoundError(
                "no provider connection for persisted command"
            ) from error


def _connection_identity(connection: ProviderConnection) -> tuple[object, ...]:
    """Return fields that must agree when capabilities share a connection."""
    return (
        str(connection.base_url),
        connection.authentication.scheme,
        connection.authentication.credential.get_secret_value()
        if connection.authentication.credential is not None
        else None,
        connection.authentication.api_key_header,
        connection.timeout,
        connection.max_concurrency,
        connection.requests_per_second,
    )
