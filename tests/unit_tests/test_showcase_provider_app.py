"""Tests for the local signed-callback Provider boundary."""

import json
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from agent.integrations.models import ProviderCommandEnvelope, ProviderCommandResult
from agent.integrations.signing import signature_is_valid
from agent.showcase import provider_app

pytestmark = pytest.mark.anyio


async def test_showcase_provider_has_a_payload_free_health_endpoint() -> None:
    transport = httpx.ASGITransport(app=provider_app.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://provider.test"
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_showcase_provider_emits_a_canonical_signed_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(202, json={"status": "accepted"})

    real_client = httpx.AsyncClient

    class Client:
        async def __aenter__(self):
            self._client = real_client(transport=httpx.MockTransport(handler))
            return await self._client.__aenter__()

        async def __aexit__(self, *args):
            return await self._client.__aexit__(*args)

    monkeypatch.setenv("SHOWCASE_PROVIDER_CALLBACK_URL", "http://api.test/webhooks/providers/provider-demo")
    monkeypatch.setenv("SHOWCASE_PROVIDER_WEBHOOK_SECRET", "synthetic-showcase-secret")
    monkeypatch.setattr(provider_app.httpx, "AsyncClient", lambda **_kwargs: Client())
    command = ProviderCommandEnvelope.model_validate(
        {
            "command_id": "11111111-1111-1111-1111-111111111111",
            "idempotency_key": "order-operation:22222222-2222-2222-2222-222222222222",
            "source_message_id": "showcase-message",
            "aggregate_type": "order_operation",
            "aggregate_id": "22222222-2222-2222-2222-222222222222",
            "expected_order_version": 1,
            "tenant_id": "tenant-demo",
            "customer_id": "customer-a",
            "connection_id": "provider-demo",
            "command_type": "cancel_order",
            "payload": {
                "order_id": "ORD-10008",
                "operation_type": "cancellation",
                "reason": "no_longer_needed",
            },
            "created_at": datetime.now(UTC),
        }
    )
    result = ProviderCommandResult(
        command_id=UUID("11111111-1111-1111-1111-111111111111"),
        status="accepted",
        provider_operation_id="showcase-operation",
        provider_reference="showcase-reference",
        received_at=datetime.now(UTC),
    )

    await provider_app._send_completed_callback(command, result)

    request = captured["request"]
    assert isinstance(request, httpx.Request)
    body = json.loads(request.content)
    timestamp = datetime.strptime(
        request.headers["x-provider-timestamp"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC)
    assert body["data"]["command_status"] == "completed"
    assert body["data"]["order_id"] == "ORD-10008"
    assert signature_is_valid(
        secret=b"synthetic-showcase-secret",
        timestamp=timestamp,
        event_id=request.headers["x-provider-event-id"],
        raw_body=request.content,
        signature=request.headers["x-provider-signature"],
        now=timestamp,
    )


async def test_showcase_fault_order_retries_once_then_is_idempotently_accepted() -> None:
    command = ProviderCommandEnvelope.model_validate(
        {
            "command_id": "33333333-3333-4333-8333-333333333333",
            "idempotency_key": "order-operation:44444444-4444-4444-8444-444444444444",
            "source_message_id": "showcase-fault-message",
            "aggregate_type": "order_operation",
            "aggregate_id": "44444444-4444-4444-8444-444444444444",
            "expected_order_version": 1,
            "tenant_id": "tenant-demo",
            "customer_id": "customer-a",
            "connection_id": "provider-demo",
            "command_type": "cancel_order",
            "payload": {
                "order_id": "ORD-10012",
                "operation_type": "cancellation",
                "reason": "no_longer_needed",
            },
            "created_at": datetime.now(UTC),
        }
    )
    headers = {
        "Idempotency-Key": command.idempotency_key,
        "X-Provider-Command-ID": str(command.command_id),
    }
    transport = httpx.ASGITransport(app=provider_app.app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://provider.test"
    ) as client:
        first = await client.post(
            "/v1/commands", json=command.model_dump(mode="json"), headers=headers
        )
        second = await client.post(
            "/v1/commands", json=command.model_dump(mode="json"), headers=headers
        )
        replay = await client.post(
            "/v1/commands", json=command.model_dump(mode="json"), headers=headers
        )

    assert first.status_code == 500
    assert second.status_code == replay.status_code == 200
    assert second.json() == replay.json()
    assert second.json()["status"] == "accepted"
    assert second.json()["command_id"] == str(command.command_id)
