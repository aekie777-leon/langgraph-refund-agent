"""End-to-end PostgreSQL coverage for one mixed Inbox Worker batch."""

import asyncio
import os

import pytest

from agent.integrations.inbox_postgres_finalizer import PostgresInboxFinalizer
from agent.integrations.inbox_worker import InboxProcessingWorker, InboxWorkerRunResult
from agent.integrations.postgres_repository import PostgresIntegrationRepository
from tests.integration_tests.test_postgres_provider_inbox import (
    _claimed_inbox,
    _status_events,
)
from tests.integration_tests.test_postgres_provider_inbox import (
    _events_for_idempotency_key as _order_events_for_idempotency_key,
)
from tests.integration_tests.test_postgres_provider_messaging import (
    postgres_context as _postgres_context,
)
from tests.integration_tests.test_postgres_provider_support_case_inbox import (
    _attempt_history_details,
    _case_events,
    _claimed_delivery_inbox,
)
from tests.integration_tests.test_postgres_provider_support_case_inbox import (
    _events_for_idempotency_key as _case_events_for_idempotency_key,
)

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]


@pytest.fixture
def anyio_backend() -> str | tuple[str, dict[str, object]]:
    if os.name == "nt":
        return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}
    return "asyncio"


@pytest.fixture
async def postgres_context():
    """Reuse the disposable Provider-messaging database and its tenant cleanup."""
    async for context in _postgres_context.__wrapped__():
        yield context


async def _expire_then_recover_claimed_inboxes(
    pool,
    repository: PostgresIntegrationRepository,
    *,
    first_inbox_id,
    second_inbox_id,
) -> None:
    """Legally recover two fixture claims so the tested Worker claims them itself."""
    async with pool.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                "UPDATE integration.inbox_messages "
                "SET lease_expires_at = clock_timestamp() - interval '1 second' "
                "WHERE inbox_id IN (%s, %s)",
                (first_inbox_id, second_inbox_id),
            )
            assert cursor.rowcount == 2
    assert await repository.recover_expired_inbox_leases(batch_size=2) == 2


def _assert_processed_inbox(inbox) -> None:
    """Assert the shared terminal Inbox state without copying aggregate policy."""
    assert inbox is not None
    assert inbox.status == "processed"
    assert inbox.processed_at is not None
    assert inbox.failed_at is None
    assert inbox.lease_id is None
    assert inbox.lease_owner is None
    assert inbox.lease_expires_at is None
    assert inbox.processing_attempts == 2


def _assert_recovered_attempt(history) -> None:
    """Assert recovery completed the initial fixture claim without resetting it."""
    assert len(history) == 1
    first = history[0]
    assert first[2] == 1
    assert first[5] is not None
    assert first[6:] == ("lease_expired", "lease_expired", None)


def _assert_attempt_history(history, *, worker_id: str) -> None:
    """Assert recovery and Worker claim form one auditable lifecycle."""
    _assert_recovered_attempt(history[:1])
    assert len(history) == 2
    second = history[1]
    assert second[2] == 2
    assert second[4] == worker_id
    assert second[5] is not None
    assert second[6:] == ("processed", None, None)


async def test_inbox_worker_processes_order_and_support_callbacks_in_one_batch(
    postgres_context,
) -> None:
    """One real claim batch atomically processes both supported Inbox aggregates."""
    pool, tenant_id = postgres_context
    (
        order_scope,
        operation_repo,
        integration_repo,
        queued_operation,
        order_inbox,
        claimed_order,
    ) = await _claimed_inbox(pool, tenant_id, "processing")
    (
        case_scope,
        case_repo,
        _delivery_integration_repo,
        support_case,
        support_inbox,
        claimed_support,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "completed")

    await _expire_then_recover_claimed_inboxes(
        pool,
        integration_repo,
        first_inbox_id=order_inbox.inbox_id,
        second_inbox_id=support_inbox.inbox_id,
    )
    order_recovered = await integration_repo.get_inbox_message(order_inbox.inbox_id)
    support_recovered = await integration_repo.get_inbox_message(support_inbox.inbox_id)
    assert order_recovered is not None and order_recovered.status == "received"
    assert support_recovered is not None and support_recovered.status == "received"
    assert order_recovered.lease_id is None and support_recovered.lease_id is None

    order_before = await operation_repo.get_operation(
        order_scope, queued_operation.operation_id
    )
    case_before = await case_repo.get_case(case_scope, support_case.case_id)
    order_outbox_before = await integration_repo.get_outbox_message(
        claimed_order.command_id
    )
    support_outbox_before = await integration_repo.get_outbox_message(
        claimed_support.command_id
    )
    order_status_events_before = await _status_events(
        pool, queued_operation.operation_id
    )
    case_events_before = await _case_events(case_repo, case_scope, support_case.case_id)
    order_attempts_before = await _attempt_history_details(pool, order_inbox.inbox_id)
    support_attempts_before = await _attempt_history_details(
        pool, support_inbox.inbox_id
    )
    order_key = f"provider-webhook:{order_inbox.inbox_id}:operation-status"
    support_key = f"provider-webhook:{support_inbox.inbox_id}:case-provider-update"
    assert order_before is not None and order_before.status == "submitted"
    assert case_before is not None and case_before.case_type == "delivery_investigation"
    assert order_outbox_before is not None and order_outbox_before.status == "published"
    assert (
        support_outbox_before is not None
        and support_outbox_before.status == "published"
    )
    assert order_outbox_before.aggregate_type == "order_operation"
    assert support_outbox_before.aggregate_type == "support_case"
    assert await _order_events_for_idempotency_key(pool, order_key) == []
    assert await _case_events_for_idempotency_key(pool, support_key) == []
    _assert_recovered_attempt(order_attempts_before)
    _assert_recovered_attempt(support_attempts_before)
    order_outbox_snapshot = order_outbox_before.model_dump(mode="json")
    support_outbox_snapshot = support_outbox_before.model_dump(mode="json")
    case_snapshot = case_before.model_dump(mode="json")

    worker = InboxProcessingWorker(
        repository=integration_repo,
        finalizer=PostgresInboxFinalizer(pool),
        worker_id="inbox-worker-e2e",
        batch_size=2,
        lease_seconds=60.0,
    )
    result = await worker.run_once()

    assert result == InboxWorkerRunResult(claimed=2, applied=2)
    safe_summary = repr(result)
    for business_value in (
        str(order_inbox.inbox_id),
        str(support_inbox.inbox_id),
        str(claimed_order.command_id),
        str(claimed_support.command_id),
        order_inbox.payload.provider_reference,
        support_inbox.payload.provider_reference,
        tenant_id,
        order_before.customer_id,
        order_before.order_id,
    ):
        assert business_value not in safe_summary

    order_after = await operation_repo.get_operation(
        order_scope, queued_operation.operation_id
    )
    case_after = await case_repo.get_case(case_scope, support_case.case_id)
    order_inbox_after = await integration_repo.get_inbox_message(order_inbox.inbox_id)
    support_inbox_after = await integration_repo.get_inbox_message(
        support_inbox.inbox_id
    )
    order_outbox_after = await integration_repo.get_outbox_message(
        claimed_order.command_id
    )
    support_outbox_after = await integration_repo.get_outbox_message(
        claimed_support.command_id
    )
    order_status_events_after = await _status_events(
        pool, queued_operation.operation_id
    )
    case_events_after = await _case_events(case_repo, case_scope, support_case.case_id)
    order_attempts_after = await _attempt_history_details(pool, order_inbox.inbox_id)
    support_attempts_after = await _attempt_history_details(
        pool, support_inbox.inbox_id
    )
    order_callback_events = await _order_events_for_idempotency_key(pool, order_key)
    support_callback_events = await _case_events_for_idempotency_key(pool, support_key)

    assert order_after is not None
    assert order_after.status == "processing"
    assert order_after.version == order_before.version + 1
    assert order_after.provider_reference == order_inbox.payload.provider_reference
    assert len(order_status_events_after) == len(order_status_events_before) + 1
    assert len(order_callback_events) == 1
    assert order_callback_events[0][1] == order_key
    _assert_processed_inbox(order_inbox_after)
    _assert_attempt_history(order_attempts_after, worker_id="inbox-worker-e2e")
    assert order_outbox_after is not None
    assert order_outbox_after.model_dump(mode="json") == order_outbox_snapshot

    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert len(case_events_after.items) == len(case_events_before.items) + 1
    assert len(support_callback_events) == 1
    assert support_callback_events[0][1] == support_key
    provider_updates = [
        event
        for event in case_events_after.items
        if event.idempotency_key == support_key
    ]
    assert len(provider_updates) == 1
    provider_update = provider_updates[0]
    assert provider_update.event_type == "provider_update"
    assert provider_update.provider_command_id == claimed_support.command_id
    assert provider_update.provider_command_status == "completed"
    assert (
        provider_update.provider_reference == support_inbox.payload.provider_reference
    )
    assert provider_update.tenant_id == case_before.tenant_id
    assert provider_update.customer_id == case_before.customer_id
    _assert_processed_inbox(support_inbox_after)
    _assert_attempt_history(support_attempts_after, worker_id="inbox-worker-e2e")
    assert support_outbox_after is not None
    assert support_outbox_after.model_dump(mode="json") == support_outbox_snapshot

    second_result = await worker.run_once()
    assert second_result == InboxWorkerRunResult()
    assert (
        await _attempt_history_details(pool, order_inbox.inbox_id)
        == order_attempts_after
    )
    assert (
        await _attempt_history_details(pool, support_inbox.inbox_id)
        == support_attempts_after
    )
    assert (
        await _order_events_for_idempotency_key(pool, order_key)
        == order_callback_events
    )
    assert (
        await _case_events_for_idempotency_key(pool, support_key)
        == support_callback_events
    )
    order_after_second = await operation_repo.get_operation(
        order_scope, queued_operation.operation_id
    )
    case_after_second = await case_repo.get_case(case_scope, support_case.case_id)
    assert order_after_second == order_after
    assert case_after_second == case_after
    assert (
        await integration_repo.get_outbox_message(claimed_order.command_id)
    ) == order_outbox_after
    assert (
        await integration_repo.get_outbox_message(claimed_support.command_id)
    ) == support_outbox_after
