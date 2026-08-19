"""Database-free HTTP security contracts for the provider webhook router."""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from agent.integrations.models import (
    ProviderWebhookConnection,
    ProviderWebhookEnvelope,
)
from agent.integrations.provider import ProviderConnectionNotFoundError
from agent.integrations.repository import (
    InboxEventConflictError,
    IntegrationPersistenceError,
)
from agent.integrations.webhook_adapter import (
    WebhookAuthenticationError,
    WebhookPayloadError,
)
from agent.integrations.webhook_router import _MAX_BODY_BYTES, router

pytestmark = pytest.mark.anyio

SECRET = "router-signing-secret-must-not-leak"
EVENT_ID = "router-event-sensitive"
CONNECTION_ID = "router-connection-sensitive"
TENANT_ID = "router-tenant-sensitive"
COMMAND_ID = UUID("11111111-1111-1111-1111-111111111111")
AGGREGATE_ID = UUID("22222222-2222-2222-2222-222222222222")
RAW_BODY = b'{"raw":"provider-reference-sensitive","order":"ORD-10001"}'
PATH = f"/webhooks/providers/{CONNECTION_ID}"


def _connection() -> ProviderWebhookConnection:
    return ProviderWebhookConnection(
        connection_id=CONNECTION_ID,
        tenant_id=TENANT_ID,
        signing_secret=SECRET,
    )


def _envelope() -> ProviderWebhookEnvelope:
    timestamp = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    return ProviderWebhookEnvelope.model_validate(
        {
            "schema_version": 1,
            "event_id": EVENT_ID,
            "provider_connection_id": CONNECTION_ID,
            "tenant_id": TENANT_ID,
            "timestamp": timestamp,
            "event_type": "provider_command_status_changed",
            "data": {
                "command_id": str(COMMAND_ID),
                "aggregate_type": "order_operation",
                "aggregate_id": str(AGGREGATE_ID),
                "command_status": "processing",
                "provider_operation_id": "provider-operation-1",
                "provider_reference": "provider-reference-sensitive",
                "order_id": "ORD-10001",
                "occurred_at": timestamp,
            },
        }
    )


class RecordingResolver:
    def __init__(self, calls: list[str], error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error
        self.connection = _connection()
        self.received_ids: list[str] = []

    async def resolve_webhook(
        self, *, provider_connection_id: str
    ) -> ProviderWebhookConnection:
        self.calls.append("resolver")
        self.received_ids.append(provider_connection_id)
        if self.error is not None:
            raise self.error
        return self.connection


class RecordingAdapter:
    def __init__(self, calls: list[str], error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error
        self.received: list[dict[str, object]] = []
        self.envelope = _envelope()

    async def verify_and_decode(self, **kwargs: object) -> ProviderWebhookEnvelope:
        self.calls.append("adapter")
        self.received.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.envelope


class RecordingRepository:
    def __init__(self, calls: list[str], error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error
        self.received: list[dict[str, object]] = []

    async def receive_inbox_idempotently(self, **kwargs: object) -> object:
        self.calls.append("repository")
        self.received.append(kwargs)
        if self.error is not None:
            raise self.error
        return object()


def _app(
    *,
    resolver_error: Exception | None = None,
    adapter_error: Exception | None = None,
    repository_error: Exception | None = None,
) -> tuple[
    FastAPI, RecordingResolver, RecordingAdapter, RecordingRepository, list[str]
]:
    calls: list[str] = []
    app = FastAPI()
    app.include_router(router)
    resolver = RecordingResolver(calls, resolver_error)
    adapter = RecordingAdapter(calls, adapter_error)
    repository = RecordingRepository(calls, repository_error)
    app.state.provider_webhook_resolver = resolver
    app.state.provider_webhook_adapter = adapter
    app.state.integration_repository = repository
    return app, resolver, adapter, repository, calls


def _protected_headers() -> list[tuple[str, str]]:
    return [
        ("x-provider-event-id", EVENT_ID),
        ("x-provider-timestamp", "2026-08-19T12:00:00Z"),
        ("x-provider-signature", "a" * 64),
    ]


async def _raw_asgi_request(
    app: FastAPI,
    *,
    headers: Sequence[tuple[str, str]],
    chunks: Sequence[bytes] = (RAW_BODY,),
) -> tuple[int, dict[str, object]]:
    index = 0
    response: dict[str, object] = {}
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PATH,
        "raw_path": PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(name.encode(), value.encode()) for name, value in headers],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, object]:
        nonlocal index
        if index < len(chunks):
            chunk = chunks[index]
            index += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks),
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            response["status"] = message["status"]
        if message["type"] == "http.response.body":
            response["body"] = response.get("body", b"") + message.get("body", b"")

    await app(scope, receive, send)
    return int(response["status"]), json.loads(bytes(response.get("body", b"{}")))


def _valid_headers(*extra: tuple[str, str]) -> list[tuple[str, str]]:
    return [*_protected_headers(), ("content-type", "application/json"), *extra]


def _assert_safe_error(payload: dict[str, object]) -> None:
    rendered = json.dumps(payload)
    for sensitive in (
        SECRET,
        EVENT_ID,
        CONNECTION_ID,
        TENANT_ID,
        str(COMMAND_ID),
        str(AGGREGATE_ID),
        "provider-reference-sensitive",
        "ORD-10001",
        "internal-router-error",
    ):
        assert sensitive not in rendered


async def test_success_preserves_raw_bytes_and_trusted_repository_arguments() -> None:
    app, resolver, adapter, repository, calls = _app()
    body = RAW_BODY + b"\n"
    headers = _valid_headers(("content-length", str(len(body))))

    status, payload = await _raw_asgi_request(app, headers=headers, chunks=(body,))

    assert (status, payload) == (202, {"status": "accepted"})
    assert calls == ["resolver", "adapter", "repository"]
    assert resolver.received_ids == [CONNECTION_ID]
    adapter_call = adapter.received[0]
    assert adapter_call["connection"] == resolver.connection
    assert adapter_call["provider_connection_id"] == CONNECTION_ID
    assert adapter_call["headers"] == dict(_protected_headers())
    assert adapter_call["raw_body"] == body
    assert isinstance(adapter_call["now"], datetime)
    assert adapter_call["now"].tzinfo == UTC
    repository_call = repository.received[0]
    assert repository_call["provider_connection_id"] == CONNECTION_ID
    assert repository_call["event_id"] == EVENT_ID
    assert repository_call["tenant_id"] == TENANT_ID
    assert repository_call["event"] == adapter.envelope.data
    assert repository_call["raw_body_sha256"] == hashlib.sha256(body).hexdigest()
    assert isinstance(repository_call["inbox_id"], UUID)
    assert isinstance(repository_call["received_at"], datetime)
    assert repository_call["received_at"].tzinfo == UTC


async def test_exact_replay_still_returns_the_fixed_accepted_response() -> None:
    app, _resolver, _adapter, repository, calls = _app()

    status, payload = await _raw_asgi_request(app, headers=_valid_headers())

    assert (status, payload) == (202, {"status": "accepted"})
    assert calls == ["resolver", "adapter", "repository"]
    assert len(repository.received) == 1


@pytest.mark.parametrize(
    "headers",
    [
        _valid_headers()[1:],
        [header for header in _valid_headers() if header[0] != "x-provider-timestamp"],
        [header for header in _valid_headers() if header[0] != "x-provider-signature"],
        [
            (name, "" if name.startswith("x-provider-") else value)
            for name, value in _valid_headers()
        ],
        [*_valid_headers(), ("x-provider-event-id", "second")],
        [*_valid_headers(), ("x-provider-timestamp", "2026-01-01T00:00:00Z")],
        [*_valid_headers(), ("x-provider-signature", "b" * 64)],
    ],
)
async def test_protected_header_framing_short_circuits_before_dependencies(
    headers: list[tuple[str, str]],
) -> None:
    app, _resolver, _adapter, _repository, calls = _app()

    status, payload = await _raw_asgi_request(app, headers=headers)

    assert (status, payload) == (400, {"detail": "Invalid provider webhook request."})
    assert calls == []
    _assert_safe_error(payload)


async def test_mixed_case_single_protected_headers_are_accepted() -> None:
    app, _resolver, _adapter, _repository, calls = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            PATH,
            content=RAW_BODY,
            headers=[
                ("X-Provider-Event-Id", EVENT_ID),
                ("X-PROVIDER-TIMESTAMP", "2026-08-19T12:00:00Z"),
                ("x-Provider-Signature", "a" * 64),
                ("Content-Type", "application/json"),
            ],
        )

    assert (response.status_code, response.json()) == (202, {"status": "accepted"})
    assert calls == ["resolver", "adapter", "repository"]


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        (_valid_headers(), 202),
        ([*(_valid_headers()[:-1]), ("content-type", "APPLICATION/JSON")], 202),
        (
            [
                *(_valid_headers()[:-1]),
                ("content-type", "application/json; charset=utf-8"),
            ],
            202,
        ),
        ([header for header in _valid_headers() if header[0] != "content-type"], 415),
        ([*(_valid_headers()[:-1]), ("content-type", "text/plain")], 415),
        ([*_valid_headers(), ("content-type", "application/json")], 415),
    ],
)
async def test_content_type_framing_matrix(
    headers: list[tuple[str, str]], expected_status: int
) -> None:
    app, _resolver, _adapter, _repository, calls = _app()

    status, payload = await _raw_asgi_request(app, headers=headers)

    assert status == expected_status
    if expected_status == 202:
        assert payload == {"status": "accepted"}
    else:
        assert payload == {"detail": "Invalid provider webhook request."}
        assert calls == []
        _assert_safe_error(payload)


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        (_valid_headers(), 202),
        (_valid_headers(("content-encoding", "identity")), 202),
        (_valid_headers(("content-encoding", " Identity ")), 202),
        (_valid_headers(("content-encoding", "gzip")), 415),
        (
            _valid_headers(
                ("content-encoding", "identity"), ("content-encoding", "identity")
            ),
            415,
        ),
        (_valid_headers(("content-encoding", "identity, gzip")), 415),
    ],
)
async def test_content_encoding_framing_matrix(
    headers: list[tuple[str, str]], expected_status: int
) -> None:
    app, _resolver, _adapter, _repository, calls = _app()

    status, payload = await _raw_asgi_request(app, headers=headers)

    assert status == expected_status
    if expected_status == 202:
        assert payload == {"status": "accepted"}
    else:
        assert payload == {"detail": "Invalid provider webhook request."}
        assert calls == []
        _assert_safe_error(payload)


@pytest.mark.parametrize(
    ("content_length", "expected_status"),
    [
        (None, 202),
        (str(len(RAW_BODY)), 202),
        ("0", 202),
        ("", 400),
        ("-1", 400),
        ("+1", 400),
        ("1.0", 400),
        (" 1", 400),
        ("not-a-number", 400),
        ("١", 400),
        (str(_MAX_BODY_BYTES + 1), 413),
        ("9" * 5000, 413),
    ],
)
async def test_content_length_validation_short_circuits_invalid_or_oversized_requests(
    content_length: str | None, expected_status: int
) -> None:
    app, _resolver, _adapter, _repository, calls = _app()
    headers = _valid_headers()
    if content_length is not None:
        headers.append(("content-length", content_length))

    status, payload = await _raw_asgi_request(app, headers=headers)

    assert status == expected_status
    if expected_status == 202:
        assert payload == {"status": "accepted"}
    else:
        expected_detail = (
            "Payload too large."
            if expected_status == 413
            else "Invalid provider webhook request."
        )
        assert payload == {"detail": expected_detail}
        assert calls == []
        _assert_safe_error(payload)


async def test_duplicate_content_length_is_rejected_before_dependencies() -> None:
    app, _resolver, _adapter, _repository, calls = _app()

    status, payload = await _raw_asgi_request(
        app,
        headers=_valid_headers(("content-length", "1"), ("content-length", "1")),
    )

    assert (status, payload) == (400, {"detail": "Invalid provider webhook request."})
    assert calls == []


@pytest.mark.parametrize(
    ("chunks", "content_length", "expected_status"),
    [
        ((b"a" * _MAX_BODY_BYTES,), None, 202),
        ((b"a" * (_MAX_BODY_BYTES + 1),), None, 413),
        ((b"a" * _MAX_BODY_BYTES, b"b"), "1", 413),
        ((b"a" * _MAX_BODY_BYTES, b"b"), None, 413),
    ],
)
async def test_streaming_size_limit_uses_actual_body_bytes(
    chunks: tuple[bytes, ...], content_length: str | None, expected_status: int
) -> None:
    app, _resolver, _adapter, _repository, calls = _app()
    headers = _valid_headers()
    if content_length is not None:
        headers.append(("content-length", content_length))

    status, payload = await _raw_asgi_request(app, headers=headers, chunks=chunks)

    assert status == expected_status
    if expected_status == 202:
        assert payload == {"status": "accepted"}
        assert calls == ["resolver", "adapter", "repository"]
    else:
        assert payload == {"detail": "Payload too large."}
        assert calls == []
        _assert_safe_error(payload)


@pytest.mark.parametrize(
    (
        "resolver_error",
        "adapter_error",
        "repository_error",
        "expected_status",
        "expected_detail",
        "expected_calls",
    ),
    [
        (
            ProviderConnectionNotFoundError(),
            None,
            None,
            401,
            "Unauthorized provider webhook.",
            ["resolver"],
        ),
        (
            None,
            WebhookAuthenticationError(),
            None,
            401,
            "Unauthorized provider webhook.",
            ["resolver", "adapter"],
        ),
        (
            None,
            WebhookPayloadError(),
            None,
            400,
            "Invalid provider webhook request.",
            ["resolver", "adapter"],
        ),
        (
            None,
            None,
            InboxEventConflictError(),
            409,
            "Provider event conflict.",
            ["resolver", "adapter", "repository"],
        ),
        (
            None,
            None,
            IntegrationPersistenceError(),
            503,
            "Provider webhook unavailable.",
            ["resolver", "adapter", "repository"],
        ),
    ],
)
async def test_known_exception_mapping_and_short_circuit_order(
    resolver_error: Exception | None,
    adapter_error: Exception | None,
    repository_error: Exception | None,
    expected_status: int,
    expected_detail: str,
    expected_calls: list[str],
) -> None:
    app, _resolver, _adapter, _repository, calls = _app(
        resolver_error=resolver_error,
        adapter_error=adapter_error,
        repository_error=repository_error,
    )

    status, payload = await _raw_asgi_request(app, headers=_valid_headers())

    assert (status, payload) == (expected_status, {"detail": expected_detail})
    assert calls == expected_calls
    _assert_safe_error(payload)


async def test_unknown_exception_remains_a_generic_safe_500() -> None:
    app, _resolver, _adapter, _repository, _calls = _app(
        resolver_error=RuntimeError(
            "internal-router-error router-signing-secret-must-not-leak"
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            PATH, content=RAW_BODY, headers=dict(_valid_headers())
        )

    assert response.status_code == 500
    _assert_safe_error({"detail": response.text})
