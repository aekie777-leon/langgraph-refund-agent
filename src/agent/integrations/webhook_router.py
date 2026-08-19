"""Unauthenticated-by-user but HMAC-authenticated provider webhook route."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent.integrations.provider import (
    ProviderConnectionNotFoundError,
    ProviderWebhookConnectionResolver,
)
from agent.integrations.repository import (
    InboxEventConflictError,
    IntegrationPersistenceError,
    IntegrationRepository,
)
from agent.integrations.webhook_adapter import (
    ProviderWebhookAdapter,
    WebhookAuthenticationError,
    WebhookPayloadError,
)

router = APIRouter(tags=["Provider webhooks"])
_MAX_BODY_BYTES = 256 * 1024


@router.post("/webhooks/providers/{provider_connection_id}", status_code=202)
async def receive_provider_webhook(
    provider_connection_id: str, request: Request
) -> JSONResponse:
    """Authenticate and durably accept one callback without touching domains."""
    protected_headers = (
        "x-provider-event-id",
        "x-provider-timestamp",
        "x-provider-signature",
    )
    if any(len(request.headers.getlist(header)) != 1 for header in protected_headers):
        return JSONResponse(
            {"detail": "Invalid provider webhook request."}, status_code=400
        )
    headers = {header: request.headers[header] for header in protected_headers}
    if any(not value.strip() for value in headers.values()):
        return JSONResponse(
            {"detail": "Invalid provider webhook request."}, status_code=400
        )
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1:
        return JSONResponse(
            {"detail": "Invalid provider webhook request."}, status_code=415
        )
    media_type = content_types[0].split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return JSONResponse(
            {"detail": "Invalid provider webhook request."}, status_code=415
        )
    content_encoding = request.headers.getlist("content-encoding")
    if len(content_encoding) > 1 or (
        content_encoding and content_encoding[0].strip().lower() != "identity"
    ):
        return JSONResponse(
            {"detail": "Invalid provider webhook request."}, status_code=415
        )
    content_lengths = request.headers.getlist("content-length")
    if len(content_lengths) > 1:
        return JSONResponse(
            {"detail": "Invalid provider webhook request."}, status_code=400
        )
    content_length = content_lengths[0] if content_lengths else None
    if content_length is not None:
        if (
            not content_length
            or not content_length.isascii()
            or not content_length.isdecimal()
        ):
            return JSONResponse(
                {"detail": "Invalid provider webhook request."}, status_code=400
            )
        if (
            len(content_length) > len(str(_MAX_BODY_BYTES))
            or int(content_length) > _MAX_BODY_BYTES
        ):
            return JSONResponse({"detail": "Payload too large."}, status_code=413)
    chunks = bytearray()
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            return JSONResponse({"detail": "Payload too large."}, status_code=413)
        chunks.extend(chunk)
    raw_body = bytes(chunks)
    try:
        resolver: ProviderWebhookConnectionResolver = (
            request.app.state.provider_webhook_resolver
        )
        adapter: ProviderWebhookAdapter = request.app.state.provider_webhook_adapter
        repository: IntegrationRepository = request.app.state.integration_repository
        connection = await resolver.resolve_webhook(
            provider_connection_id=provider_connection_id
        )
        envelope = await adapter.verify_and_decode(
            connection=connection,
            provider_connection_id=provider_connection_id,
            headers=headers,
            raw_body=raw_body,
            now=datetime.now(UTC),
        )
        await repository.receive_inbox_idempotently(
            inbox_id=uuid4(),
            provider_connection_id=provider_connection_id,
            event_id=envelope.event_id,
            tenant_id=connection.tenant_id,
            event=envelope.data,
            raw_body_sha256=hashlib.sha256(raw_body).hexdigest(),
            received_at=datetime.now(UTC),
        )
    except WebhookAuthenticationError:
        return JSONResponse(
            {"detail": "Unauthorized provider webhook."}, status_code=401
        )
    except WebhookPayloadError:
        return JSONResponse(
            {"detail": "Invalid provider webhook request."}, status_code=400
        )
    except InboxEventConflictError:
        return JSONResponse({"detail": "Provider event conflict."}, status_code=409)
    except IntegrationPersistenceError:
        return JSONResponse(
            {"detail": "Provider webhook unavailable."}, status_code=503
        )
    except ProviderConnectionNotFoundError:
        return JSONResponse(
            {"detail": "Unauthorized provider webhook."}, status_code=401
        )
    return JSONResponse({"status": "accepted"}, status_code=202)
