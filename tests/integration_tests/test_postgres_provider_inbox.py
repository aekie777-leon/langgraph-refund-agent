"""PostgreSQL contract coverage for fenced order-operation Inbox finalization."""

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent.integrations.finalization import PostgresOutboxFinalizer
from agent.integrations.inbox_finalizer import InboxFinalizationResult
from agent.integrations.inbox_postgres_finalizer import PostgresInboxFinalizer
from agent.integrations.models import (
    DeliveryInvestigationCommandPayload,
    ProviderCommandResult,
    ProviderWebhookEventData,
)
from agent.integrations.postgres_repository import PostgresIntegrationRepository
from agent.integrations.repository import (
    IntegrationPersistenceError,
    LeaseConflictError,
)
from agent.operations.models import OrderOperationEvent
from agent.operations.postgres_repository import (
    _EVENT_COLUMNS,
    _OPERATION_COLUMNS,
    PostgresOrderOperationRepository,
    _event_values,
    _operation_from_row,
)
from tests.integration_tests.test_postgres_provider_messaging import (
    _envelope,
    _operation,
    _queued_event,
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
async def postgres_context():
    """Reuse the disposable Step 2 messaging database fixture and cleanup."""
    async for context in _postgres_context.__wrapped__():
        yield context


async def test_order_operation_finalizer_requires_a_claimed_inbox(
    postgres_context,
) -> None:
    """The public finalizer is present and keeps scope to a claimed Inbox row."""
    pool, _ = postgres_context
    assert isinstance(PostgresInboxFinalizer(pool), PostgresInboxFinalizer)


async def _claimed_inbox(
    pool,
    tenant_id: str,
    status: str,
    *,
    finalize_outbox: bool = True,
):
    """Build one claimed Inbox over the real queued-to-published Outbox path."""
    scope = _scope(tenant_id)
    operation_repo = PostgresOrderOperationRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    created = _operation(tenant_id=tenant_id, source_message_id=f"source-{uuid4()}")
    await operation_repo.create_operation_with_events(
        scope, operation=created, events=()
    )
    queued = created.model_copy(update={"status": "queued", "version": 2})
    command = _envelope(queued)
    await operation_repo.queue_operation_with_events_and_command(
        scope,
        operation=queued,
        events=(_queued_event(queued),),
        command=command,
        expected_version=1,
    )
    if finalize_outbox:
        claimed_outbox = (
            await integration_repo.claim_due_outbox(
                worker_id="outbox-worker", batch_size=1, lease_seconds=60
            )
        )[0]
        await PostgresOutboxFinalizer(pool).accepted(
            claimed=claimed_outbox,
            result=ProviderCommandResult(
                command_id=command.command_id,
                status="accepted",
                provider_reference="outbox-reference",
                received_at=datetime.now(UTC),
            ),
        )
    event = ProviderWebhookEventData(
        command_id=command.command_id,
        aggregate_type="order_operation",
        aggregate_id=queued.operation_id,
        command_status=status,
        provider_reference="outbox-reference",
        order_id=queued.order_id,
        occurred_at=datetime.now(UTC),
    )
    inbox = await integration_repo.receive_inbox_idempotently(
        inbox_id=uuid4(),
        provider_connection_id="conn-1",
        event_id=f"event-{uuid4()}",
        tenant_id=tenant_id,
        event=event,
        raw_body_sha256=hashlib.sha256(b"inbox").hexdigest(),
        received_at=datetime.now(UTC),
    )
    claimed = (
        await integration_repo.claim_due_inbox(
            worker_id="inbox-worker", batch_size=1, lease_seconds=60
        )
    )[0]
    return scope, operation_repo, integration_repo, queued, inbox, claimed


async def _claim_webhook(
    integration_repo,
    *,
    tenant_id: str,
    command_id,
    operation,
    command_status: str,
    provider_reference: str = "outbox-reference",
    aggregate_id=None,
    order_id: str | None = None,
):
    """Persist and claim a distinct, valid callback for an existing command."""
    event_id = f"event-{uuid4()}"
    aggregate_id = aggregate_id or operation.operation_id
    order_id = order_id or operation.order_id
    event = ProviderWebhookEventData(
        command_id=command_id,
        aggregate_type="order_operation",
        aggregate_id=aggregate_id,
        command_status=command_status,
        provider_reference=provider_reference,
        order_id=order_id,
        occurred_at=datetime.now(UTC),
    )
    inbox = await integration_repo.receive_inbox_idempotently(
        inbox_id=uuid4(),
        provider_connection_id="conn-1",
        event_id=event_id,
        tenant_id=tenant_id,
        event=event,
        raw_body_sha256=hashlib.sha256(event_id.encode()).hexdigest(),
        received_at=datetime.now(UTC),
    )
    claimed = await integration_repo.claim_due_inbox(
        worker_id="inbox-worker", batch_size=1, lease_seconds=60
    )
    assert len(claimed) == 1
    return inbox, claimed[0]


async def _attempt_details(pool, attempt_id):
    """Read persisted completion evidence for one Inbox processing attempt."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT outcome, safe_error_code, safe_error_message "
            "FROM integration.inbox_processing_attempts WHERE attempt_id=%s",
            (attempt_id,),
        )
        return await cursor.fetchone()


async def _attempt_fencing_details(pool, attempt_id):
    """Read all immutable fields that bind an Inbox attempt to its worker lease."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT attempt_id, inbox_id, attempt_number, lease_id, worker_id, "
            "finished_at, outcome, safe_error_code, safe_error_message "
            "FROM integration.inbox_processing_attempts WHERE attempt_id=%s",
            (attempt_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return row


async def _inbox_attempt_history(pool, inbox_id):
    """Read every persisted attempt for one Inbox in lifecycle order."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT attempt_id, inbox_id, attempt_number, lease_id, worker_id, "
            "finished_at, outcome, safe_error_code, safe_error_message "
            "FROM integration.inbox_processing_attempts WHERE inbox_id=%s "
            "ORDER BY attempt_number",
            (inbox_id,),
        )
        return await cursor.fetchall()


async def _status_events(pool, operation_id):
    """Return status event evidence in deterministic insertion order."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT previous_status, current_status, provider_reference "
            "FROM case_management.order_operation_events "
            "WHERE operation_id=%s AND event_type='status_changed' "
            "ORDER BY created_at, event_id",
            (operation_id,),
        )
        return await cursor.fetchall()


async def _events_for_idempotency_key(pool, idempotency_key):
    """Return stable evidence for the unique key used by one domain event."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT event_id, idempotency_key "
            "FROM case_management.order_operation_events "
            "WHERE idempotency_key=%s",
            (idempotency_key,),
        )
        return await cursor.fetchall()


async def _get_operation_unscoped(pool, operation_id):
    """Read and validate one operation even after a tenant test mutation."""
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT {_OPERATION_COLUMNS} FROM case_management.order_operations "
                "WHERE operation_id=%s",
                (operation_id,),
            )
            row = await cursor.fetchone()
    return None if row is None else _operation_from_row(row)


async def _attempt_finished_at(pool, attempt_id):
    """Return completion evidence that is intentionally absent from the summary."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT finished_at FROM integration.inbox_processing_attempts "
            "WHERE attempt_id=%s",
            (attempt_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def _mutate_operation_association(
    pool, *, operation_id, tenant_id: str, variant: str
) -> None:
    """Change one association field while preserving a valid OrderOperation row."""
    async with pool.connection() as connection:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                if variant == "tenant_id":
                    await cursor.execute(
                        "UPDATE case_management.order_operations SET tenant_id=%s "
                        "WHERE operation_id=%s",
                        (f"{tenant_id}-other", operation_id),
                    )
                elif variant == "customer_id":
                    await cursor.execute(
                        "UPDATE case_management.order_operations SET customer_id='customer-b' "
                        "WHERE operation_id=%s",
                        (operation_id,),
                    )
                elif variant == "order_id":
                    await cursor.execute(
                        "UPDATE case_management.order_operations SET order_id='ORD-99999' "
                        "WHERE operation_id=%s",
                        (operation_id,),
                    )
                elif variant == "operation_type":
                    await cursor.execute(
                        "UPDATE case_management.order_operations SET "
                        "operation_type='cancellation', "
                        "request_reason_code='ordered_by_mistake', "
                        "replacement_variant_id=NULL WHERE operation_id=%s",
                        (operation_id,),
                    )
                elif variant == "order_version":
                    await cursor.execute(
                        "UPDATE case_management.order_operations SET order_version=4 "
                        "WHERE operation_id=%s",
                        (operation_id,),
                    )
                elif variant == "source_message_id":
                    await cursor.execute(
                        "UPDATE case_management.order_operations SET source_message_id=%s "
                        "WHERE operation_id=%s",
                        (f"source-mismatch-{uuid4()}", operation_id),
                    )
                else:
                    raise ValueError(f"unsupported operation variant: {variant}")
                assert cursor.rowcount == 1


async def _mutate_outbox_association(
    pool, *, command_id, operation, variant: str
) -> None:
    """Create one valid canonical Outbox with a different trusted association."""
    async with pool.connection() as connection:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                if variant == "tenant_id":
                    await cursor.execute(
                        "UPDATE integration.outbox_messages SET tenant_id=%s "
                        "WHERE command_id=%s",
                        (f"{operation.tenant_id}-other", command_id),
                    )
                elif variant == "provider_connection_id":
                    await cursor.execute(
                        "UPDATE integration.outbox_messages "
                        "SET provider_connection_id='other-connection' "
                        "WHERE command_id=%s",
                        (command_id,),
                    )
                elif variant == "aggregate_id":
                    aggregate_id = uuid4()
                    await cursor.execute(
                        "UPDATE integration.outbox_messages "
                        "SET aggregate_id=%s, idempotency_key=%s WHERE command_id=%s",
                        (aggregate_id, f"order-operation:{aggregate_id}", command_id),
                    )
                elif variant == "payload_family":
                    aggregate_id = uuid4()
                    payload = DeliveryInvestigationCommandPayload(
                        order_id=operation.order_id,
                        issue_type="tracking_stalled",
                    )
                    await cursor.execute(
                        "UPDATE integration.outbox_messages SET "
                        "command_type='delivery_investigation', "
                        "aggregate_type='support_case', aggregate_id=%s, "
                        "expected_order_version=NULL, idempotency_key=%s, payload=%s "
                        "WHERE command_id=%s",
                        (
                            aggregate_id,
                            f"delivery-investigation:{aggregate_id}",
                            Jsonb(payload.model_dump(mode="json")),
                            command_id,
                        ),
                    )
                else:
                    raise ValueError(f"unsupported association variant: {variant}")
                assert cursor.rowcount == 1


async def test_order_operation_inbox_published_accepted_becomes_submitted(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    result = await PostgresInboxFinalizer(pool).finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    assert result.action == "duplicate"
    assert stored is not None and stored.status == "submitted"
    assert persisted is not None and persisted.status == "processed"


async def test_order_operation_inbox_published_processing_becomes_processing(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        _integration_repo,
        queued,
        _inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "processing")
    result = await PostgresInboxFinalizer(pool).finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    stored = await operation_repo.get_operation(scope, queued.operation_id)
    assert result.action == "applied"
    assert stored is not None and stored.status == "processing"


async def test_order_operation_inbox_payload_association_mismatch_fails(
    postgres_context,
) -> None:
    """A corrupt persisted payload fails without changing the submitted operation."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "processing")
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                "UPDATE integration.inbox_messages "
                "SET payload=jsonb_set(payload, '{aggregate_id}', to_jsonb(%s::text)) "
                "WHERE inbox_id=%s",
                (str(uuid4()), inbox.inbox_id),
            )

    result = await PostgresInboxFinalizer(pool).finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    assert result.action == "failed"
    assert result.aggregate_type == "order_operation"
    assert result.safe_error_code == "inbox_payload_association_mismatch"
    assert stored is not None and stored.status == "submitted"
    assert persisted is not None and persisted.status == "failed"


async def test_order_operation_inbox_forged_claimed_payload_rolls_back(
    postgres_context,
) -> None:
    """The lease handle cannot replace the locked Inbox payload."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    forged = claimed.model_copy(
        update={
            "payload": claimed.payload.model_copy(
                update={"command_status": "processing"}
            )
        }
    )

    with pytest.raises(LeaseConflictError):
        await PostgresInboxFinalizer(pool).finalize_order_operation(
            claimed=forged,
            retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
        )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    assert stored is not None and stored.status == "submitted"
    assert persisted is not None and persisted.status == "processing"


async def test_order_operation_inbox_attempt_number_mismatch_rolls_back(
    postgres_context,
) -> None:
    """The locked attempt number must match persisted processing_attempts."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                "UPDATE integration.inbox_processing_attempts "
                "SET attempt_number=attempt_number + 1 WHERE attempt_id=%s",
                (claimed.attempt.attempt_id,),
            )

    with pytest.raises(LeaseConflictError):
        await PostgresInboxFinalizer(pool).finalize_order_operation(
            claimed=claimed,
            retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
        )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    assert stored is not None and stored.status == "submitted"
    assert persisted is not None and persisted.status == "processing"


async def test_order_operation_inbox_published_ignores_expired_retry_time(
    postgres_context,
) -> None:
    """A published Outbox is final even when the unused retry time is stale."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")

    result = await PostgresInboxFinalizer(pool).finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    assert result.action == "duplicate"
    assert stored is not None and stored.status == "submitted"
    assert persisted is not None and persisted.status == "processed"


async def test_order_operation_inbox_retry_rejects_expired_retry_time(
    postgres_context,
) -> None:
    """A nonfinal Outbox requires a future retry instant from the database clock."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted", finalize_outbox=False)

    with pytest.raises(ValueError, match="retry_available_at"):
        await PostgresInboxFinalizer(pool).finalize_order_operation(
            claimed=claimed, retry_available_at=datetime.now(UTC) - timedelta(seconds=1)
        )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    assert stored is not None and stored.status == "queued"
    assert persisted is not None and persisted.status == "processing"


async def test_order_operation_inbox_completed_applies_atomically(
    postgres_context,
) -> None:
    """A completed callback advances submitted once and completes its Inbox atomically."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        _inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    finalizer = PostgresInboxFinalizer(pool)
    await finalizer.finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    before = await operation_repo.get_operation(scope, queued.operation_id)
    assert before is not None and before.status == "submitted"
    events_before = await _status_events(pool, queued.operation_id)
    inbox, completed = await _claim_webhook(
        integration_repo,
        tenant_id=tenant_id,
        command_id=claimed.command_id,
        operation=queued,
        command_status="completed",
    )

    result = await finalizer.finalize_order_operation(
        claimed=completed,
        retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt = await _attempt_details(pool, completed.attempt.attempt_id)
    assert result.action == "applied"
    assert stored is not None
    assert stored.status == "completed"
    assert stored.version == before.version + 1
    assert stored.provider_reference == "outbox-reference"
    assert len(events_after) == len(events_before) + 1
    assert events_after[-1] == ("submitted", "completed", "outbox-reference")
    assert persisted is not None and persisted.status == "processed"
    assert attempt == ("processed", None, None)


@pytest.mark.parametrize("initial_status", ["accepted", "processing"])
async def test_order_operation_inbox_rejected_applies_atomically(
    postgres_context, initial_status: str
) -> None:
    """A rejected callback advances submitted or processing exactly once."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        _inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, initial_status)
    finalizer = PostgresInboxFinalizer(pool)
    await finalizer.finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    before = await operation_repo.get_operation(scope, queued.operation_id)
    assert before is not None and before.status in {"submitted", "processing"}
    events_before = await _status_events(pool, queued.operation_id)
    inbox, rejected = await _claim_webhook(
        integration_repo,
        tenant_id=tenant_id,
        command_id=claimed.command_id,
        operation=queued,
        command_status="rejected",
    )

    result = await finalizer.finalize_order_operation(
        claimed=rejected,
        retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt = await _attempt_details(pool, rejected.attempt.attempt_id)
    assert result.action == "applied"
    assert stored is not None and stored.status == "rejected"
    assert stored.version == before.version + 1
    assert len(events_after) == len(events_before) + 1
    assert events_after[-1] == (before.status, "rejected", "outbox-reference")
    assert persisted is not None and persisted.status == "processed"
    assert attempt == ("processed", None, None)


@pytest.mark.parametrize("terminal_status", ["completed", "rejected"])
async def test_order_operation_inbox_terminal_duplicate_is_processed(
    postgres_context, terminal_status: str
) -> None:
    """A fresh duplicate callback consumes its Inbox without another domain write."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        _inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, terminal_status)
    finalizer = PostgresInboxFinalizer(pool)
    await finalizer.finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    before = await operation_repo.get_operation(scope, queued.operation_id)
    assert before is not None and before.status == terminal_status
    events_before = await _status_events(pool, queued.operation_id)
    inbox, duplicate = await _claim_webhook(
        integration_repo,
        tenant_id=tenant_id,
        command_id=claimed.command_id,
        operation=queued,
        command_status=terminal_status,
    )

    result = await finalizer.finalize_order_operation(
        claimed=duplicate,
        retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt = await _attempt_details(pool, duplicate.attempt.attempt_id)
    assert result.action == "duplicate"
    assert stored is not None
    assert stored.status == terminal_status
    assert stored.version == before.version
    assert stored.provider_reference == before.provider_reference
    assert events_after == events_before
    assert persisted is not None and persisted.status == "processed"
    assert attempt == ("processed", None, None)


@pytest.mark.parametrize(
    ("initial_status", "stale_status"),
    [("processing", "accepted"), ("completed", "processing")],
)
async def test_order_operation_inbox_stale_callback_is_processed(
    postgres_context, initial_status: str, stale_status: str
) -> None:
    """A stale callback never moves an operation backwards or emits an event."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        _inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, initial_status)
    finalizer = PostgresInboxFinalizer(pool)
    await finalizer.finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    before = await operation_repo.get_operation(scope, queued.operation_id)
    assert before is not None
    events_before = await _status_events(pool, queued.operation_id)
    inbox, stale = await _claim_webhook(
        integration_repo,
        tenant_id=tenant_id,
        command_id=claimed.command_id,
        operation=queued,
        command_status=stale_status,
    )

    result = await finalizer.finalize_order_operation(
        claimed=stale,
        retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt = await _attempt_details(pool, stale.attempt.attempt_id)
    assert result.action == "stale"
    assert stored is not None and stored.status == before.status
    assert stored.version == before.version
    assert events_after == events_before
    assert persisted is not None and persisted.status == "processed"
    assert attempt == ("processed", None, None)


async def test_order_operation_inbox_reference_conflict_fails_atomically(
    postgres_context,
) -> None:
    """A different nonempty provider reference fails safely without domain writes."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        _inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    finalizer = PostgresInboxFinalizer(pool)
    await finalizer.finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    before = await operation_repo.get_operation(scope, queued.operation_id)
    assert before is not None and before.provider_reference == "outbox-reference"
    events_before = await _status_events(pool, queued.operation_id)
    inbox, conflict = await _claim_webhook(
        integration_repo,
        tenant_id=tenant_id,
        command_id=claimed.command_id,
        operation=queued,
        command_status="completed",
        provider_reference="reference-B-raw-body-signature-secret",
    )

    result = await finalizer.finalize_order_operation(
        claimed=conflict,
        retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt = await _attempt_details(pool, conflict.attempt.attempt_id)
    assert result.action == "failed"
    assert result.safe_error_code == "provider_reference_conflict"
    assert stored is not None
    assert stored.status == before.status
    assert stored.version == before.version
    assert stored.provider_reference == before.provider_reference
    assert events_after == events_before
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.last_error_code == "provider_reference_conflict"
    assert attempt == (
        "terminal_failure",
        "provider_reference_conflict",
        "Provider webhook could not be applied.",
    )
    assert "reference-B" not in persisted.last_error_message
    assert "reference-B" not in attempt[2]


async def test_order_operation_inbox_outbox_not_found_fails_atomically(
    postgres_context,
) -> None:
    """A callback for no local command fails without changing its operation."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        _inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    finalizer = PostgresInboxFinalizer(pool)
    await finalizer.finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    before = await operation_repo.get_operation(scope, queued.operation_id)
    assert before is not None
    events_before = await _status_events(pool, queued.operation_id)
    missing_command_id = uuid4()
    assert await integration_repo.get_outbox_message(missing_command_id) is None
    inbox, missing = await _claim_webhook(
        integration_repo,
        tenant_id=tenant_id,
        command_id=missing_command_id,
        operation=queued,
        command_status="completed",
    )

    result = await finalizer.finalize_order_operation(
        claimed=missing,
        retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt = await _attempt_details(pool, missing.attempt.attempt_id)
    assert result.action == "failed"
    assert result.aggregate_type == "order_operation"
    assert result.safe_error_code == "outbox_not_found"
    assert await integration_repo.get_outbox_message(missing_command_id) is None
    assert stored is not None
    assert stored.status == before.status
    assert stored.version == before.version
    assert stored.provider_reference == before.provider_reference
    assert events_after == events_before
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.last_error_code == "outbox_not_found"
    assert persisted.last_error_message == "Provider webhook could not be applied."
    assert attempt == (
        "terminal_failure",
        "outbox_not_found",
        "Provider webhook could not be applied.",
    )
    assert str(missing_command_id) not in persisted.last_error_message
    assert str(missing_command_id) not in attempt[2]


@pytest.mark.parametrize(
    "variant",
    ["tenant_id", "provider_connection_id", "aggregate_id", "payload_family"],
)
async def test_order_operation_inbox_outbox_association_mismatch_fails_atomically(
    postgres_context, variant: str
) -> None:
    """A valid canonical Outbox for another trusted association is rejected."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    before = await operation_repo.get_operation(scope, queued.operation_id)
    assert before is not None
    events_before = await _status_events(pool, queued.operation_id)
    await _mutate_outbox_association(
        pool,
        command_id=claimed.command_id,
        operation=queued,
        variant=variant,
    )
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert outbox_before is not None
    outbox_before.to_envelope()
    outbox_snapshot = outbox_before.model_dump(mode="json")

    result = await PostgresInboxFinalizer(pool).finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt = await _attempt_details(pool, claimed.attempt.attempt_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    assert result.action == "failed"
    assert result.aggregate_type == "order_operation"
    assert result.safe_error_code == "outbox_association_mismatch"
    assert stored is not None
    assert stored.status == before.status
    assert stored.version == before.version
    assert stored.provider_reference == before.provider_reference
    assert events_after == events_before
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.last_error_code == "outbox_association_mismatch"
    assert persisted.last_error_message == "Provider webhook could not be applied."
    assert attempt == (
        "terminal_failure",
        "outbox_association_mismatch",
        "Provider webhook could not be applied.",
    )
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert variant not in persisted.last_error_message
    assert variant not in attempt[2]


async def test_order_operation_inbox_operation_not_found_fails_atomically(
    postgres_context,
) -> None:
    """A canonical Outbox can refer to an aggregate with no local operation."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        _inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    finalizer = PostgresInboxFinalizer(pool)
    await finalizer.finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    original = await operation_repo.get_operation(scope, queued.operation_id)
    assert original is not None and original.status == "submitted"
    original_snapshot = original.model_dump(mode="json")
    events_before = await _status_events(pool, queued.operation_id)
    missing_operation_id = uuid4()
    async with pool.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                "UPDATE integration.outbox_messages SET aggregate_id=%s, "
                "idempotency_key=%s WHERE command_id=%s",
                (
                    missing_operation_id,
                    f"order-operation:{missing_operation_id}",
                    claimed.command_id,
                ),
            )
            assert cursor.rowcount == 1
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert outbox_before is not None
    outbox_before.to_envelope()
    outbox_snapshot = outbox_before.model_dump(mode="json")
    assert await _get_operation_unscoped(pool, missing_operation_id) is None
    inbox, missing = await _claim_webhook(
        integration_repo,
        tenant_id=tenant_id,
        command_id=claimed.command_id,
        operation=queued,
        aggregate_id=missing_operation_id,
        command_status="completed",
    )

    result = await finalizer.finalize_order_operation(
        claimed=missing,
        retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt = await _attempt_details(pool, missing.attempt.attempt_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    assert result.action == "failed"
    assert result.safe_error_code == "operation_not_found"
    assert stored is not None and stored.model_dump(mode="json") == original_snapshot
    assert events_after == events_before
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.failed_at is not None
    assert persisted.lease_id is None and persisted.lease_owner is None
    assert persisted.last_error_code == "operation_not_found"
    assert persisted.last_error_message == "Provider webhook could not be applied."
    assert attempt == (
        "terminal_failure",
        "operation_not_found",
        "Provider webhook could not be applied.",
    )
    assert await _attempt_finished_at(pool, missing.attempt.attempt_id) is not None
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert str(missing_operation_id) not in persisted.last_error_message
    assert str(missing_operation_id) not in attempt[2]


@pytest.mark.parametrize(
    "variant",
    [
        "tenant_id",
        "customer_id",
        "order_id",
        "operation_type",
        "order_version",
        "source_message_id",
    ],
)
async def test_order_operation_inbox_order_association_mismatch_fails_atomically(
    postgres_context, variant: str
) -> None:
    """A valid but differently associated operation is never updated by a callback."""
    pool, tenant_id = postgres_context
    (
        _scope_value,
        _operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    await _mutate_operation_association(
        pool,
        operation_id=queued.operation_id,
        tenant_id=tenant_id,
        variant=variant,
    )
    operation_before = await _get_operation_unscoped(pool, queued.operation_id)
    assert operation_before is not None
    operation_snapshot = operation_before.model_dump(mode="json")
    events_before = await _status_events(pool, queued.operation_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert outbox_before is not None
    outbox_before.to_envelope()
    outbox_snapshot = outbox_before.model_dump(mode="json")

    result = await PostgresInboxFinalizer(pool).finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )

    operation_after = await _get_operation_unscoped(pool, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt = await _attempt_details(pool, claimed.attempt.attempt_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    assert result.action == "failed"
    assert result.safe_error_code == "order_association_mismatch"
    assert operation_after is not None
    assert operation_after.model_dump(mode="json") == operation_snapshot
    assert events_after == events_before
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.failed_at is not None
    assert persisted.lease_id is None and persisted.lease_owner is None
    assert persisted.last_error_code == "order_association_mismatch"
    assert persisted.last_error_message == "Provider webhook could not be applied."
    assert attempt == (
        "terminal_failure",
        "order_association_mismatch",
        "Provider webhook could not be applied.",
    )
    assert await _attempt_finished_at(pool, claimed.attempt.attempt_id) is not None
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert variant not in persisted.last_error_message
    assert variant not in attempt[2]


async def test_order_operation_inbox_event_order_id_mismatch_fails_atomically(
    postgres_context,
) -> None:
    """Webhook auxiliary order context must still agree with the local operation."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        _inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    finalizer = PostgresInboxFinalizer(pool)
    await finalizer.finalize_order_operation(
        claimed=claimed, retry_available_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    before = await operation_repo.get_operation(scope, queued.operation_id)
    assert before is not None
    operation_snapshot = before.model_dump(mode="json")
    events_before = await _status_events(pool, queued.operation_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert outbox_before is not None
    outbox_before.to_envelope()
    outbox_snapshot = outbox_before.model_dump(mode="json")
    inbox, mismatch = await _claim_webhook(
        integration_repo,
        tenant_id=tenant_id,
        command_id=claimed.command_id,
        operation=queued,
        command_status="completed",
        order_id="ORD-99999",
    )

    result = await finalizer.finalize_order_operation(
        claimed=mismatch,
        retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    stored = await operation_repo.get_operation(scope, queued.operation_id)
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt = await _attempt_details(pool, mismatch.attempt.attempt_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    assert result.action == "failed"
    assert result.safe_error_code == "order_association_mismatch"
    assert stored is not None and stored.model_dump(mode="json") == operation_snapshot
    assert events_after == events_before
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.failed_at is not None
    assert persisted.lease_id is None and persisted.lease_owner is None
    assert persisted.last_error_code == "order_association_mismatch"
    assert persisted.last_error_message == "Provider webhook could not be applied."
    assert attempt == (
        "terminal_failure",
        "order_association_mismatch",
        "Provider webhook could not be applied.",
    )
    assert await _attempt_finished_at(pool, mismatch.attempt.attempt_id) is not None
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert "ORD-99999" not in persisted.last_error_message
    assert "ORD-99999" not in attempt[2]


@pytest.mark.parametrize("fence_field", ["lease_id", "lease_owner", "attempt_id"])
async def test_order_operation_inbox_forged_fence_rolls_back(
    postgres_context, fence_field: str
) -> None:
    """A self-consistent but forged worker handle cannot finalize a real Inbox."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    persisted_before = await integration_repo.get_inbox_message(inbox.inbox_id)
    operation_before = await operation_repo.get_operation(scope, queued.operation_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert persisted_before is not None
    assert operation_before is not None
    assert outbox_before is not None
    inbox_snapshot = persisted_before.model_dump(mode="json")
    operation_snapshot = operation_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
    events_before = await _status_events(pool, queued.operation_id)
    attempt_before = await _attempt_fencing_details(pool, claimed.attempt.attempt_id)
    if fence_field == "lease_id":
        forged_value = uuid4()
        forged = claimed.model_copy(
            update={
                "lease_id": forged_value,
                "attempt": claimed.attempt.model_copy(
                    update={"lease_id": forged_value}
                ),
            }
        )
    elif fence_field == "lease_owner":
        forged_value = "forged-inbox-worker"
        forged = claimed.model_copy(
            update={
                "lease_owner": forged_value,
                "attempt": claimed.attempt.model_copy(
                    update={"worker_id": forged_value}
                ),
            }
        )
    else:
        forged_value = uuid4()
        forged = claimed.model_copy(
            update={
                "attempt": claimed.attempt.model_copy(
                    update={"attempt_id": forged_value}
                )
            }
        )

    with pytest.raises(LeaseConflictError) as raised:
        await PostgresInboxFinalizer(pool).finalize_order_operation(
            claimed=forged,
            retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
        )

    persisted_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    operation_after = await operation_repo.get_operation(scope, queued.operation_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt_after = await _attempt_fencing_details(pool, claimed.attempt.attempt_id)
    assert str(forged_value) not in str(raised.value)
    assert persisted_after is not None
    assert persisted_after.model_dump(mode="json") == inbox_snapshot
    assert persisted_after.status == "processing"
    assert operation_after is not None
    assert operation_after.model_dump(mode="json") == operation_snapshot
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert events_after == events_before
    assert attempt_after == attempt_before
    assert attempt_after[5:] == (None, None, None, None)


async def test_order_operation_inbox_expired_lease_rolls_back(
    postgres_context,
) -> None:
    """The Finalizer fences an expired lease instead of recovering or replacing it."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    async with pool.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                "UPDATE integration.inbox_messages SET "
                "lease_expires_at=clock_timestamp() - interval '1 second' "
                "WHERE inbox_id=%s",
                (inbox.inbox_id,),
            )
            assert cursor.rowcount == 1
    persisted_before = await integration_repo.get_inbox_message(inbox.inbox_id)
    operation_before = await operation_repo.get_operation(scope, queued.operation_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert persisted_before is not None
    assert operation_before is not None
    assert outbox_before is not None
    inbox_snapshot = persisted_before.model_dump(mode="json")
    operation_snapshot = operation_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
    events_before = await _status_events(pool, queued.operation_id)
    attempt_before = await _attempt_fencing_details(pool, claimed.attempt.attempt_id)

    with pytest.raises(LeaseConflictError):
        await PostgresInboxFinalizer(pool).finalize_order_operation(
            claimed=claimed,
            retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
        )

    persisted_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    operation_after = await operation_repo.get_operation(scope, queued.operation_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt_after = await _attempt_fencing_details(pool, claimed.attempt.attempt_id)
    assert persisted_after is not None
    assert persisted_after.model_dump(mode="json") == inbox_snapshot
    assert persisted_after.status == "processing"
    assert operation_after is not None
    assert operation_after.model_dump(mode="json") == operation_snapshot
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert events_after == events_before
    assert attempt_after == attempt_before
    assert attempt_after[5:] == (None, None, None, None)


async def test_order_operation_inbox_stale_claim_after_recovery_is_fenced(
    postgres_context,
) -> None:
    """Recovery fences a crashed worker so it cannot overwrite the next claim."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed_one,
    ) = await _claimed_inbox(pool, tenant_id, "accepted")
    operation_before = await operation_repo.get_operation(scope, queued.operation_id)
    outbox_before = await integration_repo.get_outbox_message(claimed_one.command_id)
    assert operation_before is not None
    assert outbox_before is not None
    operation_snapshot = operation_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
    events_before = await _status_events(pool, queued.operation_id)
    async with pool.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                "UPDATE integration.inbox_messages SET "
                "lease_expires_at=clock_timestamp() - interval '1 second' "
                "WHERE inbox_id=%s",
                (inbox.inbox_id,),
            )
            assert cursor.rowcount == 1
    assert await integration_repo.recover_expired_inbox_leases(batch_size=1) == 1
    first_attempt = await _attempt_fencing_details(pool, claimed_one.attempt.attempt_id)
    recovered = await integration_repo.get_inbox_message(inbox.inbox_id)
    assert recovered is not None
    assert recovered.status == "received"
    assert first_attempt[5] is not None
    assert first_attempt[6] == "lease_expired"
    claimed_two = (
        await integration_repo.claim_due_inbox(
            worker_id="inbox-worker-2", batch_size=1, lease_seconds=60
        )
    )[0]
    assert claimed_two.attempt.attempt_number == 2
    assert claimed_two.lease_id != claimed_one.lease_id
    assert claimed_two.lease_owner == "inbox-worker-2"

    with pytest.raises(LeaseConflictError):
        await PostgresInboxFinalizer(pool).finalize_order_operation(
            claimed=claimed_one,
            retry_available_at=datetime.now(UTC) + timedelta(seconds=1),
        )

    persisted_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    operation_after = await operation_repo.get_operation(scope, queued.operation_id)
    outbox_after = await integration_repo.get_outbox_message(claimed_one.command_id)
    events_after = await _status_events(pool, queued.operation_id)
    second_attempt = await _attempt_fencing_details(
        pool, claimed_two.attempt.attempt_id
    )
    first_attempt_after = await _attempt_fencing_details(
        pool, claimed_one.attempt.attempt_id
    )
    assert persisted_after is not None
    assert persisted_after.status == "processing"
    assert persisted_after.lease_id == claimed_two.lease_id
    assert persisted_after.lease_owner == claimed_two.lease_owner
    assert persisted_after.processing_attempts == claimed_two.processing_attempts
    assert operation_after is not None
    assert operation_after.model_dump(mode="json") == operation_snapshot
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert events_after == events_before
    assert first_attempt_after == first_attempt
    assert second_attempt[5:] == (None, None, None, None)


async def test_order_operation_inbox_fifth_outbox_wait_fails_terminally(
    postgres_context,
) -> None:
    """Five genuine waits for a non-terminal Outbox exhaust the Inbox budget."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(
        pool,
        tenant_id,
        "accepted",
        finalize_outbox=False,
    )
    operation_before = await operation_repo.get_operation(scope, queued.operation_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert operation_before is not None and operation_before.status == "queued"
    assert outbox_before is not None and outbox_before.status == "pending"
    operation_snapshot = operation_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
    events_before = await _status_events(pool, queued.operation_id)
    finalizer = PostgresInboxFinalizer(pool)
    current_claim = claimed

    for attempt_number in range(1, 5):
        result = await finalizer.finalize_order_operation(
            claimed=current_claim,
            retry_available_at=datetime.now(UTC) + timedelta(hours=1),
        )
        assert result.action == "retry_scheduled"
        assert result.aggregate_type == "order_operation"
        assert result.safe_error_code == "outbox_not_finalized"
        persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
        attempt = await _attempt_fencing_details(pool, current_claim.attempt.attempt_id)
        assert persisted is not None
        assert persisted.status == "received"
        assert persisted.processing_attempts == attempt_number
        assert persisted.lease_id is None
        assert persisted.lease_owner is None
        assert persisted.lease_expires_at is None
        assert persisted.last_error_code == "outbox_not_finalized"
        assert persisted.last_error_message == "Provider command is not finalized."
        assert attempt[2] == attempt_number
        assert attempt[3] == current_claim.lease_id
        assert attempt[4] == current_claim.lease_owner
        assert attempt[5] is not None
        assert attempt[6:] == (
            "retry_scheduled",
            "outbox_not_finalized",
            "Provider command is not finalized.",
        )
        async with pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    "UPDATE integration.inbox_messages "
                    "SET available_at=clock_timestamp() WHERE inbox_id=%s",
                    (inbox.inbox_id,),
                )
                assert cursor.rowcount == 1
        next_claims = await integration_repo.claim_due_inbox(
            worker_id="inbox-worker",
            batch_size=1,
            lease_seconds=60,
        )
        assert len(next_claims) == 1
        current_claim = next_claims[0]
        assert current_claim.inbox_id == inbox.inbox_id
        assert current_claim.processing_attempts == attempt_number + 1
        assert current_claim.attempt.attempt_number == attempt_number + 1
        assert current_claim.attempt.attempt_id != attempt[0]
        assert current_claim.lease_id != attempt[3]

    result = await finalizer.finalize_order_operation(
        claimed=current_claim,
        retry_available_at=datetime.now(UTC) + timedelta(hours=1),
    )
    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    operation_after = await operation_repo.get_operation(scope, queued.operation_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    events_after = await _status_events(pool, queued.operation_id)
    fifth_attempt = await _attempt_fencing_details(
        pool, current_claim.attempt.attempt_id
    )
    attempts = await _inbox_attempt_history(pool, inbox.inbox_id)
    assert result.action == "failed"
    assert result.aggregate_type == "order_operation"
    assert result.safe_error_code == "outbox_not_finalized_attempts_exhausted"
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.processing_attempts == 5
    assert persisted.failed_at is not None
    assert persisted.processed_at is None
    assert persisted.lease_id is None
    assert persisted.lease_owner is None
    assert persisted.lease_expires_at is None
    assert persisted.last_error_code == "outbox_not_finalized_attempts_exhausted"
    assert persisted.last_error_message == "Provider webhook could not be applied."
    assert fifth_attempt[2] == 5
    assert fifth_attempt[5] is not None
    assert fifth_attempt[6:] == (
        "terminal_failure",
        "outbox_not_finalized_attempts_exhausted",
        "Provider webhook could not be applied.",
    )
    assert len(attempts) == 5
    assert [attempt[2] for attempt in attempts] == [1, 2, 3, 4, 5]
    assert all(attempt[5] is not None for attempt in attempts)
    assert [attempt[6] for attempt in attempts] == [
        "retry_scheduled",
        "retry_scheduled",
        "retry_scheduled",
        "retry_scheduled",
        "terminal_failure",
    ]
    assert [attempt[7] for attempt in attempts] == [
        "outbox_not_finalized",
        "outbox_not_finalized",
        "outbox_not_finalized",
        "outbox_not_finalized",
        "outbox_not_finalized_attempts_exhausted",
    ]
    assert operation_after is not None
    assert operation_after.model_dump(mode="json") == operation_snapshot
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert events_after == events_before
    for safe_message in (persisted.last_error_message, fifth_attempt[8]):
        assert str(claimed.command_id) not in safe_message
        assert str(inbox.inbox_id) not in safe_message
        assert "outbox-reference" not in safe_message
    assert (
        await integration_repo.claim_due_inbox(
            worker_id="inbox-worker", batch_size=1, lease_seconds=60
        )
        == []
    )


async def test_order_operation_inbox_event_idempotency_conflict_rolls_back_atomically(
    postgres_context,
) -> None:
    """A real domain-event unique conflict rolls back the whole finalization."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "processing")
    operation_before = await operation_repo.get_operation(scope, queued.operation_id)
    inbox_before = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert operation_before is not None and operation_before.status == "submitted"
    assert inbox_before is not None and inbox_before.status == "processing"
    assert outbox_before is not None and outbox_before.status == "published"
    idempotency_key = f"provider-webhook:{inbox.inbox_id}:operation-status"
    conflict_event = OrderOperationEvent(
        event_id=uuid4(),
        idempotency_key=idempotency_key,
        operation_id=operation_before.operation_id,
        event_type="status_changed",
        previous_status="submitted",
        current_status="processing",
        provider_reference="outbox-reference",
        actor="system",
        customer_id=operation_before.customer_id,
        tenant_id=operation_before.tenant_id,
        created_at=datetime.now(UTC),
    )
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                f"INSERT INTO case_management.order_operation_events ({_EVENT_COLUMNS}) "
                f"VALUES ({', '.join(['%s'] * 12)})",
                _event_values(conflict_event),
            )
    operation_snapshot = operation_before.model_dump(mode="json")
    inbox_snapshot = inbox_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
    events_before = await _status_events(pool, queued.operation_id)
    attempt_before = await _attempt_fencing_details(pool, claimed.attempt.attempt_id)
    conflict_rows_before = await _events_for_idempotency_key(pool, idempotency_key)
    assert conflict_rows_before == [(conflict_event.event_id, idempotency_key)]

    with pytest.raises(IntegrationPersistenceError) as raised:
        await PostgresInboxFinalizer(pool).finalize_order_operation(
            claimed=claimed,
            retry_available_at=datetime.now(UTC) + timedelta(hours=1),
        )

    message = str(raised.value)
    assert message == "Failed to finalize provider Inbox message"
    for forbidden in (
        "order_operation_events_idempotency_key_key",
        "INSERT INTO",
        idempotency_key,
        str(inbox.inbox_id),
        str(claimed.command_id),
        str(conflict_event.event_id),
        "outbox-reference",
        operation_before.order_id,
        operation_before.customer_id,
        operation_before.tenant_id,
    ):
        assert forbidden not in message

    operation_after = await operation_repo.get_operation(scope, queued.operation_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt_after = await _attempt_fencing_details(pool, claimed.attempt.attempt_id)
    conflict_rows_after = await _events_for_idempotency_key(pool, idempotency_key)
    assert operation_after is not None
    assert operation_after.model_dump(mode="json") == operation_snapshot
    assert operation_after.status == "submitted"
    assert inbox_after is not None
    assert inbox_after.model_dump(mode="json") == inbox_snapshot
    assert inbox_after.status == "processing"
    assert inbox_after.processed_at is None
    assert inbox_after.failed_at is None
    assert inbox_after.lease_id == claimed.lease_id
    assert inbox_after.lease_owner == claimed.lease_owner
    assert inbox_after.lease_expires_at == inbox_before.lease_expires_at
    assert inbox_after.processing_attempts == inbox_before.processing_attempts
    assert inbox_after.last_error_code is None
    assert inbox_after.last_error_message is None
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert outbox_after.status == "published"
    assert events_after == events_before
    assert attempt_after == attempt_before
    assert attempt_after[5:] == (None, None, None, None)
    assert conflict_rows_after == conflict_rows_before
    assert len(conflict_rows_after) == 1


async def test_order_operation_inbox_concurrent_finalizers_apply_once(
    postgres_context,
) -> None:
    """Two independent Finalizers can commit the same claimed Inbox only once."""
    pool, tenant_id = postgres_context
    (
        scope,
        operation_repo,
        integration_repo,
        queued,
        inbox,
        claimed,
    ) = await _claimed_inbox(pool, tenant_id, "processing")
    operation_before = await operation_repo.get_operation(scope, queued.operation_id)
    inbox_before = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert operation_before is not None and operation_before.status == "submitted"
    assert inbox_before is not None and inbox_before.status == "processing"
    assert outbox_before is not None and outbox_before.status == "published"
    operation_snapshot = operation_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
    events_before = await _status_events(pool, queued.operation_id)
    attempt_before = await _attempt_fencing_details(pool, claimed.attempt.attempt_id)
    idempotency_key = f"provider-webhook:{inbox.inbox_id}:operation-status"
    assert await _events_for_idempotency_key(pool, idempotency_key) == []
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    async def _finalize_when_started(finalizer, started):
        started.set()
        return await finalizer.finalize_order_operation(
            claimed=claimed,
            retry_available_at=datetime.now(UTC) + timedelta(hours=1),
        )

    tasks: list[asyncio.Task[object]] = []
    try:
        async with pool.connection() as blocker:
            async with blocker.transaction():
                await blocker.execute(
                    "SELECT inbox_id FROM integration.inbox_messages "
                    "WHERE inbox_id=%s FOR UPDATE",
                    (inbox.inbox_id,),
                )
                tasks = [
                    asyncio.create_task(
                        _finalize_when_started(
                            PostgresInboxFinalizer(pool), first_started
                        )
                    ),
                    asyncio.create_task(
                        _finalize_when_started(
                            PostgresInboxFinalizer(pool), second_started
                        )
                    ),
                ]
                await asyncio.wait_for(
                    asyncio.gather(first_started.wait(), second_started.wait()),
                    timeout=5,
                )
                await asyncio.sleep(0)
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10,
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    successes = [
        result for result in results if isinstance(result, InboxFinalizationResult)
    ]
    conflicts = [result for result in results if isinstance(result, LeaseConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    success = successes[0]
    assert success.action == "applied"
    assert success.aggregate_type == "order_operation"
    assert success.previous_status == "submitted"
    assert success.current_status == "processing"
    assert success.safe_error_code is None
    loser_message = str(conflicts[0])
    for forbidden in (
        str(claimed.lease_id),
        claimed.lease_owner,
        str(claimed.command_id),
        "outbox-reference",
        operation_before.order_id,
        operation_before.customer_id,
        operation_before.tenant_id,
    ):
        assert forbidden not in loser_message

    operation_after = await operation_repo.get_operation(scope, queued.operation_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    events_after = await _status_events(pool, queued.operation_id)
    attempt_after = await _attempt_fencing_details(pool, claimed.attempt.attempt_id)
    attempts = await _inbox_attempt_history(pool, inbox.inbox_id)
    event_rows = await _events_for_idempotency_key(pool, idempotency_key)
    assert operation_after is not None
    assert operation_after.status == "processing"
    assert operation_after.version == operation_before.version + 1
    assert operation_after.provider_reference == "outbox-reference"
    assert operation_after.model_dump(mode="json") != operation_snapshot
    assert inbox_after is not None
    assert inbox_after.status == "processed"
    assert inbox_after.processed_at is not None
    assert inbox_after.failed_at is None
    assert inbox_after.lease_id is None
    assert inbox_after.lease_owner is None
    assert inbox_after.lease_expires_at is None
    assert inbox_after.processing_attempts == 1
    assert inbox_after.last_error_code is None
    assert inbox_after.last_error_message is None
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert outbox_after.status == "published"
    assert len(attempts) == 1
    assert attempts[0][0] == claimed.attempt.attempt_id
    assert attempt_after[:5] == attempt_before[:5]
    assert attempt_after[5] is not None
    assert attempt_after[6:] == ("processed", None, None)
    assert events_after == [
        *events_before,
        ("submitted", "processing", "outbox-reference"),
    ]
    assert len(event_rows) == 1
    assert event_rows[0][1] == idempotency_key
