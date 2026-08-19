"""Integration tests for provider-messaging persistence (outbox / inbox)."""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.cases.models import SupportCase, SupportCaseEvent
from agent.cases.postgres_repository import PostgresCaseRepository
from agent.cases.repository import (
    ActiveCaseConflictError,
    CasePersistenceError,
    ConcurrentCaseUpdateError,
)
from agent.database import create_async_connection_pool
from agent.integrations.models import (
    ProviderCommandEnvelope,
    ProviderWebhookEventData,
)
from agent.integrations.postgres_repository import PostgresIntegrationRepository
from agent.integrations.postgres_writes import (
    finish_delivery_attempt,
    finish_inbox_attempt,
    update_outbox_transition,
)
from agent.integrations.repository import (
    DuplicateRedriveRequestError,
    InboxEventConflictError,
    InvalidRedriveStateError,
    LeaseConflictError,
    OutboxAttemptsExhaustedError,
)
from agent.migrations import apply_migrations
from agent.operations.models import (
    OrderOperation,
    OrderOperationEvent,
)
from agent.operations.postgres_repository import PostgresOrderOperationRepository
from agent.operations.repository import (
    ActiveOrderOperationConflictError,
    ConcurrentOperationUpdateError,
    OperationPersistenceError,
)
from tests.fakes.identity import make_scope

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]
NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)


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
    prefix = f"msg-{uuid4()}"
    try:
        yield pool, prefix
    finally:
        async with pool.connection() as connection:
            await connection.execute(
                """
                DELETE FROM integration.inbox_processing_attempts AS attempts
                USING integration.inbox_messages AS messages
                WHERE attempts.inbox_id = messages.inbox_id
                  AND messages.tenant_id LIKE %s
                """,
                (f"{prefix}%",),
            )
            await connection.execute(
                """
                DELETE FROM integration.outbox_delivery_attempts AS attempts
                USING integration.outbox_messages AS messages
                WHERE attempts.command_id = messages.command_id
                  AND messages.tenant_id LIKE %s
                """,
                (f"{prefix}%",),
            )
            await connection.execute(
                "DELETE FROM integration.outbox_redrives WHERE tenant_id LIKE %s",
                (f"{prefix}%",),
            )
            await connection.execute(
                "DELETE FROM integration.inbox_messages WHERE tenant_id LIKE %s",
                (f"{prefix}%",),
            )
            await connection.execute(
                "DELETE FROM integration.outbox_messages WHERE tenant_id LIKE %s",
                (f"{prefix}%",),
            )
            await connection.execute(
                """
                DELETE FROM case_management.order_operation_events AS events
                USING case_management.order_operations AS operations
                WHERE events.operation_id = operations.operation_id
                  AND operations.tenant_id LIKE %s
                """,
                (f"{prefix}%",),
            )
            await connection.execute(
                "DELETE FROM case_management.order_operations WHERE tenant_id LIKE %s",
                (f"{prefix}%",),
            )
            await connection.execute(
                "DELETE FROM case_management.support_case_events WHERE tenant_id LIKE %s",
                (f"{prefix}%",),
            )
            await connection.execute(
                "DELETE FROM case_management.support_cases WHERE tenant_id LIKE %s",
                (f"{prefix}%",),
            )
        await pool.close()


def _scope(tenant_id: str):
    return make_scope("customer", user_id="customer-a", tenant_id=tenant_id)


def _operation(
    *,
    tenant_id: str,
    status: str = "pending_confirmation",
    order_id: str = "ORD-10001",
    source_message_id: str = "message-1",
    thread_id: str | None = None,
) -> OrderOperation:
    """Build one readable, deterministic operation.

    The idempotency key is derived from the source message id, so callers that
    pass distinct ``source_message_id`` values never collide on the domain
    idempotency or source-message unique constraints.
    """
    return OrderOperation(
        operation_id=uuid4(),
        idempotency_key=f"operation:{tenant_id}:{source_message_id}:created",
        thread_id=thread_id or f"{tenant_id}-thread-1",
        source_message_id=source_message_id,
        order_id=order_id,
        operation_type="return",
        request_reason_code="damaged_item",
        policy_reason_codes=("return_eligible",),
        display_reason="This order is eligible for return.",
        order_version=3,
        amount=Decimal("69.99"),
        currency="USD",
        requires_manual_review=False,
        request_excerpt="Return this item.",
        status=status,
        created_at=NOW,
        updated_at=NOW,
        customer_id="customer-a",
        tenant_id=tenant_id,
        created_by=f"{tenant_id}:customer-a",
    )


def _queued_event(operation: OrderOperation) -> OrderOperationEvent:
    return OrderOperationEvent(
        event_id=uuid4(),
        idempotency_key=f"operation:{operation.operation_id}:status:queue-1",
        operation_id=operation.operation_id,
        event_type="status_changed",
        previous_status="pending_confirmation",
        current_status="queued",
        actor="system",
        customer_id="customer-a",
        tenant_id=operation.tenant_id,
        created_at=NOW,
    )


def _envelope(operation: OrderOperation) -> ProviderCommandEnvelope:
    return ProviderCommandEnvelope.for_order_operation(
        operation=operation,
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )


def _delivery_case(
    *,
    tenant_id: str,
    order_id: str,
    source_message_id: str = "message-1",
) -> SupportCase:
    return SupportCase(
        case_id=uuid4(),
        thread_id=f"{tenant_id}-thread-1",
        source_message_id=source_message_id,
        order_id=order_id,
        case_type="delivery_investigation",
        priority="p1",
        status="open",
        risk_level=None,
        risk_categories=(),
        reason_codes=("delivery_tracking_stalled",),
        display_reason="Tracking has not updated for 72 hours.",
        triggering_message_excerpt="Tracking has not updated.",
        created_at=NOW,
        updated_at=NOW,
        version=1,
        customer_id="customer-a",
        tenant_id=tenant_id,
        created_by=f"{tenant_id}:customer-a",
    )


def _case_created_event(case: SupportCase) -> SupportCaseEvent:
    return SupportCaseEvent(
        event_id=uuid4(),
        idempotency_key=f"message:{case.thread_id}:{case.source_message_id}",
        case_id=case.case_id,
        event_type="case_created",
        source_message_id=case.source_message_id,
        order_id=case.order_id,
        reason_codes=("delivery_tracking_stalled",),
        triggering_message_excerpt="Tracking has not updated.",
        current_priority="p1",
        current_status="open",
        actor="system",
        customer_id="customer-a",
        tenant_id=case.tenant_id,
        created_at=NOW,
    )


def _delivery_envelope(case: SupportCase) -> ProviderCommandEnvelope:
    return ProviderCommandEnvelope.for_delivery_investigation(
        case=case,
        issue_type="tracking_stalled",
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )


def _webhook_event(*, command_id: UUID) -> ProviderWebhookEventData:
    return ProviderWebhookEventData(
        command_id=command_id,
        aggregate_type="order_operation",
        aggregate_id=command_id,
        command_status="processing",
        provider_operation_id="provider-op-1",
        provider_reference="ref-1",
        order_id="ORD-10001",
        occurred_at=NOW,
    )


async def test_migration_0007_creates_integration_schema(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, _prefix = postgres_context

    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'integration'
            ORDER BY table_name
            """
        )
        rows = await cursor.fetchall()

    names = [row[0] for row in rows]
    assert names == [
        "inbox_messages",
        "inbox_processing_attempts",
        "outbox_delivery_attempts",
        "outbox_messages",
        "outbox_redrives",
    ]


async def test_operation_event_and_outbox_commit_atomically(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)

    created = _operation(tenant_id=tenant_id)
    await operation_repo.create_operation_with_events(
        scope,
        operation=created,
        events=(),
    )
    queued = created.model_copy(
        update={"status": "queued", "version": created.version + 1}
    )
    command = _envelope(queued)

    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=created.version,
    )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    assert stored is not None
    assert stored.status == "queued"
    outbox = await integration_repo.get_outbox_message(command.command_id)
    assert outbox is not None
    assert outbox.status == "pending"
    assert outbox.tenant_id == tenant_id
    assert outbox.to_envelope() == command


async def test_outbox_unique_failure_rolls_back_operation(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    """A duplicate outbox command_id must roll back the whole queue write."""
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)

    first = _operation(
        tenant_id=tenant_id,
        order_id="ORD-10001",
        source_message_id="message-outbox-1",
    )
    await operation_repo.create_operation_with_events(scope, operation=first, events=())
    queued_first = first.model_copy(
        update={"status": "queued", "version": first.version + 1}
    )
    first_command = _envelope(queued_first)
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued_first,
        events=(_queued_event(queued_first),),
        command=first_command,
        expected_version=first.version,
    )

    # A second operation that shares no uniqueness with the first.
    second = _operation(
        tenant_id=tenant_id,
        order_id="ORD-10002",
        source_message_id="message-outbox-2",
    )
    await operation_repo.create_operation_with_events(scope, operation=second, events=())
    queued_second = second.model_copy(
        update={"status": "queued", "version": second.version + 1}
    )
    # Reuse the first command's command_id (the outbox primary key). The
    # command_id is not part of the envelope consistency rules, so the
    # re-validated envelope stays legal and the insert hits the real primary
    # key conflict.
    colliding = _envelope(queued_second).model_copy(
        update={"command_id": first_command.command_id}
    )

    with pytest.raises(OperationPersistenceError):
        await operation_repo.queue_operation_with_events_and_command(
            scope,
            operation=queued_second,
            events=(_queued_event(queued_second),),
            command=colliding,
            expected_version=second.version,
        )

    rolled_back = await operation_repo.get_operation(scope, second.operation_id)
    assert rolled_back is not None
    assert rolled_back.status == "pending_confirmation"
    assert rolled_back.version == 1
    # No queued/status event for the second aggregate.
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT COUNT(*) FROM case_management.order_operation_events
            WHERE operation_id = %s
            """,
            (second.operation_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 0
    # No outbox row for the second aggregate, and the first stays intact.
    assert await integration_repo.get_outbox_message(colliding.command_id) is not None
    first_stored = await integration_repo.get_outbox_message(first_command.command_id)
    assert first_stored is not None
    assert first_stored.status == "pending"
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM integration.outbox_messages WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 1


async def test_optimistic_lock_failure_produces_no_outbox(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    """A version conflict must reach the SQL UPDATE and roll back all writes."""
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)

    created = _operation(tenant_id=tenant_id)
    await operation_repo.create_operation_with_events(
        scope,
        operation=created,
        events=(),
    )
    # The queued model is internally consistent (version == expected + 1),
    # but the database still holds version 1: the UPDATE must match no row.
    queued = created.model_copy(
        update={"status": "queued", "version": created.version + 2}
    )

    with pytest.raises(ConcurrentOperationUpdateError):
        await operation_repo.queue_operation_with_events_and_command(
            scope,
            operation=queued,
            events=(_queued_event(queued),),
            command=_envelope(queued),
            expected_version=created.version + 1,
        )

    stored = await operation_repo.get_operation(scope, created.operation_id)
    assert stored is not None
    assert stored.status == "pending_confirmation"
    assert stored.version == 1
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT COUNT(*) FROM case_management.order_operation_events
            WHERE operation_id = %s
            """,
            (created.operation_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 0
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM integration.outbox_messages WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 0


async def test_case_event_and_outbox_commit_atomically(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    case_repo = PostgresCaseRepository(pool)

    case = _delivery_case(tenant_id=tenant_id, order_id="ORD-10010")
    command = _delivery_envelope(case)

    await case_repo.create_case_with_event_and_command(
        scope,
        case=case,
        event=_case_created_event(case),
        command=command,
    )

    stored = await case_repo.get_case(scope, case.case_id)
    assert stored is not None
    outbox = await PostgresIntegrationRepository(pool).get_outbox_message(
        command.command_id
    )
    assert outbox is not None
    assert outbox.aggregate_type == "support_case"
    assert outbox.aggregate_id == case.case_id
    assert outbox.expected_order_version is None
    assert outbox.idempotency_key == f"delivery-investigation:{case.case_id}"


async def test_outbox_failure_rolls_back_case(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    case_repo = PostgresCaseRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)

    first = _delivery_case(
        tenant_id=tenant_id, order_id="ORD-10010", source_message_id="message-case-1"
    )
    first_command = _delivery_envelope(first)
    await case_repo.create_case_with_event_and_command(
        scope,
        case=first,
        event=_case_created_event(first),
        command=first_command,
    )

    # A second case with no shared uniqueness; only the outbox command_id
    # collides (the primary key), which re-validates as a legal envelope.
    second = _delivery_case(
        tenant_id=tenant_id, order_id="ORD-10011", source_message_id="message-case-2"
    )
    conflicting = _delivery_envelope(second).model_copy(
        update={"command_id": first_command.command_id}
    )

    with pytest.raises(CasePersistenceError):
        await case_repo.create_case_with_event_and_command(
            scope,
            case=second,
            event=_case_created_event(second),
            command=conflicting,
        )

    assert await case_repo.get_case(scope, second.case_id) is None
    assert await integration_repo.get_outbox_message(conflicting.command_id) is not None
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM integration.outbox_messages WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 1


async def test_case_optimistic_lock_failure_produces_no_outbox(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    case_repo = PostgresCaseRepository(pool)

    case = _delivery_case(tenant_id=tenant_id, order_id="ORD-10010")
    await case_repo.create_case_with_event(
        scope,
        case=case,
        event=_case_created_event(case),
    )
    # Internally consistent (version == expected + 1) but the database still
    # holds version 1: the UPDATE must match no row.
    updated = case.model_copy(
        update={
            "status": "in_progress",
            "updated_at": NOW,
            "version": 3,
        }
    )

    with pytest.raises(ConcurrentCaseUpdateError):
        await case_repo.update_case_with_event_and_command(
            scope,
            case=updated,
            event=_case_created_event(updated),
            command=_delivery_envelope(updated),
            expected_version=2,
        )

    stored = await case_repo.get_case(scope, case.case_id)
    assert stored is not None
    assert stored.status == "open"
    assert stored.version == 1
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM integration.outbox_messages WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 0


async def test_claim_creates_attempt_and_blocks_second_worker(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    created = _operation(tenant_id=tenant_id)
    await operation_repo.create_operation_with_events(
        scope,
        operation=created,
        events=(),
    )
    queued = created.model_copy(
        update={"status": "queued", "version": created.version + 1}
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=_envelope(queued),
        expected_version=created.version,
    )

    claimed = await integration_repo.claim_due_outbox(
        worker_id="worker-1", batch_size=10, lease_seconds=90
    )
    second = await integration_repo.claim_due_outbox(
        worker_id="worker-2", batch_size=10, lease_seconds=90
    )

    assert len(claimed) == 1
    assert claimed[0].status == "processing"
    assert claimed[0].lease_owner == "worker-1"
    assert claimed[0].attempt.attempt_number == 1
    assert claimed[0].attempt.worker_id == "worker-1"
    assert claimed[0].attempts_in_cycle == 1
    assert second == []

    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM integration.outbox_delivery_attempts"
        )
        rows = await cursor.fetchone()
    assert rows[0] == 1


async def test_concurrent_workers_claim_disjoint_tasks(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    # Each command must go through the real business transition: persist a
    # pending operation, then queue it, then let two workers claim. Distinct
    # order ids keep the active-order unique index out of the picture so the
    # test only exercises concurrent claiming.
    for index in range(2):
        created = _operation(
            tenant_id=tenant_id,
            order_id=f"ORD-{10001 + index}",
            source_message_id=f"message-{index}",
        )
        await operation_repo.create_operation_with_events(
            scope,
            operation=created,
            events=(),
        )
        queued = created.model_copy(
            update={"status": "queued", "version": created.version + 1}
        )
        await operation_repo.queue_operation_with_events_and_command(
            scope,
            operation=queued,
            events=(_queued_event(queued),),
            command=_envelope(queued),
            expected_version=created.version,
        )

    results = await asyncio.gather(
        integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=90
        ),
        integration_repo.claim_due_outbox(
            worker_id="worker-2", batch_size=10, lease_seconds=90
        ),
    )

    claimed_ids = [item.command_id for batch in results for item in batch]
    assert len(claimed_ids) == 2
    assert len(set(claimed_ids)) == 2


async def test_fencing_token_blocks_stale_worker(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    created = _operation(tenant_id=tenant_id)
    await operation_repo.create_operation_with_events(
        scope,
        operation=created,
        events=(),
    )
    queued = created.model_copy(
        update={"status": "queued", "version": created.version + 1}
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=_envelope(queued),
        expected_version=created.version,
    )
    claimed = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=90
        )
    )[0]

    assert (
        await integration_repo.renew_outbox_lease(
            command_id=claimed.command_id,
            lease_id=uuid4(),
            lease_owner="worker-1",
            lease_seconds=90,
        )
        is False
    )
    with pytest.raises(LeaseConflictError):
        await integration_repo.schedule_outbox_retry(
            command_id=claimed.command_id,
            lease_id=uuid4(),
            lease_owner="worker-1",
            attempt_id=claimed.attempt.attempt_id,
            failure_kind="http_retryable",
            error_code="http_500",
            error_message="upstream",
            retry_after_seconds=5,
            next_available_at=NOW + timedelta(seconds=5),
        )


async def test_lease_expiry_recovery(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    created = _operation(tenant_id=tenant_id)
    await operation_repo.create_operation_with_events(
        scope,
        operation=created,
        events=(),
    )
    queued = created.model_copy(
        update={"status": "queued", "version": created.version + 1}
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=_envelope(queued),
        expected_version=created.version,
    )
    claimed = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=0.1
        )
    )[0]

    await asyncio.sleep(0.3)
    recovered = await integration_repo.recover_expired_outbox_leases(batch_size=10)

    assert recovered == 1
    message = await integration_repo.get_outbox_message(claimed.command_id)
    assert message is not None
    assert message.status == "retry_scheduled"
    assert message.lease_id is None
    assert message.delivery_cycle == 1
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT outcome FROM integration.outbox_delivery_attempts
            WHERE attempt_id = %s
            """,
            (claimed.attempt.attempt_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == "lease_expired"


async def test_attempts_in_cycle_increment_and_dead_after_eight(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    created = _operation(tenant_id=tenant_id)
    await operation_repo.create_operation_with_events(
        scope,
        operation=created,
        events=(),
    )
    queued = created.model_copy(
        update={"status": "queued", "version": created.version + 1}
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=_envelope(queued),
        expected_version=created.version,
    )

    current = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=90
        )
    )[0]
    for index in range(1, 8):
        await integration_repo.schedule_outbox_retry(
            command_id=current.command_id,
            lease_id=current.lease_id,
            lease_owner=current.lease_owner,
            attempt_id=current.attempt.attempt_id,
            failure_kind="http_retryable",
            error_code="http_500",
            error_message="upstream",
            retry_after_seconds=1,
            next_available_at=NOW + timedelta(seconds=index),
        )
        current = (
            await integration_repo.claim_due_outbox(
                worker_id="worker-1", batch_size=10, lease_seconds=90
            )
        )[0]

    assert current.attempts_in_cycle == 8
    async with pool.connection() as connection:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT clock_timestamp() AS now")
                now = (await cursor.fetchone())[0]
                affected = await finish_delivery_attempt(
                    cursor,
                    attempt_id=current.attempt.attempt_id,
                    command_id=current.command_id,
                    lease_id=current.lease_id,
                    worker_id=current.lease_owner,
                    finished_at=now,
                    outcome="terminal_failure",
                    failure_kind="http_client_error",
                    safe_error_code="http_400",
                    safe_error_message="provider rejected",
                )
                assert affected == 1
                affected = await update_outbox_transition(
                    cursor,
                    command_id=current.command_id,
                    expected_status="processing",
                    expected_lease_id=current.lease_id,
                    expected_lease_owner=current.lease_owner,
                    target_status="dead",
                    updated_at=now,
                    dead_at=now,
                )
                assert affected == 1

    message = await integration_repo.get_outbox_message(current.command_id)
    assert message is not None
    assert message.status == "dead"
    assert message.dead_at is not None
    assert message.attempts_in_cycle == 8


async def test_redrive_opens_new_cycle_and_preserves_idempotency_key(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    created = _operation(tenant_id=tenant_id)
    await operation_repo.create_operation_with_events(
        scope,
        operation=created,
        events=(),
    )
    queued = created.model_copy(
        update={"status": "queued", "version": created.version + 1}
    )
    command = _envelope(queued)
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=created.version,
    )
    current = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=90
        )
    )[0]
    async with pool.connection() as connection:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT clock_timestamp() AS now")
                now = (await cursor.fetchone())[0]
                await finish_delivery_attempt(
                    cursor,
                    attempt_id=current.attempt.attempt_id,
                    command_id=current.command_id,
                    lease_id=current.lease_id,
                    worker_id=current.lease_owner,
                    finished_at=now,
                    outcome="terminal_failure",
                    failure_kind="validation_error",
                    safe_error_code="bad_payload",
                    safe_error_message="invalid",
                )
                await update_outbox_transition(
                    cursor,
                    command_id=current.command_id,
                    expected_status="processing",
                    expected_lease_id=current.lease_id,
                    expected_lease_owner=current.lease_owner,
                    target_status="dead",
                    updated_at=now,
                    dead_at=now,
                )

    redrive = await integration_repo.redrive_dead_outbox(
        command_id=current.command_id,
        tenant_id=tenant_id,
        request_id="redrive-1",
        requested_by="sup-1",
        reason="provider recovered",
        redrive_id=uuid4(),
        created_at=NOW,
    )

    assert redrive.previous_cycle == 1
    assert redrive.new_cycle == 2
    message = await integration_repo.get_outbox_message(current.command_id)
    assert message is not None
    assert message.status == "retry_scheduled"
    assert message.delivery_cycle == 2
    assert message.attempts_in_cycle == 0
    assert message.dead_at is None
    assert message.idempotency_key == command.idempotency_key


async def test_redrive_rejects_duplicate_request_and_non_dead_state(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    created = _operation(tenant_id=tenant_id)
    await operation_repo.create_operation_with_events(
        scope,
        operation=created,
        events=(),
    )
    queued = created.model_copy(
        update={"status": "queued", "version": created.version + 1}
    )
    command = _envelope(queued)
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=created.version,
    )

    # The outbox command_id is the envelope's command id, never the
    # operation_id.
    with pytest.raises(InvalidRedriveStateError):
        await integration_repo.redrive_dead_outbox(
            command_id=command.command_id,
            tenant_id=tenant_id,
            request_id="redrive-1",
            requested_by="sup-1",
            reason="too early",
            redrive_id=uuid4(),
            created_at=NOW,
        )

    current = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=90
        )
    )[0]
    async with pool.connection() as connection:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT clock_timestamp() AS now")
                now = (await cursor.fetchone())[0]
                await finish_delivery_attempt(
                    cursor,
                    attempt_id=current.attempt.attempt_id,
                    command_id=current.command_id,
                    lease_id=current.lease_id,
                    worker_id=current.lease_owner,
                    finished_at=now,
                    outcome="terminal_failure",
                    failure_kind="validation_error",
                    safe_error_code="bad_payload",
                    safe_error_message="invalid",
                )
                await update_outbox_transition(
                    cursor,
                    command_id=current.command_id,
                    expected_status="processing",
                    expected_lease_id=current.lease_id,
                    expected_lease_owner=current.lease_owner,
                    target_status="dead",
                    updated_at=now,
                    dead_at=now,
                )

    await integration_repo.redrive_dead_outbox(
        command_id=current.command_id,
        tenant_id=tenant_id,
        request_id="redrive-1",
        requested_by="sup-1",
        reason="provider recovered",
        redrive_id=uuid4(),
        created_at=NOW,
    )
    with pytest.raises(DuplicateRedriveRequestError):
        await integration_repo.redrive_dead_outbox(
            command_id=current.command_id,
            tenant_id=tenant_id,
            request_id="redrive-1",
            requested_by="sup-1",
            reason="provider recovered again",
            redrive_id=uuid4(),
            created_at=NOW,
        )


async def test_webhook_receive_is_idempotent_by_event_id(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    integration_repo = PostgresIntegrationRepository(pool)
    command_id = uuid4()
    event = _webhook_event(command_id=command_id)
    raw_body = b'{"command_id":"x"}'
    body_hash = hashlib.sha256(raw_body).hexdigest()

    first = await integration_repo.receive_inbox_idempotently(
        inbox_id=uuid4(),
        provider_connection_id="wc-1",
        event_id="evt-dup-1",
        tenant_id=tenant_id,
        event=event,
        raw_body_sha256=body_hash,
        received_at=NOW,
    )
    second = await integration_repo.receive_inbox_idempotently(
        inbox_id=uuid4(),
        provider_connection_id="wc-1",
        event_id="evt-dup-1",
        tenant_id=tenant_id,
        event=event,
        raw_body_sha256=body_hash,
        received_at=NOW,
    )

    assert first.inbox_id == second.inbox_id
    assert first.raw_body_sha256 == body_hash
    assert first.command_id == command_id
    assert first.tenant_id == tenant_id
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM integration.inbox_messages WHERE event_id = %s",
            ("evt-dup-1",),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 1


async def test_inbox_claim_creates_processing_attempt_and_lease_recovery(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    integration_repo = PostgresIntegrationRepository(pool)
    command_id = uuid4()
    inbox = await integration_repo.receive_inbox_idempotently(
        inbox_id=uuid4(),
        provider_connection_id="wc-1",
        event_id="evt-claim-1",
        tenant_id=tenant_id,
        event=_webhook_event(command_id=command_id),
        raw_body_sha256=hashlib.sha256(b"body").hexdigest(),
        received_at=NOW,
    )

    claimed = await integration_repo.claim_due_inbox(
        worker_id="worker-1", batch_size=10, lease_seconds=0.1
    )
    assert len(claimed) == 1
    assert claimed[0].inbox_id == inbox.inbox_id
    assert claimed[0].status == "processing"
    assert claimed[0].attempt.attempt_number == 1
    assert claimed[0].processing_attempts == 1

    second = await integration_repo.claim_due_inbox(
        worker_id="worker-2", batch_size=10, lease_seconds=60
    )
    assert second == []

    await asyncio.sleep(0.3)
    recovered = await integration_repo.recover_expired_inbox_leases(batch_size=10)
    assert recovered == 1
    message = await integration_repo.get_inbox_message(inbox.inbox_id)
    assert message is not None
    assert message.status == "received"
    assert message.lease_id is None
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT outcome FROM integration.inbox_processing_attempts
            WHERE inbox_id = %s
            """,
            (inbox.inbox_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == "lease_expired"


async def test_inbox_does_not_store_raw_body_or_signature(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    integration_repo = PostgresIntegrationRepository(pool)
    raw_body = b'{"raw":"payload","signature":"should-not-be-stored"}'
    inbox = await integration_repo.receive_inbox_idempotently(
        inbox_id=uuid4(),
        provider_connection_id="wc-1",
        event_id="evt-safe-1",
        tenant_id=tenant_id,
        event=_webhook_event(command_id=uuid4()),
        raw_body_sha256=hashlib.sha256(raw_body).hexdigest(),
        received_at=NOW,
    )

    assert "should-not-be-stored" not in inbox.payload.model_dump_json()
    assert raw_body.decode() not in inbox.payload.model_dump_json()


async def test_historical_submitted_operation_has_no_outbox(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)

    await operation_repo.create_operation_with_events(
        scope,
        operation=_operation(tenant_id=tenant_id, status="submitted"),
        events=(),
    )

    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM integration.outbox_messages WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 0


async def test_delivery_investigation_is_isolated_per_order(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    case_repo = PostgresCaseRepository(pool)

    first = _delivery_case(
        tenant_id=tenant_id, order_id="ORD-10010", source_message_id="message-iso-1"
    )
    await case_repo.create_case_with_event_and_command(
        scope,
        case=first,
        event=_case_created_event(first),
        command=_delivery_envelope(first),
    )
    # Same thread + same order conflicts on the delivery unique index; the
    # distinct source message keeps the event idempotency key unique so only
    # the delivery index can reject the insert.
    same_order = _delivery_case(
        tenant_id=tenant_id, order_id="ORD-10010", source_message_id="message-iso-2"
    )
    with pytest.raises(ActiveCaseConflictError):
        await case_repo.create_case_with_event_and_command(
            scope,
            case=same_order,
            event=_case_created_event(same_order),
            command=_delivery_envelope(same_order),
        )
    # Same thread + different order creates its own case without conflict.
    other_order = _delivery_case(
        tenant_id=tenant_id, order_id="ORD-10011", source_message_id="message-iso-3"
    )
    await case_repo.create_case_with_event_and_command(
        scope,
        case=other_order,
        event=_case_created_event(other_order),
        command=_delivery_envelope(other_order),
    )

    stored_other = await case_repo.get_case(scope, other_order.case_id)
    assert stored_other is not None
    assert stored_other.order_id == "ORD-10011"
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT COUNT(*) FROM case_management.support_cases
            WHERE tenant_id = %s AND thread_id = %s AND case_type = 'delivery_investigation'
            """,
            (tenant_id, f"{tenant_id}-thread-1"),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 2


async def test_cross_tenant_delivery_cases_do_not_conflict(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    case_repo = PostgresCaseRepository(pool)
    scope_a = _scope(f"{tenant_id}-a")
    scope_b = _scope(f"{tenant_id}-b")

    case_a = _delivery_case(tenant_id=f"{tenant_id}-a", order_id="ORD-10010")
    case_b = _delivery_case(tenant_id=f"{tenant_id}-b", order_id="ORD-10010")
    await case_repo.create_case_with_event_and_command(
        scope_a,
        case=case_a,
        event=_case_created_event(case_a),
        command=_delivery_envelope(case_a),
    )
    await case_repo.create_case_with_event_and_command(
        scope_b,
        case=case_b,
        event=_case_created_event(case_b),
        command=_delivery_envelope(case_b),
    )

    assert await case_repo.get_case(scope_a, case_a.case_id) is not None
    assert await case_repo.get_case(scope_b, case_b.case_id) is not None


# ----------------------------------------------------------------------
# Review fixes: aggregate/command association, inbox conflicts,
# tenant-scoped uniqueness, attempt fencing, 8-attempt cap, error bounds
# ----------------------------------------------------------------------


async def _persist_pending_operation(
    operation_repo: PostgresOrderOperationRepository,
    scope,
    tenant_id: str,
):
    """Persist one pending operation and return its queued twin plus command.

    Only the pending row is written here; callers decide whether (and with
    which envelope) they call ``queue_operation_with_events_and_command``.
    """
    created = _operation(tenant_id=tenant_id)
    await operation_repo.create_operation_with_events(
        scope,
        operation=created,
        events=(),
    )
    queued = created.model_copy(
        update={"status": "queued", "version": created.version + 1}
    )
    command = _envelope(queued)
    return queued, command


async def test_operation_association_mismatches_produce_no_writes(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    queued, command = await _persist_pending_operation(
        operation_repo, scope, tenant_id
    )

    other_id = uuid4()
    mismatches = [
        # Internally consistent envelope whose aggregate is a different
        # operation: exercises the association check, not the envelope.
        command.model_copy(
            update={
                "aggregate_id": other_id,
                "idempotency_key": f"order-operation:{other_id}",
            }
        ),
        command.model_copy(update={"tenant_id": f"{tenant_id}-other"}),
        command.model_copy(
            update={"payload": command.payload.model_copy(update={"order_id": "ORD-99999"})}
        ),
        command.model_copy(update={"expected_order_version": 1}),
    ]
    for mismatched in mismatches:
        with pytest.raises(ValueError):
            await operation_repo.queue_operation_with_events_and_command(
                scope,
                operation=queued,
                events=(_queued_event(queued),),
                command=mismatched,
                expected_version=1,
            )

    with pytest.raises(ValueError, match="version"):
        await operation_repo.queue_operation_with_events_and_command(
            scope,
            operation=queued,
            events=(_queued_event(queued),),
            command=command,
            expected_version=99,
        )

    mismatched_event = _queued_event(queued).model_copy(
        update={"operation_id": uuid4()}
    )
    with pytest.raises(ValueError, match="event.operation_id"):
        await operation_repo.queue_operation_with_events_and_command(
            scope,
            operation=queued,
            events=(mismatched_event,),
            command=command,
            expected_version=1,
        )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    assert stored is not None
    assert stored.status == "pending_confirmation"
    assert stored.version == 1
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM integration.outbox_messages WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 0
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT COUNT(*) FROM case_management.order_operation_events
            WHERE operation_id = %s
            """,
            (queued.operation_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 0


async def test_case_association_mismatches_produce_no_writes(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    case_repo = PostgresCaseRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    case = _delivery_case(tenant_id=tenant_id, order_id="ORD-10010")
    command = _delivery_envelope(case)
    event = _case_created_event(case)

    mismatches = [
        # Internally consistent envelope whose aggregate is a different case.
        command.model_copy(
            update={
                "aggregate_id": uuid4(),
                "idempotency_key": "delivery-investigation:00000000-0000-0000-0000-000000000000",
            }
        ),
        command.model_copy(update={"tenant_id": f"{tenant_id}-other"}),
        command.model_copy(
            update={"payload": command.payload.model_copy(update={"order_id": "ORD-99999"})}
        ),
        # A tampered command_type fails the envelope revalidation.
        command.model_copy(update={"command_type": "return_order"}),
    ]
    for mismatched in mismatches:
        with pytest.raises(ValueError):
            await case_repo.create_case_with_event_and_command(
                scope,
                case=case,
                event=event,
                command=mismatched,
            )

    with pytest.raises(ValueError, match="event.case_id"):
        await case_repo.create_case_with_event_and_command(
            scope,
            case=case,
            event=event.model_copy(update={"case_id": uuid4()}),
            command=command,
        )

    assert await case_repo.get_case(scope, case.case_id) is None
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM integration.outbox_messages WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 0
    assert await integration_repo.get_outbox_message(command.command_id) is None


async def test_inbox_exact_replay_returns_same_record(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    integration_repo = PostgresIntegrationRepository(pool)
    command_id = uuid4()
    event = _webhook_event(command_id=command_id)
    body_hash = hashlib.sha256(b"same body").hexdigest()

    first = await integration_repo.receive_inbox_idempotently(
        inbox_id=uuid4(),
        provider_connection_id="wc-1",
        event_id="evt-replay-1",
        tenant_id=tenant_id,
        event=event,
        raw_body_sha256=body_hash,
        received_at=NOW,
    )
    second = await integration_repo.receive_inbox_idempotently(
        inbox_id=uuid4(),
        provider_connection_id="wc-1",
        event_id="evt-replay-1",
        tenant_id=tenant_id,
        event=event,
        raw_body_sha256=body_hash,
        received_at=NOW,
    )

    assert first.inbox_id == second.inbox_id
    assert second.raw_body_sha256 == body_hash


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.model_copy(update={"command_id": uuid4()}),
        lambda event: event.model_copy(update={"aggregate_id": uuid4()}),
        lambda event: event.model_copy(update={"command_status": "completed"}),
    ],
    ids=["command_id", "aggregate_id", "payload"],
)
async def test_inbox_event_conflicts_are_rejected(
    postgres_context: tuple[AsyncConnectionPool, str],
    mutate,
) -> None:
    pool, tenant_id = postgres_context
    integration_repo = PostgresIntegrationRepository(pool)
    command_id = uuid4()
    event = _webhook_event(command_id=command_id)
    body_hash = hashlib.sha256(b"body").hexdigest()

    await integration_repo.receive_inbox_idempotently(
        inbox_id=uuid4(),
        provider_connection_id="wc-1",
        event_id="evt-conflict-1",
        tenant_id=tenant_id,
        event=event,
        raw_body_sha256=body_hash,
        received_at=NOW,
    )

    with pytest.raises(InboxEventConflictError):
        await integration_repo.receive_inbox_idempotently(
            inbox_id=uuid4(),
            provider_connection_id="wc-1",
            event_id="evt-conflict-1",
            tenant_id=tenant_id,
            event=mutate(event),
            raw_body_sha256=body_hash,
            received_at=NOW,
        )


async def test_inbox_event_conflicts_on_hash_and_tenant(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    integration_repo = PostgresIntegrationRepository(pool)
    command_id = uuid4()
    event = _webhook_event(command_id=command_id)
    body_hash = hashlib.sha256(b"body").hexdigest()
    original = await integration_repo.receive_inbox_idempotently(
        inbox_id=uuid4(),
        provider_connection_id="wc-1",
        event_id="evt-conflict-2",
        tenant_id=tenant_id,
        event=event,
        raw_body_sha256=body_hash,
        received_at=NOW,
    )

    with pytest.raises(InboxEventConflictError):
        await integration_repo.receive_inbox_idempotently(
            inbox_id=uuid4(),
            provider_connection_id="wc-1",
            event_id="evt-conflict-2",
            tenant_id=tenant_id,
            event=event,
            raw_body_sha256=hashlib.sha256(b"other body").hexdigest(),
            received_at=NOW,
        )
    with pytest.raises(InboxEventConflictError):
        await integration_repo.receive_inbox_idempotently(
            inbox_id=uuid4(),
            provider_connection_id="wc-1",
            event_id="evt-conflict-2",
            tenant_id=f"{tenant_id}-other",
            event=event,
            raw_body_sha256=body_hash,
            received_at=NOW,
        )

    preserved = await integration_repo.get_inbox_message(original.inbox_id)
    assert preserved is not None
    assert preserved.tenant_id == tenant_id
    assert preserved.raw_body_sha256 == body_hash
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM integration.inbox_messages WHERE event_id = %s",
            ("evt-conflict-2",),
        )
        rows = await cursor.fetchone()
    assert rows[0] == 1


async def test_active_operation_uniqueness_is_tenant_scoped(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    operation_repo = PostgresOrderOperationRepository(pool)
    scope_a = _scope(f"{tenant_id}-a")
    scope_b = _scope(f"{tenant_id}-b")

    # Same tenant + same order, but a different message and idempotency key:
    # only the active-order unique index can reject the second insert.
    first = _operation(
        tenant_id=f"{tenant_id}-a",
        order_id="ORD-50001",
        source_message_id="message-active-1",
    )
    await operation_repo.create_operation_with_events(scope_a, operation=first, events=())
    with pytest.raises(ActiveOrderOperationConflictError):
        await operation_repo.create_operation_with_events(
            scope_a,
            operation=_operation(
                tenant_id=f"{tenant_id}-a",
                order_id="ORD-50001",
                source_message_id="message-active-2",
            ),
            events=(),
        )

    # Different tenant may reuse the same order id.
    second = _operation(
        tenant_id=f"{tenant_id}-b",
        order_id="ORD-50001",
        source_message_id="message-active-1",
    )
    await operation_repo.create_operation_with_events(scope_b, operation=second, events=())
    assert await operation_repo.get_operation(scope_b, second.operation_id) is not None


async def test_historical_terminal_operations_do_not_block_new_active(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)

    for index, status in enumerate(
        ("completed", "rejected", "cancelled_by_customer")
    ):
        old = _operation(
            tenant_id=tenant_id,
            order_id="ORD-60001",
            source_message_id=f"message-hist-{index}",
            status=status,
        )
        await operation_repo.create_operation_with_events(scope, operation=old, events=())

    fresh = _operation(
        tenant_id=tenant_id,
        order_id="ORD-60001",
        source_message_id="message-hist-new",
    )
    await operation_repo.create_operation_with_events(scope, operation=fresh, events=())

    # All three terminal records plus the new active record exist.
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT status FROM case_management.order_operations
            WHERE tenant_id = %s AND order_id = %s
            ORDER BY created_at, operation_id
            """,
            (tenant_id, "ORD-60001"),
        )
        rows = await cursor.fetchall()
    assert sorted(row[0] for row in rows) == [
        "cancelled_by_customer",
        "completed",
        "pending_confirmation",
        "rejected",
    ]


async def test_attempt_fencing_blocks_mismatched_finalization(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    queued_a, command_a = await _persist_pending_operation(
        operation_repo, scope, tenant_id
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued_a,
        events=(_queued_event(queued_a),),
        command=command_a,
        expected_version=1,
    )
    scope_b = _scope(f"{tenant_id}-b")
    queued_b, command_b = await _persist_pending_operation(
        operation_repo, scope_b, f"{tenant_id}-b"
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope_b,
        operation=queued_b,
        events=(_queued_event(queued_b),),
        command=command_b,
        expected_version=1,
    )

    claimed = await integration_repo.claim_due_outbox(
        worker_id="worker-1", batch_size=10, lease_seconds=90
    )
    claimed_a, claimed_b = claimed[0], claimed[1]

    # command A's attempt paired with command B fails and rolls back.
    with pytest.raises(LeaseConflictError):
        await integration_repo.schedule_outbox_retry(
            command_id=claimed_b.command_id,
            lease_id=claimed_b.lease_id,
            lease_owner="worker-1",
            attempt_id=claimed_a.attempt.attempt_id,
            failure_kind="http_retryable",
            error_code="http_500",
            error_message="upstream",
            retry_after_seconds=5,
            next_available_at=NOW + timedelta(seconds=5),
        )
    # Correct command, wrong lease.
    with pytest.raises(LeaseConflictError):
        await integration_repo.schedule_outbox_retry(
            command_id=claimed_a.command_id,
            lease_id=uuid4(),
            lease_owner="worker-1",
            attempt_id=claimed_a.attempt.attempt_id,
            failure_kind="http_retryable",
            error_code="http_500",
            error_message="upstream",
            retry_after_seconds=5,
            next_available_at=NOW + timedelta(seconds=5),
        )
    # Correct command and lease, wrong worker.
    with pytest.raises(LeaseConflictError):
        await integration_repo.schedule_outbox_retry(
            command_id=claimed_a.command_id,
            lease_id=claimed_a.lease_id,
            lease_owner="worker-9",
            attempt_id=claimed_a.attempt.attempt_id,
            failure_kind="http_retryable",
            error_code="http_500",
            error_message="upstream",
            retry_after_seconds=5,
            next_available_at=NOW + timedelta(seconds=5),
        )

    # The mismatched attempts must not have been finalized.
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT outcome FROM integration.outbox_delivery_attempts
            WHERE attempt_id = %s
            """,
            (claimed_a.attempt.attempt_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] is None
    message_a = await integration_repo.get_outbox_message(claimed_a.command_id)
    assert message_a is not None
    assert message_a.status == "processing"


async def test_inbox_attempt_fencing_blocks_cross_inbox_finalization(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    integration_repo = PostgresIntegrationRepository(pool)
    event = _webhook_event(command_id=uuid4())
    body_hash = hashlib.sha256(b"body").hexdigest()
    for index in range(2):
        await integration_repo.receive_inbox_idempotently(
            inbox_id=uuid4(),
            provider_connection_id="wc-1",
            event_id=f"evt-fence-{index}",
            tenant_id=tenant_id,
            event=event,
            raw_body_sha256=body_hash,
            received_at=NOW,
        )
    claimed = await integration_repo.claim_due_inbox(
        worker_id="worker-1", batch_size=10, lease_seconds=60
    )
    first, second = claimed[0], claimed[1]

    # Crossing the attempt of `first` with the inbox of `second` matches no row.
    async with pool.connection() as connection:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT clock_timestamp() AS now")
                now = (await cursor.fetchone())[0]
                affected = await finish_inbox_attempt(
                    cursor,
                    attempt_id=first.attempt.attempt_id,
                    inbox_id=second.inbox_id,
                    lease_id=second.lease_id,
                    worker_id="worker-1",
                    finished_at=now,
                    outcome="processed",
                )
                assert affected == 0
                # Wrong worker on the correct attempt also matches no row.
                affected = await finish_inbox_attempt(
                    cursor,
                    attempt_id=first.attempt.attempt_id,
                    inbox_id=first.inbox_id,
                    lease_id=first.lease_id,
                    worker_id="worker-9",
                    finished_at=now,
                    outcome="processed",
                )
                assert affected == 0

    # Nothing was finalized: both attempts are still open.
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT outcome FROM integration.inbox_processing_attempts
            WHERE finished_at IS NULL
            """
        )
        rows = await cursor.fetchall()
    assert len(rows) == 2


async def test_ninth_attempt_is_impossible_and_eighth_retry_rejected(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    queued, command = await _persist_pending_operation(
        operation_repo, scope, tenant_id
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=1,
    )

    current = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=90
        )
    )[0]
    attempt_numbers = [current.attempt.attempt_number]
    for index in range(1, 8):
        await integration_repo.schedule_outbox_retry(
            command_id=current.command_id,
            lease_id=current.lease_id,
            lease_owner=current.lease_owner,
            attempt_id=current.attempt.attempt_id,
            failure_kind="http_retryable",
            error_code="http_500",
            error_message="upstream",
            retry_after_seconds=1,
            next_available_at=NOW + timedelta(seconds=index),
        )
        current = (
            await integration_repo.claim_due_outbox(
                worker_id="worker-1", batch_size=10, lease_seconds=90
            )
        )[0]
        attempt_numbers.append(current.attempt.attempt_number)

    assert attempt_numbers == [1, 2, 3, 4, 5, 6, 7, 8]
    # The 8th attempt must not be allowed to schedule a retry.
    with pytest.raises(OutboxAttemptsExhaustedError):
        await integration_repo.schedule_outbox_retry(
            command_id=current.command_id,
            lease_id=current.lease_id,
            lease_owner=current.lease_owner,
            attempt_id=current.attempt.attempt_id,
            failure_kind="http_retryable",
            error_code="http_500",
            error_message="upstream",
            retry_after_seconds=1,
            next_available_at=NOW + timedelta(seconds=9),
        )
    # Nothing may be claimed anymore: a 9th attempt cannot exist.
    assert (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=90
        )
        == []
    )
    # The rejected retry left no partial state.
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT outcome FROM integration.outbox_delivery_attempts
            WHERE attempt_id = %s
            """,
            (current.attempt.attempt_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] is None
    message = await integration_repo.get_outbox_message(current.command_id)
    assert message is not None
    assert message.status == "processing"
    assert message.attempts_in_cycle == 8


async def test_seventh_lease_expiry_recovers_to_retry_scheduled(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    queued, command = await _persist_pending_operation(
        operation_repo, scope, tenant_id
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=1,
    )

    current = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=90
        )
    )[0]
    for index in range(1, 7):
        await integration_repo.schedule_outbox_retry(
            command_id=current.command_id,
            lease_id=current.lease_id,
            lease_owner=current.lease_owner,
            attempt_id=current.attempt.attempt_id,
            failure_kind="http_retryable",
            error_code="http_500",
            error_message="upstream",
            retry_after_seconds=1,
            next_available_at=NOW + timedelta(seconds=index),
        )
        current = (
            await integration_repo.claim_due_outbox(
                worker_id="worker-1", batch_size=10, lease_seconds=0.1
            )
        )[0]
    assert current.attempts_in_cycle == 7

    await asyncio.sleep(0.3)
    recovered = await integration_repo.recover_expired_outbox_leases(batch_size=10)
    assert recovered == 1
    message = await integration_repo.get_outbox_message(current.command_id)
    assert message is not None
    assert message.status == "retry_scheduled"
    assert message.dead_at is None


async def test_eighth_lease_expiry_goes_dead(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    queued, command = await _persist_pending_operation(
        operation_repo, scope, tenant_id
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=1,
    )

    current = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=0.1
        )
    )[0]
    for index in range(1, 8):
        await integration_repo.schedule_outbox_retry(
            command_id=current.command_id,
            lease_id=current.lease_id,
            lease_owner=current.lease_owner,
            attempt_id=current.attempt.attempt_id,
            failure_kind="http_retryable",
            error_code="http_500",
            error_message="upstream",
            retry_after_seconds=1,
            next_available_at=NOW + timedelta(seconds=index),
        )
        current = (
            await integration_repo.claim_due_outbox(
                worker_id="worker-1", batch_size=10, lease_seconds=0.1
            )
        )[0]
    assert current.attempts_in_cycle == 8

    await asyncio.sleep(0.3)
    recovered = await integration_repo.recover_expired_outbox_leases(batch_size=10)
    assert recovered == 1
    message = await integration_repo.get_outbox_message(current.command_id)
    assert message is not None
    assert message.status == "dead"
    assert message.dead_at is not None
    assert message.last_error_code == "lease_expired_attempts_exhausted"
    async with pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT outcome, safe_error_code FROM integration.outbox_delivery_attempts
            WHERE attempt_id = %s
            """,
            (current.attempt.attempt_id,),
        )
        rows = await cursor.fetchone()
    assert rows[0] == "lease_expired"
    assert rows[1] == "lease_expired_attempts_exhausted"


async def test_redrive_starts_new_cycle_at_attempt_one(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    queued, command = await _persist_pending_operation(
        operation_repo, scope, tenant_id
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=1,
    )
    current = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=90
        )
    )[0]
    async with pool.connection() as connection:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT clock_timestamp() AS now")
                now = (await cursor.fetchone())[0]
                await finish_delivery_attempt(
                    cursor,
                    attempt_id=current.attempt.attempt_id,
                    command_id=current.command_id,
                    lease_id=current.lease_id,
                    worker_id=current.lease_owner,
                    finished_at=now,
                    outcome="terminal_failure",
                    failure_kind="validation_error",
                    safe_error_code="bad_payload",
                    safe_error_message="invalid",
                )
                await update_outbox_transition(
                    cursor,
                    command_id=current.command_id,
                    expected_status="processing",
                    expected_lease_id=current.lease_id,
                    expected_lease_owner=current.lease_owner,
                    target_status="dead",
                    updated_at=now,
                    dead_at=now,
                )
    redrive = await integration_repo.redrive_dead_outbox(
        command_id=current.command_id,
        tenant_id=tenant_id,
        request_id="redrive-cycle-1",
        requested_by="sup-1",
        reason="provider recovered",
        redrive_id=uuid4(),
        created_at=NOW,
    )
    assert redrive.new_cycle == 2

    claimed = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=90
        )
    )[0]
    assert claimed.attempt.attempt_number == 1
    assert claimed.delivery_cycle == 2
    message = await integration_repo.get_outbox_message(current.command_id)
    assert message is not None
    assert message.idempotency_key == command.idempotency_key


async def test_error_field_bounds_rejected_without_partial_state(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    queued, command = await _persist_pending_operation(
        operation_repo, scope, tenant_id
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=1,
    )
    current = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=90
        )
    )[0]

    for error_code, error_message in [
        ("x" * 501, "boom"),
        ("boom", "x" * 501),
        ("   ", "boom"),
        ("boom", "   "),
    ]:
        with pytest.raises(ValueError):
            await integration_repo.schedule_outbox_retry(
                command_id=current.command_id,
                lease_id=current.lease_id,
                lease_owner=current.lease_owner,
                attempt_id=current.attempt.attempt_id,
                failure_kind="http_retryable",
                error_code=error_code,
                error_message=error_message,
                retry_after_seconds=1,
                next_available_at=NOW + timedelta(seconds=1),
            )

    # No partial state: the attempt is still open and the message still processing.
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT outcome FROM integration.outbox_delivery_attempts"
        )
        rows = await cursor.fetchone()
    assert rows[0] is None
    message = await integration_repo.get_outbox_message(current.command_id)
    assert message is not None
    assert message.status == "processing"
    assert message.last_error_code is None


async def test_renew_after_lease_expiry_returns_false(
    postgres_context: tuple[AsyncConnectionPool, str],
) -> None:
    pool, tenant_id = postgres_context
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    queued, command = await _persist_pending_operation(
        operation_repo, scope, tenant_id
    )
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=1,
    )
    claimed = (
        await integration_repo.claim_due_outbox(
            worker_id="worker-1", batch_size=10, lease_seconds=0.1
        )
    )[0]

    await asyncio.sleep(0.3)
    assert (
        await integration_repo.renew_outbox_lease(
            command_id=claimed.command_id,
            lease_id=claimed.lease_id,
            lease_owner="worker-1",
            lease_seconds=90,
        )
        is False
    )
