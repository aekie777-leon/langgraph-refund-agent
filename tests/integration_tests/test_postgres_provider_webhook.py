"""Real HTTP-to-PostgreSQL acceptance tests for provider webhooks."""

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg.rows import tuple_row
from psycopg_pool import AsyncConnectionPool

from agent.database import create_async_connection_pool
from agent.integrations.postgres_repository import PostgresIntegrationRepository
from agent.integrations.signing import compute_signature, format_timestamp
from agent.integrations.webhook_adapter import CanonicalHmacWebhookAdapter
from agent.integrations.webhook_resolver import (
    EnvironmentProviderWebhookConnectionResolver,
)
from agent.integrations.webhook_router import router
from agent.migrations import apply_migrations

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]

_SECRET = "test-only-webhook-secret-not-for-production"
_CONNECTION_ID = "webhook-test-connection"


@pytest.fixture
def anyio_backend() -> str | tuple[str, dict[str, object]]:
    if os.name == "nt":
        return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}
    return "asyncio"


@pytest.fixture
async def postgres_context() -> AsyncIterator[tuple[AsyncConnectionPool, str]]:
    conninfo = os.getenv("CASE_TEST_POSTGRES_URI")
    if not conninfo:
        pytest.skip("CASE_TEST_POSTGRES_URI is not configured")

    apply_migrations(conninfo)
    pool = create_async_connection_pool(conninfo, min_size=2, max_size=4)
    await pool.open()
    await pool.wait(timeout=10)
    tenant_id = f"webhook-{uuid4()}"
    try:
        yield pool, tenant_id
    finally:
        async with pool.connection() as connection:
            await connection.execute(
                "DELETE FROM integration.inbox_processing_attempts WHERE inbox_id IN (SELECT inbox_id FROM integration.inbox_messages WHERE tenant_id = %s)",
                (tenant_id,),
            )
            await connection.execute(
                "DELETE FROM integration.inbox_messages WHERE tenant_id = %s",
                (tenant_id,),
            )
        await pool.close()


def _app(
    pool: AsyncConnectionPool,
    *,
    tenant_id: str,
    connection_id: str = _CONNECTION_ID,
) -> FastAPI:
    resolver = EnvironmentProviderWebhookConnectionResolver.from_environment(
        environment={
            "PROVIDER_WEBHOOK_CONNECTIONS_JSON": json.dumps(
                [
                    {
                        "connection_id": connection_id,
                        "tenant_id": tenant_id,
                        "signing_secret": _SECRET,
                        "validity_window_seconds": 300,
                    }
                ]
            )
        }
    )
    app = FastAPI()
    app.include_router(router)
    app.state.provider_webhook_resolver = resolver
    app.state.provider_webhook_adapter = CanonicalHmacWebhookAdapter()
    app.state.integration_repository = PostgresIntegrationRepository(pool)
    return app


def _payload(
    *,
    tenant_id: str,
    event_id: str,
    command_id: UUID,
    aggregate_id: UUID,
    timestamp: datetime,
    connection_id: str = _CONNECTION_ID,
    aggregate_type: str = "order_operation",
    command_status: str = "processing",
    order_id: str = "ORD-10001",
    provider_reference: str = "provider-reference-test",
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "event_id": event_id,
            "provider_connection_id": connection_id,
            "tenant_id": tenant_id,
            "timestamp": format_timestamp(timestamp),
            "event_type": "provider_command_status_changed",
            "data": {
                "command_id": str(command_id),
                "aggregate_type": aggregate_type,
                "aggregate_id": str(aggregate_id),
                "command_status": command_status,
                "provider_operation_id": "provider-operation-test",
                "provider_reference": provider_reference,
                "order_id": order_id,
                "occurred_at": format_timestamp(timestamp),
            },
        },
        separators=(",", ":"),
    ).encode()


def _headers(*, event_id: str, timestamp: datetime, raw_body: bytes) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "x-provider-event-id": event_id,
        "x-provider-timestamp": format_timestamp(timestamp),
        "x-provider-signature": compute_signature(
            secret=_SECRET.encode(),
            timestamp=timestamp,
            event_id=event_id,
            raw_body=raw_body,
        ),
    }


async def _post(
    app: FastAPI,
    *,
    connection_id: str,
    raw_body: bytes,
    headers: dict[str, str],
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/webhooks/providers/{connection_id}", content=raw_body, headers=headers
        )


async def _inbox_rows(
    pool: AsyncConnectionPool, *, tenant_id: str
) -> list[tuple[object, ...]]:
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=tuple_row) as cursor:
            await cursor.execute(
                """
                SELECT inbox_id, provider_connection_id, event_id, tenant_id,
                       command_id, aggregate_type, aggregate_id, payload,
                       raw_body_sha256, status, processing_attempts
                FROM integration.inbox_messages
                WHERE tenant_id = %s
                ORDER BY inbox_id
                """,
                (tenant_id,),
            )
            return list(await cursor.fetchall())


async def _attempt_count(pool: AsyncConnectionPool, *, inbox_id: UUID) -> int:
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=tuple_row) as cursor:
            await cursor.execute(
                "SELECT COUNT(*) FROM integration.inbox_processing_attempts WHERE inbox_id = %s",
                (inbox_id,),
            )
            row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


def _assert_safe_response(
    response: httpx.Response, *, raw_body: bytes, signature: str
) -> None:
    rendered = response.text
    for sensitive in (
        _SECRET,
        signature,
        raw_body.decode(),
        "provider-reference-test",
        "ORD-10001",
        "case_management",
        "integration.",
        "postgres",
    ):
        assert sensitive not in rendered


async def test_webhook_http_persists_one_verified_inbox(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    app = _app(pool, tenant_id=tenant_id)
    timestamp = datetime.now(UTC).replace(microsecond=0)
    event_id = f"event-{uuid4()}"
    command_id = uuid4()
    aggregate_id = uuid4()
    raw_body = _payload(
        tenant_id=tenant_id,
        event_id=event_id,
        command_id=command_id,
        aggregate_id=aggregate_id,
        timestamp=timestamp,
    )
    headers = _headers(event_id=event_id, timestamp=timestamp, raw_body=raw_body)

    response = await _post(
        app,
        connection_id=_CONNECTION_ID,
        raw_body=raw_body,
        headers=headers,
    )

    assert (response.status_code, response.json()) == (202, {"status": "accepted"})
    rows = await _inbox_rows(pool, tenant_id=tenant_id)
    assert len(rows) == 1
    (
        inbox_id,
        provider_connection_id,
        persisted_event_id,
        persisted_tenant_id,
        persisted_command_id,
        aggregate_type,
        persisted_aggregate_id,
        persisted_payload,
        raw_body_sha256,
        status,
        processing_attempts,
    ) = rows[0]
    assert provider_connection_id == _CONNECTION_ID
    assert persisted_event_id == event_id
    assert persisted_tenant_id == tenant_id
    assert persisted_command_id == command_id
    assert aggregate_type == "order_operation"
    assert persisted_aggregate_id == aggregate_id
    assert persisted_payload["command_id"] == str(command_id)
    assert raw_body_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert (status, processing_attempts) == ("received", 0)
    assert await _attempt_count(pool, inbox_id=inbox_id) == 0
    assert _SECRET not in json.dumps(persisted_payload)
    assert headers["x-provider-signature"] not in json.dumps(persisted_payload)


async def test_webhook_http_exact_replay_keeps_the_original_inbox_snapshot(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    app = _app(pool, tenant_id=tenant_id)
    timestamp = datetime.now(UTC).replace(microsecond=0)
    event_id = f"event-{uuid4()}"
    raw_body = _payload(
        tenant_id=tenant_id,
        event_id=event_id,
        command_id=uuid4(),
        aggregate_id=uuid4(),
        timestamp=timestamp,
    )
    headers = _headers(event_id=event_id, timestamp=timestamp, raw_body=raw_body)

    first = await _post(
        app, connection_id=_CONNECTION_ID, raw_body=raw_body, headers=headers
    )
    before = await _inbox_rows(pool, tenant_id=tenant_id)
    replay = await _post(
        app, connection_id=_CONNECTION_ID, raw_body=raw_body, headers=headers
    )
    after = await _inbox_rows(pool, tenant_id=tenant_id)

    assert (first.status_code, replay.status_code) == (202, 202)
    assert before == after
    assert len(after) == 1
    assert await _attempt_count(pool, inbox_id=after[0][0]) == 0


async def test_webhook_http_conflicting_replay_preserves_the_original_inbox(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    app = _app(pool, tenant_id=tenant_id)
    timestamp = datetime.now(UTC).replace(microsecond=0)
    event_id = f"event-{uuid4()}"
    command_id = uuid4()
    aggregate_id = uuid4()
    original_body = _payload(
        tenant_id=tenant_id,
        event_id=event_id,
        command_id=command_id,
        aggregate_id=aggregate_id,
        timestamp=timestamp,
    )
    original_headers = _headers(
        event_id=event_id, timestamp=timestamp, raw_body=original_body
    )
    changed_body = _payload(
        tenant_id=tenant_id,
        event_id=event_id,
        command_id=command_id,
        aggregate_id=aggregate_id,
        timestamp=timestamp,
        provider_reference="provider-reference-conflict",
    )
    changed_headers = _headers(
        event_id=event_id, timestamp=timestamp, raw_body=changed_body
    )

    accepted = await _post(
        app,
        connection_id=_CONNECTION_ID,
        raw_body=original_body,
        headers=original_headers,
    )
    before = await _inbox_rows(pool, tenant_id=tenant_id)
    conflict = await _post(
        app,
        connection_id=_CONNECTION_ID,
        raw_body=changed_body,
        headers=changed_headers,
    )
    after = await _inbox_rows(pool, tenant_id=tenant_id)

    assert accepted.status_code == 202
    assert (conflict.status_code, conflict.json()) == (
        409,
        {"detail": "Provider event conflict."},
    )
    _assert_safe_response(
        conflict,
        raw_body=changed_body,
        signature=changed_headers["x-provider-signature"],
    )
    assert before == after
    assert len(after) == 1
    assert await _attempt_count(pool, inbox_id=after[0][0]) == 0


@pytest.mark.parametrize("failure", ["signature", "tenant", "connection"])
async def test_webhook_http_authentication_and_trusted_identity_failures_do_not_persist(
    postgres_context: tuple[AsyncConnectionPool, str], failure: str
) -> None:
    pool, tenant_id = postgres_context
    app = _app(pool, tenant_id=tenant_id)
    timestamp = datetime.now(UTC).replace(microsecond=0)
    event_id = f"event-{uuid4()}"
    raw_body = _payload(
        tenant_id=tenant_id if failure != "tenant" else f"{tenant_id}-untrusted",
        event_id=event_id,
        command_id=uuid4(),
        aggregate_id=uuid4(),
        timestamp=timestamp,
    )
    headers = _headers(event_id=event_id, timestamp=timestamp, raw_body=raw_body)
    connection_id = _CONNECTION_ID
    if failure == "signature":
        headers["x-provider-signature"] = "0" * 64
    if failure == "connection":
        connection_id = "unknown-provider-connection"

    response = await _post(
        app,
        connection_id=connection_id,
        raw_body=raw_body,
        headers=headers,
    )

    expected_status = 400 if failure == "tenant" else 401
    assert response.status_code == expected_status
    _assert_safe_response(
        response, raw_body=raw_body, signature=headers["x-provider-signature"]
    )
    assert await _inbox_rows(pool, tenant_id=tenant_id) == []
