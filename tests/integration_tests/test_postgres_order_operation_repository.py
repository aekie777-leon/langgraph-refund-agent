"""Integration tests for PostgreSQL order-operation persistence."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.database import create_async_connection_pool
from agent.migrations import apply_migrations
from agent.operations.models import (
    OperationDecision,
    OrderOperationRequest,
    OrderSnapshot,
)
from agent.operations.postgres_repository import PostgresOrderOperationRepository
from agent.operations.service import OperationService
from tests.fakes.identity import make_scope
from tests.fakes.operations import InMemoryOrderProvider

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]
NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
SCOPE = make_scope("customer")


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
    pool = create_async_connection_pool(conninfo, min_size=1, max_size=4)
    await pool.open()
    await pool.wait(timeout=10)
    thread_id = f"operation-integration-{uuid4()}"
    try:
        yield pool, thread_id
    finally:
        async with pool.connection() as connection:
            await connection.execute(
                """
                DELETE FROM case_management.order_operation_events AS events
                USING case_management.order_operations AS operations
                WHERE events.operation_id = operations.operation_id
                  AND operations.thread_id = %s
                """,
                (thread_id,),
            )
            await connection.execute(
                """
                DELETE FROM case_management.order_operations
                WHERE thread_id = %s
                """,
                (thread_id,),
            )
        await pool.close()


def _snapshot() -> OrderSnapshot:
    return OrderSnapshot(
        order_id="ORD-10001",
        version=3,
        amount=Decimal("69.99"),
        currency="USD",
        order_status="confirmed",
        payment_status="paid",
        fulfillment_status="delivered",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        shipped_at=datetime(2026, 8, 2, tzinfo=UTC),
        delivered_at=datetime(2026, 8, 10, tzinfo=UTC),
        return_eligible=True,
        exchange_eligible=True,
        customer_id="customer-a",
        tenant_id="tenant-demo",
    )


def _decision() -> OperationDecision:
    return OperationDecision(
        outcome="eligible",
        operation_type="return",
        requires_confirmation=True,
        reason_codes=("return_eligible",),
        display_reason="This order is eligible for return.",
    )


@pytest.mark.anyio
async def test_operation_round_trips_and_confirmation_is_idempotent(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresOrderOperationRepository(pool)
    service = OperationService(repository, clock=lambda: NOW)
    provider = InMemoryOrderProvider(orders=(_snapshot(),))
    request = OrderOperationRequest(
        thread_id=thread_id,
        source_message_id="message-1",
        order_id="ORD-10001",
        operation_type="return",
        reason="damaged_item",
        customer_id="customer-a",
        tenant_id="tenant-demo",
    )

    created = await service.create_pending_operation(
        SCOPE,
        request=request,
        snapshot=_snapshot(),
        decision=_decision(),
        request_excerpt="Please return the damaged item.",
    )
    confirmed = await service.submit_confirmed_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
        provider=provider,
    )
    duplicate = await service.submit_confirmed_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
        provider=provider,
    )

    stored = await repository.get_operation(SCOPE, created.operation.operation_id)
    assert stored == confirmed.operation
    assert stored is not None
    assert stored.status == "submitted"
    assert stored.customer_id == "customer-a"
    assert stored.tenant_id == "tenant-demo"
    assert duplicate.action == "status_unchanged"


@pytest.mark.anyio
async def test_active_order_index_blocks_another_pending_operation(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, thread_id = postgres_context
    repository = PostgresOrderOperationRepository(pool)
    service = OperationService(repository, clock=lambda: NOW)
    first = OrderOperationRequest(
        thread_id=thread_id,
        source_message_id="message-1",
        order_id="ORD-10001",
        operation_type="return",
        reason="damaged_item",
        customer_id="customer-a",
        tenant_id="tenant-demo",
    )
    second = first.model_copy(update={"source_message_id": "message-2"})

    await service.create_pending_operation(
        SCOPE,
        request=first,
        snapshot=_snapshot(),
        decision=_decision(),
        request_excerpt="Please return the damaged item.",
    )
    result = await service.create_pending_operation(
        SCOPE,
        request=second,
        snapshot=_snapshot(),
        decision=_decision(),
        request_excerpt="Please return the damaged item.",
    )

    assert result.action == "duplicate_ignored"
    assert result.operation.source_message_id == "message-1"
