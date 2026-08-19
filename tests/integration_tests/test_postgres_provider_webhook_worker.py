"""Cross-component PostgreSQL evidence for signed provider callbacks."""

import asyncio
import hashlib
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agent.cases.postgres_repository import PostgresCaseRepository
from agent.integrations.finalization import PostgresOutboxFinalizer
from agent.integrations.inbox_postgres_finalizer import PostgresInboxFinalizer
from agent.integrations.inbox_worker import InboxProcessingWorker, InboxWorkerRunResult
from agent.integrations.models import ProviderCommandResult
from agent.integrations.postgres_repository import PostgresIntegrationRepository
from agent.operations.postgres_repository import PostgresOrderOperationRepository
from tests.integration_tests.test_postgres_provider_inbox import (
    _events_for_idempotency_key as _order_events_for_key,
)
from tests.integration_tests.test_postgres_provider_inbox import _status_events
from tests.integration_tests.test_postgres_provider_messaging import (
    _case_created_event,
    _delivery_case,
    _delivery_envelope,
    _envelope,
    _operation,
    _queued_event,
    _scope,
)
from tests.integration_tests.test_postgres_provider_messaging import (
    postgres_context as _postgres_context,
)
from tests.integration_tests.test_postgres_provider_support_case_inbox import (
    _attempt_history_details,
    _case_events,
)
from tests.integration_tests.test_postgres_provider_support_case_inbox import (
    _events_for_idempotency_key as _case_events_for_key,
)
from tests.integration_tests.test_postgres_provider_webhook import (
    _app,
    _attempt_count,
    _headers,
    _inbox_rows,
    _payload,
    _post,
)

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]

_CONNECTION_ID = "conn-1"
_WORKER_ID = "webhook-inbox-e2e"


@pytest.fixture
def anyio_backend() -> str | tuple[str, dict[str, object]]:
    if os.name == "nt":
        return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}
    return "asyncio"


@pytest.fixture
async def postgres_context():
    """Reuse the disposable provider-messaging fixture and its tenant cleanup."""
    async for context in _postgres_context.__wrapped__():
        yield context


async def _publish_order_outbox(pool, tenant_id: str):
    scope = _scope(tenant_id)
    operation_repository = PostgresOrderOperationRepository(pool)
    integration_repository = PostgresIntegrationRepository(pool)
    created = _operation(tenant_id=tenant_id, source_message_id=f"http-order-{uuid4()}")
    await operation_repository.create_operation_with_events(
        scope, operation=created, events=()
    )
    queued = created.model_copy(update={"status": "queued", "version": 2})
    command = _envelope(queued)
    await operation_repository.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=1,
    )
    claimed = (
        await integration_repository.claim_due_outbox(
            worker_id="webhook-outbox-order", batch_size=1, lease_seconds=60.0
        )
    )[0]
    await PostgresOutboxFinalizer(pool).accepted(
        claimed=claimed,
        result=ProviderCommandResult(
            command_id=command.command_id,
            status="accepted",
            provider_reference="outbox-reference",
            received_at=datetime.now(UTC),
        ),
    )
    return scope, operation_repository, integration_repository, queued, command


async def _publish_support_case_outbox(pool, tenant_id: str):
    scope = _scope(tenant_id)
    case_repository = PostgresCaseRepository(pool)
    integration_repository = PostgresIntegrationRepository(pool)
    case = _delivery_case(
        tenant_id=tenant_id,
        order_id="ORD-10010",
        source_message_id=f"http-delivery-{uuid4()}",
    )
    command = _delivery_envelope(case)
    await case_repository.create_case_with_event_and_command(
        scope,
        case=case,
        event=_case_created_event(case),
        command=command,
    )
    claimed = (
        await integration_repository.claim_due_outbox(
            worker_id="webhook-outbox-case", batch_size=1, lease_seconds=60.0
        )
    )[0]
    await PostgresOutboxFinalizer(pool).accepted(
        claimed=claimed,
        result=ProviderCommandResult(
            command_id=command.command_id,
            status="accepted",
            provider_reference="outbox-reference",
            received_at=datetime.now(UTC),
        ),
    )
    return scope, case_repository, integration_repository, case, command


def _assert_processed_inbox(inbox) -> None:
    assert inbox is not None
    assert inbox.status == "processed"
    assert inbox.processed_at is not None
    assert inbox.lease_id is None
    assert inbox.lease_owner is None
    assert inbox.lease_expires_at is None
    assert inbox.processing_attempts == 1


def _assert_single_processed_attempt(history, *, worker_id: str) -> None:
    assert len(history) == 1
    attempt = history[0]
    assert attempt[2] == 1
    assert attempt[4] == worker_id
    assert attempt[5] is not None
    assert attempt[6:] == ("processed", None, None)


async def test_signed_http_callbacks_are_durably_processed_by_one_worker_batch(
    postgres_context,
) -> None:
    """HTTP ingress, Inbox fencing, and both aggregate finalizers compose safely."""
    pool, tenant_id = postgres_context
    (
        order_scope,
        operation_repository,
        integration_repository,
        queued_operation,
        order_command,
    ) = await _publish_order_outbox(pool, tenant_id)
    (
        case_scope,
        case_repository,
        _case_integration_repository,
        support_case,
        support_command,
    ) = await _publish_support_case_outbox(pool, tenant_id)
    app = _app(pool, tenant_id=tenant_id, connection_id=_CONNECTION_ID)
    timestamp = datetime.now(UTC).replace(microsecond=0)
    order_event_id = f"http-order-event-{uuid4()}"
    support_event_id = f"http-case-event-{uuid4()}"
    order_raw_body = _payload(
        tenant_id=tenant_id,
        event_id=order_event_id,
        command_id=order_command.command_id,
        aggregate_id=queued_operation.operation_id,
        timestamp=timestamp,
        connection_id=_CONNECTION_ID,
        aggregate_type="order_operation",
        command_status="processing",
        order_id=queued_operation.order_id,
        provider_reference="outbox-reference",
    )
    support_raw_body = _payload(
        tenant_id=tenant_id,
        event_id=support_event_id,
        command_id=support_command.command_id,
        aggregate_id=support_case.case_id,
        timestamp=timestamp,
        connection_id=_CONNECTION_ID,
        aggregate_type="support_case",
        command_status="completed",
        order_id=support_case.order_id,
        provider_reference="callback-reference",
    )
    order_headers = _headers(
        event_id=order_event_id, timestamp=timestamp, raw_body=order_raw_body
    )
    support_headers = _headers(
        event_id=support_event_id, timestamp=timestamp, raw_body=support_raw_body
    )

    order_before = await operation_repository.get_operation(
        order_scope, queued_operation.operation_id
    )
    case_before = await case_repository.get_case(case_scope, support_case.case_id)
    order_outbox_before = await integration_repository.get_outbox_message(
        order_command.command_id
    )
    support_outbox_before = await integration_repository.get_outbox_message(
        support_command.command_id
    )
    order_events_before = await _status_events(pool, queued_operation.operation_id)
    case_events_before = await _case_events(
        case_repository, case_scope, support_case.case_id
    )
    assert order_before is not None and order_before.status == "submitted"
    assert case_before is not None and case_before.case_type == "delivery_investigation"
    assert order_outbox_before is not None and order_outbox_before.status == "published"
    assert (
        support_outbox_before is not None
        and support_outbox_before.status == "published"
    )
    order_outbox_snapshot = order_outbox_before.model_dump(mode="json")
    support_outbox_snapshot = support_outbox_before.model_dump(mode="json")
    case_snapshot = case_before.model_dump(mode="json")

    order_response = await _post(
        app,
        connection_id=_CONNECTION_ID,
        raw_body=order_raw_body,
        headers=order_headers,
    )
    support_response = await _post(
        app,
        connection_id=_CONNECTION_ID,
        raw_body=support_raw_body,
        headers=support_headers,
    )

    assert (order_response.status_code, support_response.status_code) == (202, 202)
    inbox_rows_before_worker = await _inbox_rows(pool, tenant_id=tenant_id)
    assert len(inbox_rows_before_worker) == 2
    inbox_by_command = {row[4]: row for row in inbox_rows_before_worker}
    order_inbox_row = inbox_by_command[order_command.command_id]
    support_inbox_row = inbox_by_command[support_command.command_id]
    for row, event_id, command, aggregate_type, aggregate_id, raw_body in (
        (
            order_inbox_row,
            order_event_id,
            order_command,
            "order_operation",
            queued_operation.operation_id,
            order_raw_body,
        ),
        (
            support_inbox_row,
            support_event_id,
            support_command,
            "support_case",
            support_case.case_id,
            support_raw_body,
        ),
    ):
        assert row[1:7] == (
            _CONNECTION_ID,
            event_id,
            tenant_id,
            command.command_id,
            aggregate_type,
            aggregate_id,
        )
        assert row[8] == hashlib.sha256(raw_body).hexdigest()
        assert row[9:] == ("received", 0)
        assert await _attempt_count(pool, inbox_id=row[0]) == 0

    assert (
        await operation_repository.get_operation(
            order_scope, queued_operation.operation_id
        )
        == order_before
    )
    assert (
        await case_repository.get_case(case_scope, support_case.case_id) == case_before
    )
    assert (
        await _status_events(pool, queued_operation.operation_id) == order_events_before
    )
    assert (
        await _case_events(case_repository, case_scope, support_case.case_id)
    ).items == case_events_before.items
    assert (
        await integration_repository.get_outbox_message(order_command.command_id)
    ).model_dump(mode="json") == order_outbox_snapshot
    assert (
        await integration_repository.get_outbox_message(support_command.command_id)
    ).model_dump(mode="json") == support_outbox_snapshot

    worker = InboxProcessingWorker(
        repository=integration_repository,
        finalizer=PostgresInboxFinalizer(pool),
        worker_id=_WORKER_ID,
        batch_size=2,
        lease_seconds=60.0,
    )
    worker_result = await worker.run_once()

    assert worker_result == InboxWorkerRunResult(claimed=2, applied=2)
    safe_summary = repr(worker_result)
    for value in (
        tenant_id,
        order_before.customer_id,
        order_before.order_id,
        str(order_command.command_id),
        str(support_command.command_id),
        str(order_inbox_row[0]),
        str(support_inbox_row[0]),
        "outbox-reference",
        "callback-reference",
        order_headers["x-provider-signature"],
    ):
        assert value not in safe_summary

    order_inbox_id = order_inbox_row[0]
    support_inbox_id = support_inbox_row[0]
    order_key = f"provider-webhook:{order_inbox_id}:operation-status"
    support_key = f"provider-webhook:{support_inbox_id}:case-provider-update"
    order_after = await operation_repository.get_operation(
        order_scope, queued_operation.operation_id
    )
    case_after = await case_repository.get_case(case_scope, support_case.case_id)
    order_inbox_after = await integration_repository.get_inbox_message(order_inbox_id)
    support_inbox_after = await integration_repository.get_inbox_message(
        support_inbox_id
    )
    order_events_after = await _status_events(pool, queued_operation.operation_id)
    case_events_after = await _case_events(
        case_repository, case_scope, support_case.case_id
    )
    order_attempts_after = await _attempt_history_details(pool, order_inbox_id)
    support_attempts_after = await _attempt_history_details(pool, support_inbox_id)
    order_callback_events = await _order_events_for_key(pool, order_key)
    support_callback_events = await _case_events_for_key(pool, support_key)

    assert order_after is not None
    assert order_after.status == "processing"
    assert order_after.version == order_before.version + 1
    assert order_after.provider_reference == "outbox-reference"
    assert len(order_events_after) == len(order_events_before) + 1
    assert len(order_callback_events) == 1
    _assert_processed_inbox(order_inbox_after)
    _assert_single_processed_attempt(order_attempts_after, worker_id=_WORKER_ID)
    assert (
        await integration_repository.get_outbox_message(order_command.command_id)
    ).model_dump(mode="json") == order_outbox_snapshot

    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert len(case_events_after.items) == len(case_events_before.items) + 1
    assert len(support_callback_events) == 1
    provider_updates = [
        event
        for event in case_events_after.items
        if event.idempotency_key == support_key
    ]
    assert len(provider_updates) == 1
    provider_update = provider_updates[0]
    assert provider_update.event_type == "provider_update"
    assert provider_update.provider_command_id == support_command.command_id
    assert provider_update.provider_command_status == "completed"
    assert provider_update.provider_reference == "callback-reference"
    assert provider_update.tenant_id == case_before.tenant_id
    assert provider_update.customer_id == case_before.customer_id
    _assert_processed_inbox(support_inbox_after)
    _assert_single_processed_attempt(support_attempts_after, worker_id=_WORKER_ID)
    assert (
        await integration_repository.get_outbox_message(support_command.command_id)
    ).model_dump(mode="json") == support_outbox_snapshot

    processed_inbox_snapshot = await _inbox_rows(pool, tenant_id=tenant_id)
    order_replay = await _post(
        app,
        connection_id=_CONNECTION_ID,
        raw_body=order_raw_body,
        headers=order_headers,
    )
    support_replay = await _post(
        app,
        connection_id=_CONNECTION_ID,
        raw_body=support_raw_body,
        headers=support_headers,
    )

    assert (order_replay.status_code, support_replay.status_code) == (202, 202)
    assert await _inbox_rows(pool, tenant_id=tenant_id) == processed_inbox_snapshot
    assert await worker.run_once() == InboxWorkerRunResult()
    assert (
        await operation_repository.get_operation(
            order_scope, queued_operation.operation_id
        )
        == order_after
    )
    assert (
        await case_repository.get_case(case_scope, support_case.case_id) == case_after
    )
    assert (
        await _status_events(pool, queued_operation.operation_id) == order_events_after
    )
    assert (
        await _case_events(case_repository, case_scope, support_case.case_id)
    ).items == case_events_after.items
    assert await _attempt_history_details(pool, order_inbox_id) == order_attempts_after
    assert (
        await _attempt_history_details(pool, support_inbox_id) == support_attempts_after
    )
    assert await _order_events_for_key(pool, order_key) == order_callback_events
    assert await _case_events_for_key(pool, support_key) == support_callback_events
