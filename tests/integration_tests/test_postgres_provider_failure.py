"""PostgreSQL contract tests for the provider-configuration manual fallback."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agent.cases.models import SupportCaseEvent
from agent.cases.postgres_repository import PostgresCaseRepository
from agent.integrations.provider_failure import PostgresProviderQueueFailureCoordinator
from agent.operations.postgres_repository import PostgresOrderOperationRepository
from agent.operations.repository import (
    OperationNotFoundError,
    OperationPersistenceError,
)
from tests.fakes.identity import make_scope
from tests.integration_tests.test_postgres_provider_messaging import (  # noqa: F401, F811
    _operation,
    _scope,
)
from tests.integration_tests.test_postgres_provider_messaging import (
    postgres_context as _postgres_context,
)

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]


@pytest.fixture
def anyio_backend() -> str | tuple[str, dict[str, object]]:
    if os.name == "nt":
        return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}
    return "asyncio"


@pytest.fixture
async def provider_failure_postgres_context():
    """Reuse the established disposable PostgreSQL fixture and its cleanup."""
    async for context in _postgres_context.__wrapped__():
        yield context


async def _pending(
    pool,
    prefix,
    *,
    order_id="ORD-10001",
    thread_id="thread-1",
    source="message-1",
):
    scope = _scope(prefix)
    operation = _operation(tenant_id=prefix, order_id=order_id, thread_id=thread_id, source_message_id=source)
    await PostgresOrderOperationRepository(pool).create_operation_with_events(scope, operation=operation, events=())
    return scope, operation


async def _count(pool, query, *parameters) -> int:
    async with pool.connection() as connection:
        cursor = await connection.execute(query, parameters)
        row = await cursor.fetchone()
    return int(row[0])


async def test_provider_failure_creates_case_and_events_without_outbox(provider_failure_postgres_context) -> None:
    pool, prefix = provider_failure_postgres_context
    scope, operation = await _pending(pool, prefix)
    result = await PostgresProviderQueueFailureCoordinator(pool).move_to_manual_review(scope, operation_id=operation.operation_id, request_id="provider-failure-1")
    stored = await PostgresOrderOperationRepository(pool).get_operation(scope, operation.operation_id)
    assert stored is not None and stored.status == "manual_review"
    assert stored.support_case_id == result.support_case.case_id
    assert result.support_case.priority == "p1"
    assert "provider_delivery_failed" in result.support_case.reason_codes
    assert await _count(pool, "SELECT COUNT(*) FROM integration.outbox_messages WHERE tenant_id = %s", prefix) == 0
    assert await _count(pool, "SELECT COUNT(*) FROM case_management.order_operation_events WHERE operation_id = %s", operation.operation_id) == 3
    assert await _count(pool, "SELECT COUNT(*) FROM case_management.support_case_events WHERE case_id = %s", result.support_case.case_id) == 1


async def test_provider_failure_replay_is_idempotent(provider_failure_postgres_context) -> None:
    pool, prefix = provider_failure_postgres_context
    scope, operation = await _pending(pool, prefix)
    coordinator = PostgresProviderQueueFailureCoordinator(pool)
    first = await coordinator.move_to_manual_review(scope, operation_id=operation.operation_id, request_id="replay-1")
    counts = (
        await _count(pool, "SELECT COUNT(*) FROM case_management.order_operation_events WHERE operation_id = %s", operation.operation_id),
        await _count(pool, "SELECT COUNT(*) FROM case_management.support_case_events WHERE case_id = %s", first.support_case.case_id),
    )
    replay = await coordinator.move_to_manual_review(scope, operation_id=operation.operation_id, request_id="replay-1")
    assert replay.action == "duplicate_ignored"
    assert replay.support_case.case_id == first.support_case.case_id
    assert counts == (
        await _count(pool, "SELECT COUNT(*) FROM case_management.order_operation_events WHERE operation_id = %s", operation.operation_id),
        await _count(pool, "SELECT COUNT(*) FROM case_management.support_case_events WHERE case_id = %s", first.support_case.case_id),
    )


async def test_provider_failure_reuses_active_case_and_preserves_reasons(provider_failure_postgres_context) -> None:
    pool, prefix = provider_failure_postgres_context
    scope, first = await _pending(pool, prefix)
    coordinator = PostgresProviderQueueFailureCoordinator(pool)
    created = await coordinator.move_to_manual_review(scope, operation_id=first.operation_id, request_id="first")
    _, second = await _pending(pool, prefix, order_id="ORD-10002", source="message-2")
    reused = await coordinator.move_to_manual_review(scope, operation_id=second.operation_id, request_id="second")
    assert reused.action == "reused"
    assert reused.support_case.case_id == created.support_case.case_id
    assert "provider_delivery_failed" in reused.support_case.reason_codes
    assert reused.support_case.priority == "p1"
    assert await _count(pool, "SELECT COUNT(*) FROM case_management.support_cases WHERE tenant_id = %s AND thread_id = %s AND case_type = 'order_operation_review'", prefix, first.thread_id) == 1
    stored = await PostgresOrderOperationRepository(pool).get_operation(scope, second.operation_id)
    assert stored is not None and stored.support_case_id == created.support_case.case_id


async def test_provider_failure_rolls_back_on_case_event_conflict(provider_failure_postgres_context) -> None:
    pool, prefix = provider_failure_postgres_context
    scope, first = await _pending(pool, prefix, thread_id="conflict-thread", source="first")
    coordinator = PostgresProviderQueueFailureCoordinator(pool)
    created = await coordinator.move_to_manual_review(scope, operation_id=first.operation_id, request_id="first")
    _, operation = await _pending(pool, prefix, order_id="ORD-10002", thread_id="conflict-thread", source="second")
    duplicate_key = f"provider-queue-failure:{operation.operation_id}:case:conflict"
    case = created.support_case
    preexisting_event = SupportCaseEvent(
        event_id=uuid4(),
        idempotency_key=duplicate_key,
        case_id=case.case_id,
        event_type="trigger_appended",
        source_message_id=operation.source_message_id,
        order_id=operation.order_id,
        reason_codes=("provider_delivery_failed",),
        triggering_message_excerpt=operation.request_excerpt,
        previous_priority=case.priority,
        current_priority=case.priority,
        current_status=case.status,
        actor="system",
        customer_id=case.customer_id,
        tenant_id=case.tenant_id,
        created_at=datetime.now(UTC),
    )
    case_for_event = case.model_copy(update={"updated_at": datetime.now(UTC), "version": case.version + 1})
    await PostgresCaseRepository(pool).update_case_with_event(scope, case=case_for_event, event=preexisting_event, expected_version=case.version)

    with pytest.raises(OperationPersistenceError):
        await coordinator.move_to_manual_review(scope, operation_id=operation.operation_id, request_id="conflict")

    stored = await PostgresOrderOperationRepository(pool).get_operation(scope, operation.operation_id)
    assert stored is not None and stored.status == "pending_confirmation"
    assert await _count(pool, "SELECT COUNT(*) FROM case_management.order_operation_events WHERE operation_id = %s", operation.operation_id) == 0
    assert await _count(pool, "SELECT COUNT(*) FROM case_management.support_cases WHERE tenant_id = %s", prefix) == 1
    assert await _count(pool, "SELECT COUNT(*) FROM integration.outbox_messages WHERE tenant_id = %s", prefix) == 0


@pytest.mark.parametrize("tenant,customer", [("other-tenant", "customer-a"), ("", "other-customer")])
async def test_provider_failure_enforces_tenant_and_customer_scope(provider_failure_postgres_context, tenant, customer) -> None:
    pool, prefix = provider_failure_postgres_context
    scope, operation = await _pending(pool, prefix)
    bad_scope = make_scope("customer", user_id=customer, tenant_id=tenant or prefix)
    with pytest.raises(OperationNotFoundError):
        await PostgresProviderQueueFailureCoordinator(pool).move_to_manual_review(bad_scope, operation_id=operation.operation_id, request_id="scope")
    stored = await PostgresOrderOperationRepository(pool).get_operation(scope, operation.operation_id)
    assert stored is not None and stored.status == "pending_confirmation"


async def test_provider_failure_concurrent_operations_share_one_active_case(provider_failure_postgres_context) -> None:
    pool, prefix = provider_failure_postgres_context
    scope, first = await _pending(pool, prefix, order_id="ORD-10001", thread_id="shared", source="one")
    _, second = await _pending(pool, prefix, order_id="ORD-10002", thread_id="shared", source="two")
    coordinator = PostgresProviderQueueFailureCoordinator(pool)
    outcomes = await asyncio.gather(
        coordinator.move_to_manual_review(scope, operation_id=first.operation_id, request_id="one"),
        coordinator.move_to_manual_review(scope, operation_id=second.operation_id, request_id="two"),
    )
    assert outcomes[0].support_case.case_id == outcomes[1].support_case.case_id
    assert await _count(pool, "SELECT COUNT(*) FROM case_management.support_cases WHERE tenant_id = %s AND thread_id = %s AND case_type = 'order_operation_review'", prefix, "shared") == 1
    for operation in (first, second):
        stored = await PostgresOrderOperationRepository(pool).get_operation(scope, operation.operation_id)
        assert stored is not None
        assert stored.status == "manual_review"
        assert stored.support_case_id == outcomes[0].support_case.case_id
    assert await _count(pool, "SELECT COUNT(*) FROM integration.outbox_messages WHERE tenant_id = %s", prefix) == 0
