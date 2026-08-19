"""Database-free contract tests for canonical provider webhook verification."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from agent.integrations.models import ProviderWebhookConnection
from agent.integrations.signing import compute_signature, format_timestamp
from agent.integrations.webhook_adapter import (
    CanonicalHmacWebhookAdapter,
    WebhookAuthenticationError,
    WebhookPayloadError,
)

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
SECRET = "adapter-secret-must-not-leak"
EVENT_ID = "event-sensitive-id"
CONNECTION_ID = "connection-sensitive-id"
TENANT_ID = "tenant-sensitive-id"
COMMAND_ID = UUID("11111111-1111-1111-1111-111111111111")
AGGREGATE_ID = UUID("22222222-2222-2222-2222-222222222222")
PROVIDER_REFERENCE = "provider-reference-sensitive"


def _connection(**overrides: object) -> ProviderWebhookConnection:
    values: dict[str, object] = {
        "connection_id": CONNECTION_ID,
        "tenant_id": TENANT_ID,
        "signing_secret": SECRET,
        "validity_window_seconds": 300,
    }
    values.update(overrides)
    return ProviderWebhookConnection.model_validate(values)


def _payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": 1,
        "event_id": EVENT_ID,
        "provider_connection_id": CONNECTION_ID,
        "tenant_id": TENANT_ID,
        "timestamp": format_timestamp(NOW),
        "event_type": "provider_command_status_changed",
        "data": {
            "command_id": str(COMMAND_ID),
            "aggregate_type": "order_operation",
            "aggregate_id": str(AGGREGATE_ID),
            "command_status": "processing",
            "provider_operation_id": "provider-operation-1",
            "provider_reference": PROVIDER_REFERENCE,
            "order_id": "ORD-10001",
            "occurred_at": format_timestamp(NOW),
        },
    }
    values.update(overrides)
    return values


def _raw(**overrides: object) -> bytes:
    return json.dumps(_payload(**overrides), separators=(",", ":")).encode()


def _raw_without(field: str) -> bytes:
    payload = _payload()
    del payload[field]
    return json.dumps(payload, separators=(",", ":")).encode()


def _headers(
    raw_body: bytes,
    *,
    event_id: str = EVENT_ID,
    timestamp: datetime = NOW,
    signature: str | None = None,
) -> dict[str, str]:
    return {
        "x-provider-event-id": event_id,
        "x-provider-timestamp": format_timestamp(timestamp),
        "x-provider-signature": signature
        or compute_signature(
            secret=SECRET.encode(),
            timestamp=timestamp,
            event_id=event_id,
            raw_body=raw_body,
        ),
    }


def _assert_safe(error: ValueError) -> None:
    rendered = f"{error!s} {error!r}"
    for sensitive in (
        SECRET,
        EVENT_ID,
        CONNECTION_ID,
        TENANT_ID,
        PROVIDER_REFERENCE,
        str(COMMAND_ID),
    ):
        assert sensitive not in rendered


async def _verify(
    raw_body: bytes,
    *,
    headers: dict[str, str] | None = None,
    provider_connection_id: str = CONNECTION_ID,
    connection: ProviderWebhookConnection | None = None,
    now: datetime = NOW,
):
    return await CanonicalHmacWebhookAdapter().verify_and_decode(
        connection=connection or _connection(),
        provider_connection_id=provider_connection_id,
        headers=_headers(raw_body) if headers is None else headers,
        raw_body=raw_body,
        now=now,
    )


async def test_valid_canonical_raw_body_returns_a_typed_envelope() -> None:
    raw_body = _raw()

    envelope = await _verify(raw_body)

    assert envelope.event_id == EVENT_ID
    assert envelope.tenant_id == TENANT_ID
    assert envelope.data.command_id == COMMAND_ID
    assert envelope.data.provider_reference == PROVIDER_REFERENCE


async def test_semantically_equivalent_body_with_different_bytes_fails_authentication_first() -> (
    None
):
    signed_body = _raw()
    different_bytes = json.dumps(_payload(), indent=2).encode()

    with pytest.raises(WebhookAuthenticationError) as caught:
        await _verify(different_bytes, headers=_headers(signed_body))

    _assert_safe(caught.value)


@pytest.mark.parametrize(
    "raw_body",
    [b"{", b"\xff\xfe", b'{"event_id":"leak-but-not-valid"}'],
)
async def test_invalid_signature_is_checked_before_json_parsing(
    raw_body: bytes,
) -> None:
    with pytest.raises(WebhookAuthenticationError) as caught:
        await _verify(raw_body, headers=_headers(raw_body, signature="0" * 64))

    _assert_safe(caught.value)


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({}, _raw()),
        (
            {
                "x-provider-event-id": "",
                "x-provider-timestamp": "",
                "x-provider-signature": "",
            },
            _raw(),
        ),
        (
            {
                "x-provider-timestamp": format_timestamp(NOW),
                "x-provider-signature": "0" * 64,
            },
            _raw(),
        ),
        (
            {
                "x-provider-event-id": EVENT_ID,
                "x-provider-signature": "0" * 64,
            },
            _raw(),
        ),
        (
            {
                "x-provider-event-id": EVENT_ID,
                "x-provider-timestamp": format_timestamp(NOW),
            },
            _raw(),
        ),
        (
            {
                "x-provider-event-id": EVENT_ID,
                "x-provider-timestamp": "not-a-time",
                "x-provider-signature": "0" * 64,
            },
            _raw(),
        ),
    ],
)
async def test_missing_or_malformed_required_headers_are_payload_errors(
    headers: dict[str, str], body: bytes
) -> None:
    with pytest.raises(WebhookPayloadError) as caught:
        await _verify(body, headers=headers)

    _assert_safe(caught.value)


@pytest.mark.parametrize(
    "headers",
    [
        lambda body: {**_headers(body), "x-provider-event-id": "different-event"},
        lambda body: {
            **_headers(body),
            "x-provider-timestamp": format_timestamp(NOW + timedelta(seconds=1)),
        },
        lambda body: _headers(body, signature=("A" * 64)),
        lambda body: _headers(body, signature="0" * 63),
        lambda body: _headers(body, signature="g" * 64),
        lambda body: _headers(body, timestamp=NOW - timedelta(seconds=301)),
        lambda body: _headers(body, timestamp=NOW + timedelta(seconds=301)),
    ],
)
async def test_signature_and_timestamp_failures_are_safe_authentication_errors(
    headers,
) -> None:
    raw_body = _raw()
    with pytest.raises(WebhookAuthenticationError) as caught:
        await _verify(raw_body, headers=headers(raw_body))

    _assert_safe(caught.value)


@pytest.mark.parametrize(
    "raw_body",
    [
        b"{",
        b"\xff\xfe",
        _raw_without("event_id"),
        _raw(unexpected="schema-extra"),
        _raw(event_type="unrecognized"),
        _raw(data={"command_id": str(COMMAND_ID)}),
    ],
)
async def test_verified_malformed_payloads_are_safe_payload_errors(
    raw_body: bytes,
) -> None:
    with pytest.raises(WebhookPayloadError) as caught:
        await _verify(raw_body)

    _assert_safe(caught.value)


@pytest.mark.parametrize(
    ("payload_overrides", "provider_connection_id", "connection"),
    [
        ({"event_id": "different-event"}, CONNECTION_ID, None),
        ({"provider_connection_id": "other-connection"}, CONNECTION_ID, None),
        ({"tenant_id": "body-tenant-must-not-select"}, CONNECTION_ID, None),
        (
            {"timestamp": format_timestamp(NOW + timedelta(seconds=1))},
            CONNECTION_ID,
            None,
        ),
        (
            {"tenant_id": "tenant-two"},
            CONNECTION_ID,
            _connection(tenant_id="tenant-one"),
        ),
    ],
)
async def test_verified_identity_mismatches_are_payload_errors(
    payload_overrides: dict[str, object],
    provider_connection_id: str,
    connection: ProviderWebhookConnection | None,
) -> None:
    raw_body = _raw(**payload_overrides)
    with pytest.raises(WebhookPayloadError) as caught:
        await _verify(
            raw_body,
            provider_connection_id=provider_connection_id,
            connection=connection,
        )

    _assert_safe(caught.value)
