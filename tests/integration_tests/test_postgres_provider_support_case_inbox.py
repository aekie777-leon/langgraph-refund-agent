"""PostgreSQL coverage for delivery-investigation Inbox finalization."""

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent.cases.models import SupportCaseEvent
from agent.cases.postgres_repository import (
    _CASE_COLUMNS,
    _EVENT_COLUMNS,
    PostgresCaseRepository,
    _case_from_row,
    _event_from_row,
    _event_values,
)
from agent.integrations.finalization import PostgresOutboxFinalizer
from agent.integrations.inbox_finalizer import InboxFinalizationResult
from agent.integrations.inbox_postgres_finalizer import PostgresInboxFinalizer
from agent.integrations.models import (
    OrderOperationCommandPayload,
    ProviderCommandResult,
    ProviderWebhookEventData,
)
from agent.integrations.postgres_repository import PostgresIntegrationRepository
from agent.integrations.repository import (
    IntegrationPersistenceError,
    LeaseConflictError,
)
from tests.integration_tests.test_postgres_provider_messaging import (
    _case_created_event,
    _delivery_case,
    _delivery_envelope,
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
    """Reuse the disposable provider-messaging fixture and its cleanup."""
    async for context in _postgres_context.__wrapped__():
        yield context


async def _claimed_delivery_inbox(
    pool,
    tenant_id: str,
    command_status: str,
    *,
    finalize_outbox: bool = True,
):
    """Build a delivery command and one claimed callback, published by default."""
    scope = _scope(tenant_id)
    case_repo = PostgresCaseRepository(pool)
    integration_repo = PostgresIntegrationRepository(pool)
    case = _delivery_case(
        tenant_id=tenant_id,
        order_id="ORD-10010",
        source_message_id=f"delivery-source-{uuid4()}",
    )
    command = _delivery_envelope(case)
    await case_repo.create_case_with_event_and_command(
        scope,
        case=case,
        event=_case_created_event(case),
        command=command,
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
        aggregate_type="support_case",
        aggregate_id=case.case_id,
        command_status=command_status,
        provider_reference="callback-reference",
        order_id=case.order_id,
        occurred_at=datetime.now(UTC),
    )
    inbox = await integration_repo.receive_inbox_idempotently(
        inbox_id=uuid4(),
        provider_connection_id="conn-1",
        event_id=f"delivery-event-{uuid4()}",
        tenant_id=tenant_id,
        event=event,
        raw_body_sha256=hashlib.sha256(b"delivery-inbox").hexdigest(),
        received_at=datetime.now(UTC),
    )
    claimed = (
        await integration_repo.claim_due_inbox(
            worker_id="inbox-worker", batch_size=1, lease_seconds=60
        )
    )[0]
    return scope, case_repo, integration_repo, case, inbox, claimed


async def _attempt(pool, attempt_id):
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT finished_at, outcome, safe_error_code, safe_error_message "
            "FROM integration.inbox_processing_attempts WHERE attempt_id=%s",
            (attempt_id,),
        )
        return await cursor.fetchone()


async def _attempt_history(pool, inbox_id):
    """Return all processing attempts for this Inbox in lifecycle order."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT attempt_id FROM integration.inbox_processing_attempts "
            "WHERE inbox_id=%s ORDER BY attempt_number",
            (inbox_id,),
        )
        return await cursor.fetchall()


async def _attempt_details(pool, attempt_id):
    """Read full persisted fencing and outcome details for one Inbox Attempt."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT attempt_id, inbox_id, attempt_number, lease_id, worker_id, "
            "finished_at, outcome, safe_error_code, safe_error_message "
            "FROM integration.inbox_processing_attempts WHERE attempt_id=%s",
            (attempt_id,),
        )
        return await cursor.fetchone()


async def _attempt_history_details(pool, inbox_id):
    """Read every persisted Inbox Attempt in lifecycle order."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT attempt_id, inbox_id, attempt_number, lease_id, worker_id, "
            "finished_at, outcome, safe_error_code, safe_error_message "
            "FROM integration.inbox_processing_attempts WHERE inbox_id=%s "
            "ORDER BY attempt_number",
            (inbox_id,),
        )
        return await cursor.fetchall()


async def _claim_support_case_webhook(
    integration_repo,
    *,
    tenant_id: str,
    case,
    command_id,
    aggregate_id=None,
    command_status: str = "completed",
    order_id: str | None = None,
):
    """Persist and claim one internally consistent support-case callback."""
    event_id = f"delivery-event-{uuid4()}"
    event = ProviderWebhookEventData(
        command_id=command_id,
        aggregate_type="support_case",
        aggregate_id=case.case_id if aggregate_id is None else aggregate_id,
        command_status=command_status,
        provider_reference="callback-reference",
        order_id=case.order_id if order_id is None else order_id,
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


async def _mutate_support_case_outbox_association(
    pool,
    *,
    command_id,
    case,
    variant: str,
) -> None:
    """Keep an Outbox canonical while changing one trusted association."""
    async with pool.connection() as connection:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                if variant == "tenant_id":
                    await cursor.execute(
                        "UPDATE integration.outbox_messages SET tenant_id=%s "
                        "WHERE command_id=%s",
                        (f"{case.tenant_id}-other", command_id),
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
                        "SET aggregate_id=%s, idempotency_key=%s "
                        "WHERE command_id=%s",
                        (
                            aggregate_id,
                            f"delivery-investigation:{aggregate_id}",
                            command_id,
                        ),
                    )
                elif variant == "payload_family":
                    aggregate_id = uuid4()
                    payload = OrderOperationCommandPayload(
                        order_id=case.order_id,
                        operation_type="cancellation",
                        reason="ordered_by_mistake",
                    )
                    await cursor.execute(
                        "UPDATE integration.outbox_messages SET "
                        "command_type='cancel_order', "
                        "aggregate_type='order_operation', aggregate_id=%s, "
                        "expected_order_version=1, idempotency_key=%s, payload=%s "
                        "WHERE command_id=%s",
                        (
                            aggregate_id,
                            f"order-operation:{aggregate_id}",
                            Jsonb(payload.model_dump(mode="json")),
                            command_id,
                        ),
                    )
                else:
                    raise ValueError(f"unsupported association variant: {variant}")
                assert cursor.rowcount == 1


async def _case_events(case_repo, scope, case_id):
    """Read enough events to compare this fixture's complete small history."""
    return await case_repo.list_case_events(
        scope,
        case_id=case_id,
        limit=100,
        offset=0,
    )


async def _get_case_unscoped(pool, case_id):
    """Read a mutated case by ID solely for this persistence-boundary test."""
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT {_CASE_COLUMNS} FROM case_management.support_cases "
                "WHERE case_id=%s",
                (case_id,),
            )
            row = await cursor.fetchone()
    return _case_from_row(row) if row is not None else None


async def _case_events_unscoped(pool, case_id):
    """Read immutable case events without treating mutated ownership as access."""
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                f"SELECT {_EVENT_COLUMNS} FROM case_management.support_case_events "
                "WHERE case_id=%s ORDER BY created_at, event_id",
                (case_id,),
            )
            rows = await cursor.fetchall()
    return tuple(_event_from_row(row) for row in rows)


async def _events_for_idempotency_key(pool, idempotency_key: str):
    """Return the exact event identities occupying a provider-update key."""
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT event_id, idempotency_key "
            "FROM case_management.support_case_events "
            "WHERE idempotency_key=%s ORDER BY event_id",
            (idempotency_key,),
        )
        return await cursor.fetchall()


async def _mutate_support_case_association(
    pool, *, case_id, tenant_id: str, variant: str
):
    """Change one model-valid Case association while preserving its aggregate row."""
    values = {
        "tenant_id": ("tenant_id", f"{tenant_id}-other"),
        "customer_id": ("customer_id", "customer-b"),
        "order_id": ("order_id", "ORD-99999"),
        "case_type": ("case_type", "general_support"),
        "source_message_id": (
            "source_message_id",
            f"delivery-source-mismatch-{uuid4()}",
        ),
    }
    try:
        column, value = values[variant]
    except KeyError as error:
        raise ValueError(f"unsupported Case association variant: {variant}") from error
    async with pool.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                f"UPDATE case_management.support_cases SET {column}=%s "
                "WHERE case_id=%s",
                (value, case_id),
            )
            assert cursor.rowcount == 1
    return value


@pytest.mark.parametrize(
    "command_status",
    ["accepted", "processing", "completed", "rejected"],
)
async def test_support_case_inbox_callback_appends_provider_update_atomically(
    postgres_context,
    command_status: str,
) -> None:
    """Every execution status appends an event without changing the Case."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        inbox,
        claimed,
    ) = await _claimed_delivery_inbox(pool, tenant_id, command_status)
    case_before = await case_repo.get_case(scope, case.case_id)
    inbox_before = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    events_before = await case_repo.list_case_events(
        scope,
        case_id=case.case_id,
        limit=100,
        offset=0,
    )
    assert case_before is not None
    assert case_before.case_type == "delivery_investigation"
    assert inbox_before is not None and inbox_before.status == "processing"
    assert outbox_before is not None and outbox_before.status == "published"
    assert outbox_before.aggregate_type == "support_case"
    case_snapshot = case_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")

    result = await PostgresInboxFinalizer(pool).finalize_support_case(
        claimed=claimed,
        retry_available_at=datetime.now(UTC) + timedelta(hours=1),
    )

    case_after = await case_repo.get_case(scope, case.case_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    events_after = await case_repo.list_case_events(
        scope,
        case_id=case.case_id,
        limit=100,
        offset=0,
    )
    attempt = await _attempt(pool, claimed.attempt.attempt_id)
    attempts = await _attempt_history(pool, inbox.inbox_id)
    assert result.action == "applied"
    assert result.aggregate_type == "support_case"
    assert result.previous_status is None
    assert result.current_status is None
    assert result.safe_error_code is None
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
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
    assert attempt is not None
    assert attempt[0] is not None
    assert attempt[1:] == ("processed", None, None)
    assert attempts == [(claimed.attempt.attempt_id,)]
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert len(events_after.items) == len(events_before.items) + 1
    provider_events = [
        event
        for event in events_after.items
        if event.idempotency_key
        == f"provider-webhook:{inbox.inbox_id}:case-provider-update"
    ]
    assert len(provider_events) == 1
    provider_event = provider_events[0]
    assert provider_event.case_id == case.case_id
    assert provider_event.event_type == "provider_update"
    assert provider_event.provider_command_id == claimed.command_id
    assert provider_event.provider_command_status == command_status
    assert provider_event.provider_reference == "callback-reference"
    assert provider_event.actor == "system"
    assert provider_event.customer_id == case_before.customer_id
    assert provider_event.tenant_id == case_before.tenant_id
    assert provider_event.previous_status is None
    assert provider_event.current_status is None


async def test_support_case_inbox_outbox_not_found_fails_atomically(
    postgres_context,
) -> None:
    """A self-consistent callback for no Outbox fails without changing its Case."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        _inbox,
        claimed,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "accepted")
    case_before = await case_repo.get_case(scope, case.case_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    assert case_before is not None
    assert outbox_before is not None
    case_snapshot = case_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
    missing_command_id = uuid4()
    assert await integration_repo.get_outbox_message(missing_command_id) is None
    inbox, missing = await _claim_support_case_webhook(
        integration_repo,
        tenant_id=tenant_id,
        case=case,
        command_id=missing_command_id,
    )

    result = await PostgresInboxFinalizer(pool).finalize_support_case(
        claimed=missing,
        retry_available_at=datetime.now(UTC) + timedelta(hours=1),
    )

    case_after = await case_repo.get_case(scope, case.case_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    attempt = await _attempt(pool, missing.attempt.attempt_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    assert result.action == "failed"
    assert result.aggregate_type == "support_case"
    assert result.safe_error_code == "outbox_not_found"
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert inbox_after is not None
    assert inbox_after.status == "failed"
    assert inbox_after.failed_at is not None
    assert inbox_after.processed_at is None
    assert inbox_after.lease_id is None
    assert inbox_after.lease_owner is None
    assert inbox_after.lease_expires_at is None
    assert inbox_after.last_error_code == "outbox_not_found"
    assert inbox_after.last_error_message == "Provider webhook could not be applied."
    assert attempt is not None
    assert attempt[0] is not None
    assert attempt[1:] == (
        "terminal_failure",
        "outbox_not_found",
        "Provider webhook could not be applied.",
    )
    assert events_after == events_before
    for safe_text in (inbox_after.last_error_message, attempt[3], str(result)):
        assert str(missing_command_id) not in safe_text
        assert str(inbox.inbox_id) not in safe_text
        assert "callback-reference" not in safe_text
        assert case.customer_id not in safe_text
        assert case.order_id not in safe_text


@pytest.mark.parametrize(
    "variant",
    ["tenant_id", "provider_connection_id", "aggregate_id", "payload_family"],
)
async def test_support_case_inbox_outbox_association_mismatch_fails_atomically(
    postgres_context,
    variant: str,
) -> None:
    """A canonical Outbox for another trusted association fails atomically."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        inbox,
        claimed,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "processing")
    case_before = await case_repo.get_case(scope, case.case_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    assert case_before is not None
    case_snapshot = case_before.model_dump(mode="json")
    await _mutate_support_case_outbox_association(
        pool,
        command_id=claimed.command_id,
        case=case,
        variant=variant,
    )
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert outbox_before is not None
    outbox_before.to_envelope()
    outbox_snapshot = outbox_before.model_dump(mode="json")

    result = await PostgresInboxFinalizer(pool).finalize_support_case(
        claimed=claimed,
        retry_available_at=datetime.now(UTC) + timedelta(hours=1),
    )

    case_after = await case_repo.get_case(scope, case.case_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    attempt = await _attempt(pool, claimed.attempt.attempt_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    assert result.action == "failed"
    assert result.aggregate_type == "support_case"
    assert result.safe_error_code == "outbox_association_mismatch"
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert inbox_after is not None
    assert inbox_after.status == "failed"
    assert inbox_after.failed_at is not None
    assert inbox_after.processed_at is None
    assert inbox_after.lease_id is None
    assert inbox_after.lease_owner is None
    assert inbox_after.lease_expires_at is None
    assert inbox_after.last_error_code == "outbox_association_mismatch"
    assert inbox_after.last_error_message == "Provider webhook could not be applied."
    assert attempt is not None
    assert attempt[0] is not None
    assert attempt[1:] == (
        "terminal_failure",
        "outbox_association_mismatch",
        "Provider webhook could not be applied.",
    )
    assert events_after == events_before
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    for safe_text in (inbox_after.last_error_message, attempt[3], str(result)):
        assert str(claimed.command_id) not in safe_text
        assert str(inbox.inbox_id) not in safe_text
        assert "callback-reference" not in safe_text
        assert case.customer_id not in safe_text
        assert case.order_id not in safe_text
        assert case.tenant_id not in safe_text
        assert "other-connection" not in safe_text
        assert str(outbox_before.aggregate_id) not in safe_text
        assert "ordered_by_mistake" not in safe_text


async def test_support_case_inbox_case_not_found_fails_atomically(
    postgres_context,
) -> None:
    """A canonical Outbox for a missing Case fails after all earlier checks pass."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        _initial_inbox,
        claimed,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "accepted")
    finalizer = PostgresInboxFinalizer(pool)
    await finalizer.finalize_support_case(
        claimed=claimed,
        retry_available_at=datetime.now(UTC) + timedelta(hours=1),
    )
    case_before = await case_repo.get_case(scope, case.case_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    assert case_before is not None
    case_snapshot = case_before.model_dump(mode="json")
    missing_case_id = uuid4()
    assert await _get_case_unscoped(pool, missing_case_id) is None
    async with pool.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                "UPDATE integration.outbox_messages SET aggregate_id=%s, "
                "idempotency_key=%s WHERE command_id=%s",
                (
                    missing_case_id,
                    f"delivery-investigation:{missing_case_id}",
                    claimed.command_id,
                ),
            )
            assert cursor.rowcount == 1
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert outbox_before is not None
    assert outbox_before.aggregate_type == "support_case"
    assert outbox_before.aggregate_id == missing_case_id
    outbox_before.to_envelope()
    outbox_snapshot = outbox_before.model_dump(mode="json")
    inbox, missing = await _claim_support_case_webhook(
        integration_repo,
        tenant_id=tenant_id,
        case=case,
        command_id=claimed.command_id,
        aggregate_id=missing_case_id,
    )

    result = await finalizer.finalize_support_case(
        claimed=missing,
        retry_available_at=datetime.now(UTC) + timedelta(hours=1),
    )

    case_after = await case_repo.get_case(scope, case.case_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    attempt = await _attempt(pool, missing.attempt.attempt_id)
    attempts = await _attempt_history(pool, inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    assert result.action == "failed"
    assert result.aggregate_type == "support_case"
    assert result.safe_error_code == "case_not_found"
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert events_after == events_before
    assert inbox_after is not None
    assert inbox_after.status == "failed"
    assert inbox_after.failed_at is not None
    assert inbox_after.processed_at is None
    assert inbox_after.lease_id is None
    assert inbox_after.lease_owner is None
    assert inbox_after.lease_expires_at is None
    assert inbox_after.last_error_code == "case_not_found"
    assert inbox_after.last_error_message == "Provider webhook could not be applied."
    assert attempt is not None
    assert attempt[0] is not None
    assert attempt[1:] == (
        "terminal_failure",
        "case_not_found",
        "Provider webhook could not be applied.",
    )
    assert attempts == [(missing.attempt.attempt_id,)]
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert await _get_case_unscoped(pool, missing_case_id) is None
    for safe_text in (inbox_after.last_error_message, attempt[3], str(result)):
        assert str(missing_case_id) not in safe_text
        assert str(claimed.command_id) not in safe_text
        assert str(inbox.inbox_id) not in safe_text
        assert "callback-reference" not in safe_text
        assert case.tenant_id not in safe_text
        assert case.customer_id not in safe_text
        assert case.order_id not in safe_text


@pytest.mark.parametrize(
    "variant",
    ["tenant_id", "customer_id", "order_id", "case_type", "source_message_id"],
)
async def test_support_case_inbox_case_association_mismatch_fails_atomically(
    postgres_context,
    variant: str,
) -> None:
    """A model-valid Case for another association is never updated by a callback."""
    pool, tenant_id = postgres_context
    (
        _scope_value,
        _case_repo,
        integration_repo,
        case,
        inbox,
        claimed,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "processing")
    target_value = await _mutate_support_case_association(
        pool,
        case_id=case.case_id,
        tenant_id=tenant_id,
        variant=variant,
    )
    case_before = await _get_case_unscoped(pool, case.case_id)
    events_before = await _case_events_unscoped(pool, case.case_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert case_before is not None
    assert case_before.model_dump(mode="json")[variant] == target_value
    assert outbox_before is not None and outbox_before.status == "published"
    outbox_before.to_envelope()
    case_snapshot = case_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")

    result = await PostgresInboxFinalizer(pool).finalize_support_case(
        claimed=claimed,
        retry_available_at=datetime.now(UTC) + timedelta(hours=1),
    )

    case_after = await _get_case_unscoped(pool, case.case_id)
    events_after = await _case_events_unscoped(pool, case.case_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    attempt = await _attempt(pool, claimed.attempt.attempt_id)
    attempts = await _attempt_history(pool, inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    assert result.action == "failed"
    assert result.aggregate_type == "support_case"
    assert result.safe_error_code == "support_case_association_mismatch"
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert events_after == events_before
    assert inbox_after is not None
    assert inbox_after.status == "failed"
    assert inbox_after.failed_at is not None
    assert inbox_after.processed_at is None
    assert inbox_after.lease_id is None
    assert inbox_after.lease_owner is None
    assert inbox_after.lease_expires_at is None
    assert inbox_after.last_error_code == "support_case_association_mismatch"
    assert inbox_after.last_error_message == "Provider webhook could not be applied."
    assert attempt is not None
    assert attempt[0] is not None
    assert attempt[1:] == (
        "terminal_failure",
        "support_case_association_mismatch",
        "Provider webhook could not be applied.",
    )
    assert attempts == [(claimed.attempt.attempt_id,)]
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    for safe_text in (inbox_after.last_error_message, attempt[3], str(result)):
        assert target_value not in safe_text
        assert str(claimed.command_id) not in safe_text
        assert str(inbox.inbox_id) not in safe_text
        assert "callback-reference" not in safe_text
        assert case.customer_id not in safe_text
        assert case.order_id not in safe_text


async def test_support_case_inbox_event_order_id_mismatch_fails_atomically(
    postgres_context,
) -> None:
    """A callback's auxiliary order ID must still agree with the local Case."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        _initial_inbox,
        claimed,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "accepted")
    finalizer = PostgresInboxFinalizer(pool)
    await finalizer.finalize_support_case(
        claimed=claimed,
        retry_available_at=datetime.now(UTC) + timedelta(hours=1),
    )
    case_before = await case_repo.get_case(scope, case.case_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert case_before is not None
    assert outbox_before is not None
    case_snapshot = case_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
    inbox, mismatch = await _claim_support_case_webhook(
        integration_repo,
        tenant_id=tenant_id,
        case=case,
        command_id=claimed.command_id,
        order_id="ORD-99999",
    )

    result = await finalizer.finalize_support_case(
        claimed=mismatch,
        retry_available_at=datetime.now(UTC) + timedelta(hours=1),
    )

    case_after = await case_repo.get_case(scope, case.case_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    attempt = await _attempt(pool, mismatch.attempt.attempt_id)
    attempts = await _attempt_history(pool, inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    assert result.action == "failed"
    assert result.aggregate_type == "support_case"
    assert result.safe_error_code == "support_case_association_mismatch"
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert events_after == events_before
    assert inbox_after is not None
    assert inbox_after.status == "failed"
    assert inbox_after.failed_at is not None
    assert inbox_after.processed_at is None
    assert inbox_after.lease_id is None
    assert inbox_after.lease_owner is None
    assert inbox_after.lease_expires_at is None
    assert inbox_after.last_error_code == "support_case_association_mismatch"
    assert inbox_after.last_error_message == "Provider webhook could not be applied."
    assert attempt is not None
    assert attempt[0] is not None
    assert attempt[1:] == (
        "terminal_failure",
        "support_case_association_mismatch",
        "Provider webhook could not be applied.",
    )
    assert attempts == [(mismatch.attempt.attempt_id,)]
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    for safe_text in (inbox_after.last_error_message, attempt[3], str(result)):
        assert "ORD-99999" not in safe_text
        assert str(claimed.command_id) not in safe_text
        assert str(inbox.inbox_id) not in safe_text
        assert "callback-reference" not in safe_text
        assert case.customer_id not in safe_text
        assert case.order_id not in safe_text


async def test_support_case_inbox_published_outbox_ignores_expired_retry_time(
    postgres_context,
) -> None:
    """A final Outbox does not consume or validate its unused retry instant."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        inbox,
        claimed,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "accepted")
    case_before = await case_repo.get_case(scope, case.case_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    inbox_before = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert case_before is not None
    assert inbox_before is not None and inbox_before.status == "processing"
    assert outbox_before is not None and outbox_before.status == "published"
    case_snapshot = case_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")

    result = await PostgresInboxFinalizer(pool).finalize_support_case(
        claimed=claimed,
        retry_available_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    case_after = await case_repo.get_case(scope, case.case_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    attempt = await _attempt_details(pool, claimed.attempt.attempt_id)
    attempts = await _attempt_history_details(pool, inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    assert result.action == "applied"
    assert result.aggregate_type == "support_case"
    assert result.safe_error_code is None
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert inbox_after is not None
    assert inbox_after.status == "processed"
    assert inbox_after.processed_at is not None
    assert inbox_after.failed_at is None
    assert inbox_after.lease_id is None
    assert inbox_after.lease_owner is None
    assert inbox_after.lease_expires_at is None
    assert inbox_after.last_error_code is None
    assert inbox_after.last_error_message is None
    assert attempt is not None
    assert attempt[5] is not None
    assert attempt[6:] == ("processed", None, None)
    assert attempts == [attempt]
    assert outbox_after is not None
    assert outbox_after.status == "published"
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    provider_events = [
        event
        for event in events_after.items
        if event.idempotency_key
        == f"provider-webhook:{inbox.inbox_id}:case-provider-update"
    ]
    assert len(events_after.items) == len(events_before.items) + 1
    assert len(provider_events) == 1


async def test_support_case_inbox_retry_rejects_expired_retry_time_atomically(
    postgres_context,
) -> None:
    """A pending Outbox rejects stale retry scheduling without partial writes."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        inbox,
        claimed,
    ) = await _claimed_delivery_inbox(
        pool,
        tenant_id,
        "accepted",
        finalize_outbox=False,
    )
    case_before = await case_repo.get_case(scope, case.case_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    inbox_before = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    attempt_before = await _attempt_details(pool, claimed.attempt.attempt_id)
    attempts_before = await _attempt_history_details(pool, inbox.inbox_id)
    assert case_before is not None
    assert inbox_before is not None and inbox_before.status == "processing"
    assert outbox_before is not None and outbox_before.status == "pending"
    assert attempt_before is not None
    case_snapshot = case_before.model_dump(mode="json")
    inbox_snapshot = inbox_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")

    with pytest.raises(ValueError, match="retry_available_at"):
        await PostgresInboxFinalizer(pool).finalize_support_case(
            claimed=claimed,
            retry_available_at=datetime.now(UTC) - timedelta(seconds=1),
        )

    case_after = await case_repo.get_case(scope, case.case_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    attempt_after = await _attempt_details(pool, claimed.attempt.attempt_id)
    attempts_after = await _attempt_history_details(pool, inbox.inbox_id)
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert events_after == events_before
    assert inbox_after is not None
    assert inbox_after.model_dump(mode="json") == inbox_snapshot
    assert inbox_after.status == "processing"
    assert inbox_after.processed_at is None
    assert inbox_after.failed_at is None
    assert inbox_after.last_error_code is None
    assert inbox_after.last_error_message is None
    assert outbox_after is not None
    assert outbox_after.status == "pending"
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert attempt_after == attempt_before
    assert attempt_after[5:] == (None, None, None, None)
    assert attempts_after == attempts_before


async def test_support_case_inbox_fifth_outbox_wait_fails_terminally(
    postgres_context,
) -> None:
    """Five genuine waits for a pending delivery command exhaust the Inbox budget."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        inbox,
        claimed,
    ) = await _claimed_delivery_inbox(
        pool,
        tenant_id,
        "accepted",
        finalize_outbox=False,
    )
    case_before = await case_repo.get_case(scope, case.case_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    assert case_before is not None
    assert outbox_before is not None and outbox_before.status == "pending"
    case_snapshot = case_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
    finalizer = PostgresInboxFinalizer(pool)
    current_claim = claimed

    for attempt_number in range(1, 5):
        retry_available_at = datetime.now(UTC) + timedelta(hours=1)
        result = await finalizer.finalize_support_case(
            claimed=current_claim,
            retry_available_at=retry_available_at,
        )
        persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
        attempt = await _attempt_details(pool, current_claim.attempt.attempt_id)
        case_after = await case_repo.get_case(scope, case.case_id)
        events_after = await _case_events(case_repo, scope, case.case_id)
        outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
        assert result.action == "retry_scheduled"
        assert result.aggregate_type == "support_case"
        assert result.safe_error_code == "outbox_not_finalized"
        assert persisted is not None
        assert persisted.status == "received"
        assert persisted.processing_attempts == attempt_number
        assert persisted.lease_id is None
        assert persisted.lease_owner is None
        assert persisted.lease_expires_at is None
        assert persisted.last_error_code == "outbox_not_finalized"
        assert persisted.last_error_message == "Provider command is not finalized."
        assert persisted.available_at >= retry_available_at - timedelta(seconds=1)
        assert attempt is not None
        assert attempt[2] == attempt_number
        assert attempt[3] == current_claim.lease_id
        assert attempt[4] == current_claim.lease_owner
        assert attempt[5] is not None
        assert attempt[6:] == (
            "retry_scheduled",
            "outbox_not_finalized",
            "Provider command is not finalized.",
        )
        assert case_after is not None
        assert case_after.model_dump(mode="json") == case_snapshot
        assert events_after == events_before
        assert outbox_after is not None
        assert outbox_after.model_dump(mode="json") == outbox_snapshot
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

    result = await finalizer.finalize_support_case(
        claimed=current_claim,
        retry_available_at=datetime.now(UTC) + timedelta(hours=1),
    )

    persisted = await integration_repo.get_inbox_message(inbox.inbox_id)
    case_after = await case_repo.get_case(scope, case.case_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    fifth_attempt = await _attempt_details(pool, current_claim.attempt.attempt_id)
    attempts = await _attempt_history_details(pool, inbox.inbox_id)
    assert result.action == "failed"
    assert result.aggregate_type == "support_case"
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
    assert fifth_attempt is not None
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
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert events_after == events_before
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    for safe_message in (persisted.last_error_message, fifth_attempt[8], str(result)):
        assert str(claimed.command_id) not in safe_message
        assert str(inbox.inbox_id) not in safe_message
        assert "callback-reference" not in safe_message
        assert case.tenant_id not in safe_message
        assert case.customer_id not in safe_message
        assert case.order_id not in safe_message
        assert str(case.case_id) not in safe_message
    assert (
        await integration_repo.claim_due_inbox(
            worker_id="inbox-worker",
            batch_size=1,
            lease_seconds=60,
        )
        == []
    )


@pytest.mark.parametrize("fence_field", ["lease_id", "lease_owner", "attempt_id"])
async def test_support_case_inbox_forged_fence_rolls_back(
    postgres_context,
    fence_field: str,
) -> None:
    """A self-consistent forged handle cannot finalize a claimed Case callback."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        inbox,
        claimed,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "accepted")
    case_before = await case_repo.get_case(scope, case.case_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    inbox_before = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    attempt_before = await _attempt_details(pool, claimed.attempt.attempt_id)
    attempts_before = await _attempt_history_details(pool, inbox.inbox_id)
    assert case_before is not None
    assert inbox_before is not None
    assert outbox_before is not None and outbox_before.status == "published"
    assert attempt_before is not None
    case_snapshot = case_before.model_dump(mode="json")
    inbox_snapshot = inbox_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
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
        await PostgresInboxFinalizer(pool).finalize_support_case(
            claimed=forged,
            retry_available_at=datetime.now(UTC) + timedelta(hours=1),
        )

    case_after = await case_repo.get_case(scope, case.case_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    attempt_after = await _attempt_details(pool, claimed.attempt.attempt_id)
    attempts_after = await _attempt_history_details(pool, inbox.inbox_id)
    error_text = str(raised.value)
    assert str(forged_value) not in error_text
    assert str(claimed.command_id) not in error_text
    assert "callback-reference" not in error_text
    assert case.customer_id not in error_text
    assert case.order_id not in error_text
    assert str(case.case_id) not in error_text
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert events_after == events_before
    assert inbox_after is not None
    assert inbox_after.model_dump(mode="json") == inbox_snapshot
    assert inbox_after.status == "processing"
    assert inbox_after.processed_at is None
    assert inbox_after.failed_at is None
    assert inbox_after.last_error_code is None
    assert inbox_after.last_error_message is None
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert attempt_after == attempt_before
    assert attempt_after[5:] == (None, None, None, None)
    assert attempts_after == attempts_before


async def test_support_case_inbox_expired_lease_rolls_back(
    postgres_context,
) -> None:
    """An expired Case callback lease is fenced without implicit recovery."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        inbox,
        claimed,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "accepted")
    async with pool.connection() as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                "UPDATE integration.inbox_messages SET "
                "lease_expires_at=clock_timestamp() - interval '1 second' "
                "WHERE inbox_id=%s",
                (inbox.inbox_id,),
            )
            assert cursor.rowcount == 1
    case_before = await case_repo.get_case(scope, case.case_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    inbox_before = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    attempt_before = await _attempt_details(pool, claimed.attempt.attempt_id)
    attempts_before = await _attempt_history_details(pool, inbox.inbox_id)
    assert case_before is not None
    assert inbox_before is not None
    assert inbox_before.lease_expires_at is not None
    assert inbox_before.lease_expires_at < datetime.now(UTC)
    assert outbox_before is not None and outbox_before.status == "published"
    assert attempt_before is not None
    case_snapshot = case_before.model_dump(mode="json")
    inbox_snapshot = inbox_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")

    with pytest.raises(LeaseConflictError):
        await PostgresInboxFinalizer(pool).finalize_support_case(
            claimed=claimed,
            retry_available_at=datetime.now(UTC) + timedelta(hours=1),
        )

    case_after = await case_repo.get_case(scope, case.case_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    attempt_after = await _attempt_details(pool, claimed.attempt.attempt_id)
    attempts_after = await _attempt_history_details(pool, inbox.inbox_id)
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert events_after == events_before
    assert inbox_after is not None
    assert inbox_after.model_dump(mode="json") == inbox_snapshot
    assert inbox_after.status == "processing"
    assert inbox_after.processed_at is None
    assert inbox_after.failed_at is None
    assert inbox_after.last_error_code is None
    assert inbox_after.last_error_message is None
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert attempt_after == attempt_before
    assert attempt_after[5:] == (None, None, None, None)
    assert attempts_after == attempts_before


async def test_support_case_inbox_stale_claim_after_recovery_is_fenced(
    postgres_context,
) -> None:
    """Recovery prevents worker one from changing worker two's claimed callback."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        inbox,
        claimed_one,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "accepted")
    case_before = await case_repo.get_case(scope, case.case_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    outbox_before = await integration_repo.get_outbox_message(claimed_one.command_id)
    assert case_before is not None
    assert outbox_before is not None and outbox_before.status == "published"
    case_snapshot = case_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
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
    recovered = await integration_repo.get_inbox_message(inbox.inbox_id)
    first_attempt = await _attempt_details(pool, claimed_one.attempt.attempt_id)
    assert recovered is not None
    assert recovered.status == "received"
    assert recovered.lease_id is None
    assert recovered.lease_owner is None
    assert recovered.lease_expires_at is None
    assert first_attempt is not None
    assert first_attempt[5] is not None
    assert first_attempt[6] == "lease_expired"
    claimed_two = (
        await integration_repo.claim_due_inbox(
            worker_id="inbox-worker-2",
            batch_size=1,
            lease_seconds=60,
        )
    )[0]
    persisted_two = await integration_repo.get_inbox_message(inbox.inbox_id)
    attempts_after_claim = await _attempt_history_details(pool, inbox.inbox_id)
    assert claimed_two.attempt.attempt_number == 2
    assert claimed_two.processing_attempts == 2
    assert claimed_two.lease_owner == "inbox-worker-2"
    assert claimed_two.lease_id != claimed_one.lease_id
    assert claimed_two.attempt.attempt_id != claimed_one.attempt.attempt_id
    assert persisted_two is not None
    assert persisted_two.status == "processing"
    assert persisted_two.lease_id == claimed_two.lease_id
    assert persisted_two.lease_owner == claimed_two.lease_owner
    assert persisted_two.lease_expires_at == claimed_two.lease_expires_at
    assert len(attempts_after_claim) == 2
    assert [attempt[0] for attempt in attempts_after_claim] == [
        claimed_one.attempt.attempt_id,
        claimed_two.attempt.attempt_id,
    ]
    assert attempts_after_claim[0] == first_attempt
    assert attempts_after_claim[1][5:] == (None, None, None, None)

    with pytest.raises(LeaseConflictError):
        await PostgresInboxFinalizer(pool).finalize_support_case(
            claimed=claimed_one,
            retry_available_at=datetime.now(UTC) + timedelta(hours=1),
        )

    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    case_after = await case_repo.get_case(scope, case.case_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    outbox_after = await integration_repo.get_outbox_message(claimed_one.command_id)
    first_attempt_after = await _attempt_details(pool, claimed_one.attempt.attempt_id)
    second_attempt = await _attempt_details(pool, claimed_two.attempt.attempt_id)
    attempts_after = await _attempt_history_details(pool, inbox.inbox_id)
    assert inbox_after is not None
    assert inbox_after.status == "processing"
    assert inbox_after.lease_id == claimed_two.lease_id
    assert inbox_after.lease_owner == claimed_two.lease_owner
    assert inbox_after.lease_expires_at == claimed_two.lease_expires_at
    assert inbox_after.processing_attempts == claimed_two.processing_attempts
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert events_after == events_before
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert first_attempt_after == first_attempt
    assert second_attempt is not None
    assert second_attempt[5:] == (None, None, None, None)
    assert len(attempts_after) == 2
    assert [attempt[0] for attempt in attempts_after] == [
        claimed_one.attempt.attempt_id,
        claimed_two.attempt.attempt_id,
    ]


async def test_support_case_inbox_event_idempotency_conflict_rolls_back_atomically(
    postgres_context,
) -> None:
    """A real provider-update key conflict rolls back support-case finalization."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        inbox,
        claimed,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "processing")
    idempotency_key = f"provider-webhook:{inbox.inbox_id}:case-provider-update"
    conflict_event = SupportCaseEvent(
        event_id=uuid4(),
        idempotency_key=idempotency_key,
        case_id=case.case_id,
        event_type="provider_update",
        provider_command_id=claimed.command_id,
        provider_command_status="processing",
        provider_reference="preexisting-reference",
        actor="system",
        customer_id=case.customer_id,
        tenant_id=case.tenant_id,
        created_at=datetime.now(UTC),
    )
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                f"INSERT INTO case_management.support_case_events ({_EVENT_COLUMNS}) "
                f"VALUES ({', '.join(['%s'] * 24)})",
                _event_values(conflict_event),
            )
    case_before = await case_repo.get_case(scope, case.case_id)
    inbox_before = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    attempt_before = await _attempt_details(pool, claimed.attempt.attempt_id)
    attempts_before = await _attempt_history_details(pool, inbox.inbox_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    conflict_rows_before = await _events_for_idempotency_key(pool, idempotency_key)
    assert case_before is not None
    assert case_before.case_type == "delivery_investigation"
    assert inbox_before is not None and inbox_before.status == "processing"
    assert inbox_before.lease_id == claimed.lease_id
    assert inbox_before.lease_owner == claimed.lease_owner
    assert outbox_before is not None and outbox_before.status == "published"
    assert attempt_before is not None
    assert attempt_before[5:] == (None, None, None, None)
    assert conflict_rows_before == [(conflict_event.event_id, idempotency_key)]
    case_snapshot = case_before.model_dump(mode="json")
    inbox_snapshot = inbox_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")

    with pytest.raises(IntegrationPersistenceError) as raised:
        await PostgresInboxFinalizer(pool).finalize_support_case(
            claimed=claimed,
            retry_available_at=datetime.now(UTC) + timedelta(hours=1),
        )

    message = str(raised.value)
    assert message == "Failed to finalize provider Inbox message"
    for forbidden in (
        "uq_support_case_events_idempotency",
        "support_case_events",
        "INSERT INTO",
        idempotency_key,
        str(inbox.inbox_id),
        str(claimed.command_id),
        str(conflict_event.event_id),
        "preexisting-reference",
        str(case.case_id),
        case.order_id,
        case.customer_id,
        case.tenant_id,
    ):
        assert forbidden not in message

    case_after = await case_repo.get_case(scope, case.case_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    attempt_after = await _attempt_details(pool, claimed.attempt.attempt_id)
    attempts_after = await _attempt_history_details(pool, inbox.inbox_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    conflict_rows_after = await _events_for_idempotency_key(pool, idempotency_key)
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert inbox_after is not None
    assert inbox_after.model_dump(mode="json") == inbox_snapshot
    assert inbox_after.status == "processing"
    assert inbox_after.processed_at is None
    assert inbox_after.failed_at is None
    assert inbox_after.lease_id == claimed.lease_id
    assert inbox_after.lease_owner == claimed.lease_owner
    assert inbox_after.last_error_code is None
    assert inbox_after.last_error_message is None
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert outbox_after.status == "published"
    assert attempt_after == attempt_before
    assert attempt_after[5:] == (None, None, None, None)
    assert attempts_after == attempts_before
    assert events_after == events_before
    assert conflict_rows_after == conflict_rows_before
    assert conflict_rows_after == [(conflict_event.event_id, idempotency_key)]


async def test_support_case_inbox_concurrent_finalizers_apply_once(
    postgres_context,
) -> None:
    """Two Finalizers sharing one claim commit one provider update at most once."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        inbox,
        claimed,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "processing")
    case_before = await case_repo.get_case(scope, case.case_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    inbox_before = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_before = await integration_repo.get_outbox_message(claimed.command_id)
    attempt_before = await _attempt_details(pool, claimed.attempt.attempt_id)
    attempts_before = await _attempt_history_details(pool, inbox.inbox_id)
    idempotency_key = f"provider-webhook:{inbox.inbox_id}:case-provider-update"
    event_rows_before = await _events_for_idempotency_key(pool, idempotency_key)
    assert case_before is not None
    assert inbox_before is not None and inbox_before.status == "processing"
    assert outbox_before is not None and outbox_before.status == "published"
    assert attempt_before is not None
    assert attempt_before[5:] == (None, None, None, None)
    assert attempts_before == [attempt_before]
    assert event_rows_before == []
    case_snapshot = case_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    async def _finalize_when_started(started: asyncio.Event):
        started.set()
        return await PostgresInboxFinalizer(pool).finalize_support_case(
            claimed=claimed,
            retry_available_at=datetime.now(UTC) + timedelta(hours=1),
        )

    tasks: list[asyncio.Task[object]] = []
    results: list[object] = []
    try:
        async with pool.connection() as blocker:
            async with blocker.transaction():
                await blocker.execute(
                    "SELECT inbox_id FROM integration.inbox_messages "
                    "WHERE inbox_id=%s FOR UPDATE",
                    (inbox.inbox_id,),
                )
                tasks = [
                    asyncio.create_task(_finalize_when_started(first_started)),
                    asyncio.create_task(_finalize_when_started(second_started)),
                ]
                await asyncio.wait_for(
                    asyncio.gather(first_started.wait(), second_started.wait()),
                    timeout=5,
                )
                await asyncio.sleep(0)
        results = list(
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=10,
            )
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
    assert success.aggregate_type == "support_case"
    assert success.previous_status is None
    assert success.current_status is None
    assert success.safe_error_code is None
    loser_message = str(conflicts[0])
    for forbidden in (
        str(claimed.lease_id),
        claimed.lease_owner,
        str(claimed.command_id),
        "callback-reference",
        str(case.case_id),
        case.order_id,
        case.customer_id,
        case.tenant_id,
    ):
        assert forbidden not in loser_message

    case_after = await case_repo.get_case(scope, case.case_id)
    inbox_after = await integration_repo.get_inbox_message(inbox.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed.command_id)
    attempt_after = await _attempt_details(pool, claimed.attempt.attempt_id)
    attempts_after = await _attempt_history_details(pool, inbox.inbox_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    event_rows_after = await _events_for_idempotency_key(pool, idempotency_key)
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
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
    assert len(attempts_after) == 1
    assert attempts_after[0][0] == claimed.attempt.attempt_id
    assert attempt_after[:5] == attempt_before[:5]
    assert attempt_after[5] is not None
    assert attempt_after[6:] == ("processed", None, None)
    assert len(events_after.items) == len(events_before.items) + 1
    provider_events = [
        event
        for event in events_after.items
        if event.idempotency_key == idempotency_key
    ]
    assert len(provider_events) == 1
    provider_event = provider_events[0]
    assert provider_event.event_type == "provider_update"
    assert provider_event.case_id == case.case_id
    assert provider_event.provider_command_id == claimed.command_id
    assert provider_event.provider_command_status == "processing"
    assert provider_event.provider_reference == "callback-reference"
    assert provider_event.actor == "system"
    assert provider_event.customer_id == case.customer_id
    assert provider_event.tenant_id == case.tenant_id
    assert len(event_rows_after) == 1
    assert event_rows_after[0][1] == idempotency_key


async def test_support_case_inbox_concurrent_distinct_callbacks_both_apply(
    postgres_context,
) -> None:
    """Completed and rejected callbacks append independent audits to one Case."""
    pool, tenant_id = postgres_context
    (
        scope,
        case_repo,
        integration_repo,
        case,
        inbox_one,
        claimed_one,
    ) = await _claimed_delivery_inbox(pool, tenant_id, "completed")
    inbox_two, claimed_two = await _claim_support_case_webhook(
        integration_repo,
        tenant_id=tenant_id,
        case=case,
        command_id=claimed_one.command_id,
        command_status="rejected",
    )
    case_before = await case_repo.get_case(scope, case.case_id)
    events_before = await _case_events(case_repo, scope, case.case_id)
    persisted_one_before = await integration_repo.get_inbox_message(inbox_one.inbox_id)
    persisted_two_before = await integration_repo.get_inbox_message(inbox_two.inbox_id)
    outbox_before = await integration_repo.get_outbox_message(claimed_one.command_id)
    attempt_one_before = await _attempt_details(pool, claimed_one.attempt.attempt_id)
    attempt_two_before = await _attempt_details(pool, claimed_two.attempt.attempt_id)
    attempts_one_before = await _attempt_history_details(pool, inbox_one.inbox_id)
    attempts_two_before = await _attempt_history_details(pool, inbox_two.inbox_id)
    key_one = f"provider-webhook:{inbox_one.inbox_id}:case-provider-update"
    key_two = f"provider-webhook:{inbox_two.inbox_id}:case-provider-update"
    key_one_before = await _events_for_idempotency_key(pool, key_one)
    key_two_before = await _events_for_idempotency_key(pool, key_two)
    assert case_before is not None
    assert (
        persisted_one_before is not None and persisted_one_before.status == "processing"
    )
    assert (
        persisted_two_before is not None and persisted_two_before.status == "processing"
    )
    assert outbox_before is not None and outbox_before.status == "published"
    assert inbox_one.inbox_id != inbox_two.inbox_id
    assert inbox_one.event_id != inbox_two.event_id
    assert claimed_one.attempt.attempt_id != claimed_two.attempt.attempt_id
    assert claimed_one.lease_id != claimed_two.lease_id
    assert claimed_one.command_id == claimed_two.command_id
    assert claimed_one.aggregate_id == claimed_two.aggregate_id == case.case_id
    assert attempt_one_before is not None
    assert attempt_two_before is not None
    assert attempt_one_before[5:] == (None, None, None, None)
    assert attempt_two_before[5:] == (None, None, None, None)
    assert attempts_one_before == [attempt_one_before]
    assert attempts_two_before == [attempt_two_before]
    assert key_one_before == []
    assert key_two_before == []
    before_provider_update_count = sum(
        event.event_type == "provider_update" for event in events_before.items
    )
    case_snapshot = case_before.model_dump(mode="json")
    outbox_snapshot = outbox_before.model_dump(mode="json")
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    async def _finalize_when_started(claimed, started: asyncio.Event):
        started.set()
        return await PostgresInboxFinalizer(pool).finalize_support_case(
            claimed=claimed,
            retry_available_at=datetime.now(UTC) + timedelta(hours=1),
        )

    tasks: list[asyncio.Task[object]] = []
    results: list[object] = []
    try:
        async with pool.connection() as blocker:
            async with blocker.transaction():
                await blocker.execute(
                    "SELECT command_id FROM integration.outbox_messages "
                    "WHERE command_id=%s FOR UPDATE",
                    (claimed_one.command_id,),
                )
                tasks = [
                    asyncio.create_task(
                        _finalize_when_started(claimed_one, first_started)
                    ),
                    asyncio.create_task(
                        _finalize_when_started(claimed_two, second_started)
                    ),
                ]
                await asyncio.wait_for(
                    asyncio.gather(first_started.wait(), second_started.wait()),
                    timeout=5,
                )
                await asyncio.sleep(0)
        results = list(
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=10,
            )
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    assert len(results) == 2
    assert all(isinstance(result, InboxFinalizationResult) for result in results)
    for result in results:
        assert isinstance(result, InboxFinalizationResult)
        assert result.action == "applied"
        assert result.aggregate_type == "support_case"
        assert result.previous_status is None
        assert result.current_status is None
        assert result.safe_error_code is None

    case_after = await case_repo.get_case(scope, case.case_id)
    persisted_one_after = await integration_repo.get_inbox_message(inbox_one.inbox_id)
    persisted_two_after = await integration_repo.get_inbox_message(inbox_two.inbox_id)
    outbox_after = await integration_repo.get_outbox_message(claimed_one.command_id)
    attempt_one_after = await _attempt_details(pool, claimed_one.attempt.attempt_id)
    attempt_two_after = await _attempt_details(pool, claimed_two.attempt.attempt_id)
    attempts_one_after = await _attempt_history_details(pool, inbox_one.inbox_id)
    attempts_two_after = await _attempt_history_details(pool, inbox_two.inbox_id)
    events_after = await _case_events(case_repo, scope, case.case_id)
    key_one_after = await _events_for_idempotency_key(pool, key_one)
    key_two_after = await _events_for_idempotency_key(pool, key_two)
    assert case_after is not None
    assert case_after.model_dump(mode="json") == case_snapshot
    assert outbox_after is not None
    assert outbox_after.model_dump(mode="json") == outbox_snapshot
    assert outbox_after.status == "published"
    for persisted in (persisted_one_after, persisted_two_after):
        assert persisted is not None
        assert persisted.status == "processed"
        assert persisted.processed_at is not None
        assert persisted.failed_at is None
        assert persisted.lease_id is None
        assert persisted.lease_owner is None
        assert persisted.lease_expires_at is None
        assert persisted.processing_attempts == 1
        assert persisted.last_error_code is None
        assert persisted.last_error_message is None
    for attempt_after, attempt_before, attempts_after, claimed in (
        (attempt_one_after, attempt_one_before, attempts_one_after, claimed_one),
        (attempt_two_after, attempt_two_before, attempts_two_after, claimed_two),
    ):
        assert attempt_after is not None
        assert len(attempts_after) == 1
        assert attempts_after[0][0] == claimed.attempt.attempt_id
        assert attempt_after[:5] == attempt_before[:5]
        assert attempt_after[5] is not None
        assert attempt_after[6:] == ("processed", None, None)
    assert len(events_after.items) == len(events_before.items) + 2
    assert len(key_one_after) == 1
    assert len(key_two_after) == 1
    assert key_one_after[0][1] == key_one
    assert key_two_after[0][1] == key_two
    assert key_one_after[0][0] != key_two_after[0][0]
    provider_events = {
        event.idempotency_key: event
        for event in events_after.items
        if event.event_type == "provider_update"
        and event.idempotency_key in {key_one, key_two}
    }
    assert set(provider_events) == {key_one, key_two}
    after_provider_update_count = sum(
        event.event_type == "provider_update" for event in events_after.items
    )
    assert after_provider_update_count == before_provider_update_count + 2
    for key, expected_status in ((key_one, "completed"), (key_two, "rejected")):
        event = provider_events[key]
        assert event.case_id == case.case_id
        assert event.provider_command_id == claimed_one.command_id
        assert event.provider_command_status == expected_status
        assert event.provider_reference == "callback-reference"
        assert event.actor == "system"
        assert event.customer_id == case.customer_id
        assert event.tenant_id == case.tenant_id
