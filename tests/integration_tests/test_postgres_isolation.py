"""Integration tests for cross-user and cross-tenant data isolation."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.cases.models import CaseTrigger, HandoffPolicyInput
from agent.cases.policy import determine_handoff_policy
from agent.cases.postgres_repository import PostgresCaseRepository
from agent.cases.repository import CaseNotFoundError
from agent.cases.service import CaseService
from agent.database import create_async_connection_pool
from agent.migrations import apply_migrations
from agent.operations.models import (
    OperationDecision,
    OrderOperationRequest,
    OrderSnapshot,
)
from agent.operations.postgres_repository import PostgresOrderOperationRepository
from agent.operations.repository import OperationNotFoundError
from agent.operations.service import OperationService
from agent.refunds.postgres_repository import PostgresRefundRepository
from agent.refunds.service import RefundService
from tests.fakes.identity import make_scope

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]
NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str | tuple[str, dict[str, object]]:
    if os.name == "nt":
        return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}
    return "asyncio"


@pytest.fixture
async def postgres_context() -> AsyncIterator[AsyncConnectionPool]:
    conninfo = os.getenv("CASE_TEST_POSTGRES_URI")
    if not conninfo:
        pytest.skip("CASE_TEST_POSTGRES_URI is not configured")

    apply_migrations(conninfo)
    pool = create_async_connection_pool(conninfo, min_size=1, max_size=4)
    await pool.open()
    await pool.wait(timeout=10)
    try:
        yield pool
    finally:
        async with pool.connection() as connection:
            await connection.execute(
                "DELETE FROM case_management.order_operation_events WHERE tenant_id LIKE %s",
                ("isolation-%",),
            )
            await connection.execute(
                "DELETE FROM case_management.order_operations WHERE tenant_id LIKE %s",
                ("isolation-%",),
            )
            await connection.execute(
                "DELETE FROM case_management.support_case_events WHERE tenant_id LIKE %s",
                ("isolation-%",),
            )
            await connection.execute(
                "DELETE FROM case_management.support_cases WHERE tenant_id LIKE %s",
                ("isolation-%",),
            )
            await connection.execute(
                "DELETE FROM refund_requests WHERE tenant_id LIKE %s",
                ("isolation-%",),
            )
        await pool.close()


def _decision() -> OperationDecision:
    return OperationDecision(
        outcome="eligible",
        operation_type="return",
        requires_confirmation=True,
        reason_codes=("return_eligible",),
        display_reason="This order is eligible for return.",
    )


@pytest.mark.anyio
async def test_support_cases_are_isolated_between_customers(
    postgres_context: AsyncConnectionPool,
) -> None:
    pool = postgres_context
    tenant = f"isolation-{uuid4()}"
    owner = make_scope("customer", user_id="customer-a", tenant_id=tenant)
    other = make_scope("customer", user_id="customer-b", tenant_id=tenant)
    service = CaseService(PostgresCaseRepository(pool), clock=lambda: NOW)

    created = await service.record_handoff(
        owner,
        trigger=CaseTrigger(
            thread_id=f"{tenant}-thread-1",
            source_message_id="message-1",
            risk_level="medium",
            risk_categories=("self_harm",),
            triggering_message_excerpt="I need help.",
        ),
        decision=determine_handoff_policy(
            HandoffPolicyInput(
                semantic_risk_level="medium",
                semantic_risk_categories=("self_harm",),
            )
        ),
    )
    assert created.case is not None

    assert await service.get_case(owner, created.case.case_id) is not None
    with pytest.raises(CaseNotFoundError):
        await service.get_case(other, created.case.case_id)


@pytest.mark.anyio
async def test_support_cases_are_isolated_between_tenants(
    postgres_context: AsyncConnectionPool,
) -> None:
    pool = postgres_context
    prefix = f"isolation-{uuid4()}"
    tenant_a = f"{prefix}-a"
    tenant_b = f"{prefix}-b"
    owner_a = make_scope("customer", user_id="customer-a", tenant_id=tenant_a)
    other = make_scope("customer", user_id="customer-a", tenant_id=tenant_b)
    service = CaseService(PostgresCaseRepository(pool), clock=lambda: NOW)

    created = await service.record_handoff(
        owner_a,
        trigger=CaseTrigger(
            thread_id=f"{tenant_a}-thread-1",
            source_message_id="message-1",
            risk_level="medium",
            risk_categories=("self_harm",),
            triggering_message_excerpt="I need help.",
        ),
        decision=determine_handoff_policy(
            HandoffPolicyInput(
                semantic_risk_level="medium",
                semantic_risk_categories=("self_harm",),
            )
        ),
    )
    assert created.case is not None

    with pytest.raises(CaseNotFoundError):
        await service.get_case(other, created.case.case_id)


@pytest.mark.anyio
async def test_order_operations_are_isolated_between_customers(
    postgres_context: AsyncConnectionPool,
) -> None:
    pool = postgres_context
    tenant = f"isolation-{uuid4()}"
    owner = make_scope("customer", user_id="customer-a", tenant_id=tenant)
    other = make_scope("customer", user_id="customer-b", tenant_id=tenant)
    service = OperationService(
        PostgresOrderOperationRepository(pool),
        clock=lambda: NOW,
    )
    snapshot = OrderSnapshot(
        order_id="ORD-10001",
        version=1,
        amount=Decimal("69.99"),
        currency="USD",
        order_status="confirmed",
        payment_status="paid",
        fulfillment_status="delivered",
        created_at=NOW,
        shipped_at=NOW,
        delivered_at=NOW,
        return_eligible=True,
        exchange_eligible=True,
        customer_id=owner.customer_id,
        tenant_id=owner.tenant_id,
    )

    created = await service.create_pending_operation(
        owner,
        request=OrderOperationRequest(
            thread_id=f"{tenant}-thread-1",
            source_message_id="message-1",
            order_id="ORD-10001",
            operation_type="return",
            reason="damaged_item",
            customer_id=owner.customer_id,
            tenant_id=owner.tenant_id,
        ),
        snapshot=snapshot,
        decision=_decision(),
        request_excerpt="Return this item.",
    )

    assert await service.get_operation(owner, created.operation.operation_id) is not None
    with pytest.raises(OperationNotFoundError):
        await service.get_operation(other, created.operation.operation_id)


@pytest.mark.anyio
async def test_refunds_are_isolated_between_customers(
    postgres_context: AsyncConnectionPool,
) -> None:
    pool = postgres_context
    tenant = f"isolation-{uuid4()}"
    owner = make_scope("customer", user_id="customer-a", tenant_id=tenant)
    other = make_scope("customer", user_id="customer-b", tenant_id=tenant)
    service = RefundService(PostgresRefundRepository(pool), clock=lambda: NOW)

    await service.create_refund(owner, order_id="ORD-10001")

    assert await service.get_by_order_id(owner, order_id="ORD-10001") is not None
    assert await service.get_by_order_id(other, order_id="ORD-10001") is None
