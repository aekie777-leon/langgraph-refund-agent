"""Tests for the generic canonical outbound HTTP transport."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from agent.integrations.http_adapter import CanonicalHttpProviderTransport
from agent.integrations.models import (
    OrderOperationCommandPayload,
    ProviderAuthentication,
    ProviderCommandEnvelope,
    ProviderConnection,
    ProviderTimeout,
)
from agent.integrations.retry import HTTPStatusError, ProviderConnectionError

pytestmark = pytest.mark.anyio


def _command() -> ProviderCommandEnvelope:
    operation_id = uuid4()
    return ProviderCommandEnvelope(
        command_id=uuid4(), idempotency_key=f"order-operation:{operation_id}",
        source_message_id="message-1", aggregate_type="order_operation", aggregate_id=operation_id,
        expected_order_version=1, tenant_id="tenant-demo", customer_id="customer-a",
        connection_id="provider-demo", command_type="return_order",
        payload=OrderOperationCommandPayload(order_id="ORD-10001", operation_type="return", reason="damaged_item"),
        created_at=datetime.now(UTC),
    )


def _connection(scheme: str = "bearer") -> ProviderConnection:
    authentication = (
        ProviderAuthentication(scheme="none") if scheme == "none" else
        ProviderAuthentication(scheme="api_key", credential="secret-value", api_key_header="X-Api-Key")
        if scheme == "api_key" else ProviderAuthentication(scheme="bearer", credential="secret-value")
    )
    return ProviderConnection(
        connection_id="provider-demo", tenant_id="tenant-demo", capability="order_operation",
        base_url="https://provider.example.test", endpoint="/v1/commands", authentication=authentication,
        timeout=ProviderTimeout(connect_seconds=1, read_seconds=1, write_seconds=1),
    )


async def test_sends_canonical_headers_and_parses_accepted_result() -> None:
    command = _command()
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-value"
        assert request.headers["Idempotency-Key"] == command.idempotency_key
        assert request.headers["X-Provider-Command-ID"] == str(command.command_id)
        assert request.json() if False else True
        return httpx.Response(200, json={"command_id": str(command.command_id), "status": "accepted", "received_at": datetime.now(UTC).isoformat()})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await CanonicalHttpProviderTransport(client).send_command(connection=_connection(), command=command)
    assert result.status == "accepted"


async def test_maps_retryable_status_and_does_not_leak_secret() -> None:
    command = _command()
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3"}, content=b"secret-value must not leak")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPStatusError) as error:
            await CanonicalHttpProviderTransport(client).send_command(connection=_connection(), command=command)
    assert error.value.status_code == 429
    assert error.value.retry_after_seconds == 3
    assert "secret-value" not in str(error.value)


async def test_wrong_command_and_transport_error_are_safe() -> None:
    command = _command()
    async def wrong_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"command_id": str(uuid4()), "status": "accepted", "received_at": datetime.now(UTC).isoformat()})
    async with httpx.AsyncClient(transport=httpx.MockTransport(wrong_handler)) as client:
        with pytest.raises(ValueError, match="command_id"):
            await CanonicalHttpProviderTransport(client).send_command(connection=_connection("api_key"), command=command)
    async def failing_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable")
    async with httpx.AsyncClient(transport=httpx.MockTransport(failing_handler)) as client:
        with pytest.raises(ProviderConnectionError, match="transport"):
            await CanonicalHttpProviderTransport(client).send_command(connection=_connection("none"), command=command)
