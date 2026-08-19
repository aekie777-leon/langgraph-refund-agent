"""Canonical inbound webhook verification boundary."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError

from agent.integrations.models import ProviderWebhookConnection, ProviderWebhookEnvelope
from agent.integrations.signing import TIMESTAMP_FORMAT, signature_is_valid


class WebhookAuthenticationError(ValueError):
    """Report a deliberately non-descriptive webhook authentication failure."""


class WebhookPayloadError(ValueError):
    """Report a verified but malformed canonical webhook payload."""


class ProviderWebhookAdapter(Protocol):
    """Verify and translate one provider callback into the stable envelope."""

    async def verify_and_decode(
        self,
        *,
        connection: ProviderWebhookConnection,
        provider_connection_id: str,
        headers: Mapping[str, str],
        raw_body: bytes,
        now: datetime,
    ) -> ProviderWebhookEnvelope:
        """Verify trust headers and decode the immutable webhook envelope."""
        ...


class CanonicalHmacWebhookAdapter:
    """Validate fixed headers, canonical HMAC, and the stable envelope."""

    async def verify_and_decode(
        self,
        *,
        connection: ProviderWebhookConnection,
        provider_connection_id: str,
        headers: Mapping[str, str],
        raw_body: bytes,
        now: datetime,
    ) -> ProviderWebhookEnvelope:
        """Verify this canonical HMAC request and decode its JSON envelope."""
        event_id = headers.get("x-provider-event-id")
        timestamp_raw = headers.get("x-provider-timestamp")
        signature = headers.get("x-provider-signature")
        if not event_id or not timestamp_raw or not signature:
            raise WebhookPayloadError("required provider webhook headers are missing")
        try:
            timestamp = datetime.strptime(timestamp_raw, TIMESTAMP_FORMAT).replace(
                tzinfo=UTC
            )
        except ValueError:
            raise WebhookPayloadError("provider webhook timestamp is invalid") from None
        valid = signature_is_valid(
            secret=connection.signing_secret.get_secret_value().encode(),
            timestamp=timestamp,
            event_id=event_id,
            raw_body=raw_body,
            signature=signature,
            now=now,
            max_age_seconds=connection.validity_window_seconds,
        )
        if not valid:
            raise WebhookAuthenticationError("provider webhook authentication failed")
        try:
            envelope = ProviderWebhookEnvelope.model_validate(json.loads(raw_body))
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
            raise WebhookPayloadError("provider webhook payload is invalid") from None
        if (
            envelope.event_id != event_id
            or envelope.provider_connection_id != provider_connection_id
            or envelope.tenant_id != connection.tenant_id
            or envelope.timestamp != timestamp
        ):
            raise WebhookPayloadError("provider webhook identifiers are inconsistent")
        return envelope
