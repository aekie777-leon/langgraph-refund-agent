"""Local-only Provider simulator that emits canonical signed callbacks."""

import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from agent.integrations.models import ProviderCommandEnvelope, ProviderCommandResult
from agent.integrations.signing import compute_signature, format_timestamp
from agent.integrations.simulator import create_provider_simulator
from agent.showcase.config import validate_showcase_environment


def _select_showcase_outcome(
    command: ProviderCommandEnvelope,
    attempt_number: int,
) -> str | None:
    """Fail the first synthetic fault-demo attempt and then recover normally."""
    if command.payload.order_id == "ORD-10012" and attempt_number == 1:
        return "http_500"
    return None


async def _send_completed_callback(
    command: ProviderCommandEnvelope,
    result: ProviderCommandResult,
) -> None:
    """Post one canonical callback to the local API with bounded retries."""
    callback_url = os.environ.get("SHOWCASE_PROVIDER_CALLBACK_URL", "").strip()
    secret = os.environ.get("SHOWCASE_PROVIDER_WEBHOOK_SECRET", "")
    if not callback_url or not secret:
        return
    timestamp = datetime.now(UTC).replace(microsecond=0)
    event_id = f"showcase-{uuid4()}"
    payload = {
        "schema_version": 1,
        "event_id": event_id,
        "provider_connection_id": command.connection_id,
        "tenant_id": command.tenant_id,
        "timestamp": format_timestamp(timestamp),
        "event_type": "provider_command_status_changed",
        "data": {
            "command_id": str(command.command_id),
            "aggregate_type": command.aggregate_type,
            "aggregate_id": str(command.aggregate_id),
            "command_status": "completed",
            "provider_operation_id": result.provider_operation_id,
            "provider_reference": result.provider_reference,
            "order_id": command.payload.order_id,
            "occurred_at": format_timestamp(timestamp),
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "content-type": "application/json",
        "x-provider-event-id": event_id,
        "x-provider-timestamp": format_timestamp(timestamp),
        "x-provider-signature": compute_signature(
            secret=secret.encode(),
            timestamp=timestamp,
            event_id=event_id,
            raw_body=raw_body,
        ),
    }
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        for attempt in range(10):
            try:
                response = await client.post(
                    callback_url,
                    content=raw_body,
                    headers=headers,
                )
                if response.status_code == 202:
                    return
            except httpx.HTTPError:
                pass
            if attempt < 9:
                await asyncio.sleep(0.5)


validate_showcase_environment()
app = create_provider_simulator(
    accepted_callback=_send_completed_callback,
    outcome_selector=_select_showcase_outcome,
)
