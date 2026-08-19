"""Security contract tests for startup-loaded webhook configuration."""

import json

import pytest

from agent.integrations.provider import ProviderConnectionNotFoundError
from agent.integrations.webhook_resolver import (
    EnvironmentProviderWebhookConnectionResolver,
)

SECRET = "resolver-secret-must-not-leak"


def _configuration(*items: dict[str, object]) -> str:
    return json.dumps(list(items))


def _connection(
    connection_id: str = "webhook-one",
    *,
    tenant_id: str = "tenant-one",
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "connection_id": connection_id,
        "tenant_id": tenant_id,
        "signing_secret": SECRET,
        "validity_window_seconds": 300,
    }
    values.update(overrides)
    return values


@pytest.mark.anyio
async def test_missing_empty_and_blank_environment_create_an_empty_resolver() -> None:
    for raw in (None, "", " \t "):
        environment = {} if raw is None else {"PROVIDER_WEBHOOK_CONNECTIONS_JSON": raw}
        resolver = EnvironmentProviderWebhookConnectionResolver.from_environment(
            environment=environment
        )

        with pytest.raises(ProviderConnectionNotFoundError) as caught:
            await resolver.resolve_webhook(provider_connection_id="unknown-id")

        assert str(caught.value) == "no trusted provider webhook connection"
        assert "unknown-id" not in repr(caught.value)


@pytest.mark.anyio
async def test_resolver_returns_each_trusted_connection_from_startup_snapshot() -> None:
    environment = {
        "PROVIDER_WEBHOOK_CONNECTIONS_JSON": _configuration(
            _connection(), _connection("webhook-two", tenant_id="tenant-two")
        )
    }
    resolver = EnvironmentProviderWebhookConnectionResolver.from_environment(
        environment=environment
    )
    environment["PROVIDER_WEBHOOK_CONNECTIONS_JSON"] = _configuration(
        _connection("mutated", tenant_id="tenant-mutated", signing_secret="changed")
    )

    first = await resolver.resolve_webhook(provider_connection_id="webhook-one")
    second = await resolver.resolve_webhook(provider_connection_id="webhook-two")

    assert (first.connection_id, first.tenant_id) == ("webhook-one", "tenant-one")
    assert (second.connection_id, second.tenant_id) == ("webhook-two", "tenant-two")
    assert SECRET not in repr(first)
    assert SECRET not in str(first)
    assert SECRET not in str(first.model_dump())


@pytest.mark.anyio
async def test_unknown_connection_has_a_fixed_safe_error() -> None:
    resolver = EnvironmentProviderWebhookConnectionResolver.from_environment(
        environment={"PROVIDER_WEBHOOK_CONNECTIONS_JSON": _configuration(_connection())}
    )

    with pytest.raises(ProviderConnectionNotFoundError) as caught:
        await resolver.resolve_webhook(
            provider_connection_id="unknown-tenant-sensitive"
        )

    assert str(caught.value) == "no trusted provider webhook connection"
    assert "unknown-tenant-sensitive" not in repr(caught.value)
    assert SECRET not in repr(caught.value)


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "{}",
        "[1]",
        _configuration({"connection_id": "webhook-one"}),
        _configuration(_connection(unexpected="not-allowed")),
        _configuration(_connection(signing_secret="")),
        _configuration(_connection(validity_window_seconds=0)),
        _configuration(_connection(validity_window_seconds=float("nan"))),
        _configuration(_connection(validity_window_seconds=float("inf"))),
        _configuration(
            _connection(), _connection("webhook-one", tenant_id="tenant-two")
        ),
        _configuration(
            _connection(), {"connection_id": "broken", "signing_secret": SECRET}
        ),
    ],
)
def test_invalid_configuration_rejects_the_entire_snapshot_without_leaking(
    raw: str,
) -> None:
    with pytest.raises(ValueError) as caught:
        EnvironmentProviderWebhookConnectionResolver.from_environment(
            environment={"PROVIDER_WEBHOOK_CONNECTIONS_JSON": raw}
        )

    assert str(caught.value) in {
        "provider webhook connection configuration is invalid",
        "duplicate provider webhook connection",
    }
    assert caught.value.__cause__ is None
    assert SECRET not in repr(caught.value)
    assert SECRET not in str(caught.value)
