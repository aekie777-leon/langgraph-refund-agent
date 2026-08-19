"""PostgreSQL coverage for tenant-scoped Provider operations and recovery."""

import asyncio
import hashlib
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from agent.auth.dependencies import require_access_scope
from agent.auth.rbac import role_permissions
from agent.auth.visibility import ForbiddenError
from agent.cases.models import SupportCase, SupportCaseEvent
from agent.cases.postgres_repository import PostgresCaseRepository
from agent.database import create_async_connection_pool
from agent.integrations.finalization import PostgresOutboxFinalizer
from agent.integrations.models import ProviderCommandResult, ProviderWebhookEventData
from agent.integrations.postgres_repository import PostgresIntegrationRepository
from agent.integrations.postgres_writes import insert_outbox_message
from agent.integrations.provider_operations_api import router
from agent.integrations.provider_operations_api_errors import (
    register_provider_operations_exception_handlers,
)
from agent.integrations.provider_operations_contracts import ProviderRedriveRequest
from agent.integrations.provider_operations_postgres import (
    PostgresProviderOperationsRepository,
)
from agent.integrations.provider_operations_repository import (
    ProviderOperationsConflictError,
    ProviderOperationsNotFoundError,
)
from agent.integrations.provider_operations_service import ProviderOperationsService
from agent.migrations import discover_migrations
from agent.operations.models import OrderOperation
from agent.operations.postgres_repository import (
    _EVENT_COLUMNS as _OPERATION_EVENT_COLUMNS,
)
from agent.operations.postgres_repository import PostgresOrderOperationRepository
from tests.fakes.identity import make_scope
from tests.integration_tests.test_postgres_provider_messaging import (
    _case_created_event,
    _delivery_case,
    _delivery_envelope,
    _envelope,
    _operation,
    _scope,
)
from tests.integration_tests.test_postgres_provider_messaging import (
    postgres_context as _postgres_context,
)

pytestmark = [pytest.mark.anyio, pytest.mark.postgres]
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
FUTURE_NOW = datetime(2099, 8, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str | tuple[str, dict[str, object]]:
    if os.name == "nt":
        return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}
    return "asyncio"


@pytest.fixture
async def postgres_context():
    """Reuse the disposable messaging database fixture and its scoped cleanup."""
    async for context in _postgres_context.__wrapped__():
        yield context


def _supervisor(tenant_id: str):
    return make_scope(
        "supervisor", tenant_id=tenant_id, user_id="provider-ops-supervisor"
    )


def _request(
    request_id: str, reason: str = "transient_incident_resolved"
) -> ProviderRedriveRequest:
    return ProviderRedriveRequest(request_id=request_id, reason_code=reason)


async def _make_dead_outbox(
    pool,
    command_id: UUID,
    *,
    failure_kind: str = "network_error",
    outcome: str = "terminal_failure",
) -> None:
    """Create current-cycle terminal evidence without involving a provider."""
    attempt_id = uuid4()
    lease_id = uuid4()
    async with pool.connection() as connection:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT GREATEST(clock_timestamp(), created_at)
                    FROM integration.outbox_messages WHERE command_id = %s
                    """,
                    (command_id,),
                )
                now = (await cursor.fetchone())[0]
                await cursor.execute(
                    """
                    UPDATE integration.outbox_messages
                    SET status = 'dead', attempts_in_cycle = 1,
                        last_failure_kind = %s,
                        last_error_code = 'provider_technical_failure',
                        last_error_message = 'sensitive diagnostic',
                        dead_at = %s, updated_at = %s
                    WHERE command_id = %s
                    """,
                    (failure_kind, now, now, command_id),
                )
                assert cursor.rowcount == 1
                await cursor.execute(
                    """
                    INSERT INTO integration.outbox_delivery_attempts (
                        attempt_id, command_id, delivery_cycle, attempt_number,
                        lease_id, worker_id, started_at, finished_at, outcome,
                        failure_kind, safe_error_code, safe_error_message
                    ) VALUES (%s, %s, 1, 1, %s, 'worker-a', %s, %s, %s,
                              %s, 'provider_technical_failure',
                              'sensitive diagnostic')
                    """,
                    (
                        attempt_id,
                        command_id,
                        lease_id,
                        now,
                        now,
                        outcome,
                        failure_kind,
                    ),
                )


async def _support_case_command(pool, tenant_id: str, *, suffix: str):
    case = _delivery_case(
        tenant_id=tenant_id,
        order_id=f"ORD-{int(suffix):05d}",
        source_message_id=f"source-{suffix}",
    )
    command = _delivery_envelope(case)
    await PostgresCaseRepository(pool).create_case_with_event_and_command(
        _scope(tenant_id),
        case=case,
        event=_case_created_event(case),
        command=command,
    )
    await _make_dead_outbox(pool, command.command_id)
    return case, command


def _review_case(tenant_id: str) -> SupportCase:
    base = _delivery_case(
        tenant_id=tenant_id,
        order_id="ORD-80101",
        source_message_id="operation-review-source",
    )
    return SupportCase.model_validate(
        {
            **base.model_dump(),
            "case_type": "order_operation_review",
            "reason_codes": ("return_manual_amount_review",),
            "display_reason": "A Supervisor must review this operation.",
        }
    )


def _review_case_event(case: SupportCase) -> SupportCaseEvent:
    return SupportCaseEvent(
        event_id=uuid4(),
        idempotency_key=f"message:{case.thread_id}:{case.source_message_id}",
        case_id=case.case_id,
        event_type="case_created",
        source_message_id=case.source_message_id,
        order_id=case.order_id,
        reason_codes=("return_manual_amount_review",),
        triggering_message_excerpt="Review this operation.",
        current_priority=case.priority,
        current_status=case.status,
        actor="system",
        customer_id=case.customer_id,
        tenant_id=case.tenant_id,
        created_at=case.created_at,
    )


async def _manual_review_operation_command(pool, tenant_id: str):
    case = _review_case(tenant_id)
    await PostgresCaseRepository(pool).create_case_with_event(
        _scope(tenant_id), case=case, event=_review_case_event(case)
    )
    base = _operation(
        tenant_id=tenant_id,
        status="manual_review",
        order_id="ORD-80101",
        source_message_id="manual-review-operation",
    )
    operation = OrderOperation.model_validate(
        {
            **base.model_dump(),
            "requires_manual_review": True,
            "review_case_type": "order_operation_review",
            "review_priority": "p1",
            "support_case_id": case.case_id,
        }
    )
    await PostgresOrderOperationRepository(pool).create_operation_with_events(
        _scope(tenant_id), operation=operation, events=()
    )
    command = _envelope(operation)
    async with pool.connection() as connection:
        async with connection.transaction():
            async with connection.cursor() as cursor:
                await insert_outbox_message(
                    cursor,
                    command=command,
                    status="pending",
                    available_at=NOW,
                    now=NOW,
                )
    await _make_dead_outbox(pool, command.command_id)
    return case, operation, command


async def test_safe_reads_are_tenant_scoped_and_hide_sensitive_columns(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    case, command = await _support_case_command(pool, tenant_id, suffix="80111")
    repository = PostgresProviderOperationsRepository(pool)
    scope = _supervisor(tenant_id)
    async with pool.connection() as connection:
        await connection.execute(
            """
            UPDATE integration.outbox_messages
            SET last_error_code = 'SECRET invalid diagnostic code'
            WHERE tenant_id = %s AND command_id = %s
            """,
            (tenant_id, command.command_id),
        )
        await connection.execute(
            """
            UPDATE integration.outbox_delivery_attempts
            SET safe_error_code = 'SECRET invalid diagnostic code', http_status = 700
            WHERE command_id = %s
            """,
            (command.command_id,),
        )

    overview = await repository.get_queue_overview(scope)
    detail = await repository.get_outbox_detail(scope, command.command_id)

    assert any(row.status == "dead" and row.count >= 1 for row in overview.outbox)
    serialized = detail.model_dump(mode="json")
    assert serialized["aggregate_id"] == str(case.case_id)
    assert "payload" not in serialized
    assert "provider_connection_id" not in serialized
    assert "provider_reference" not in str(serialized)
    assert "sensitive diagnostic" not in str(serialized)
    assert "SECRET invalid diagnostic code" not in str(serialized)
    assert detail.last_error_code is None
    assert detail.attempts[0].safe_error_code is None
    assert detail.attempts[0].http_status is None

    with pytest.raises(ProviderOperationsNotFoundError):
        await repository.get_outbox_detail(
            _supervisor(f"{tenant_id}-other"), command.command_id
        )
    with pytest.raises(ProviderOperationsNotFoundError):
        await repository.get_outbox_detail(scope, uuid4())
    with pytest.raises(ValueError, match="between 1 and 100"):
        await repository.get_outbox_detail(scope, command.command_id, history_limit=101)
    forged = make_scope("support_agent", tenant_id=tenant_id).model_copy(
        update={"permissions": role_permissions("supervisor")}
    )
    with pytest.raises(ForbiddenError):
        await repository.get_queue_overview(forged)


async def test_support_case_redrive_is_idempotent_concurrent_and_case_immutable(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    case, command = await _support_case_command(pool, tenant_id, suffix="80112")
    repository = PostgresProviderOperationsRepository(pool)
    scope = _supervisor(tenant_id)
    request = _request("ops:support-case-same")
    async with pool.connection() as connection:
        before = await (
            await connection.execute(
                """
                SELECT status, priority, assigned_agent_id, version
                FROM case_management.support_cases WHERE case_id = %s
                """,
                (case.case_id,),
            )
        ).fetchone()

    first, second = await asyncio.gather(
        repository.redrive_outbox(scope, command.command_id, request),
        repository.redrive_outbox(scope, command.command_id, request),
    )

    assert first == second
    assert first.previous_cycle == 1 and first.new_cycle == 2
    async with pool.connection() as connection:
        after = await (
            await connection.execute(
                """
                SELECT status, priority, assigned_agent_id, version
                FROM case_management.support_cases WHERE case_id = %s
                """,
                (case.case_id,),
            )
        ).fetchone()
        audit_count = (
            await (
                await connection.execute(
                    """
                    SELECT count(*) FROM case_management.support_case_events
                    WHERE tenant_id = %s AND case_id = %s
                      AND event_type = 'provider_redrive'
                    """,
                    (tenant_id, case.case_id),
                )
            ).fetchone()
        )[0]
        redrive_count = (
            await (
                await connection.execute(
                    """
                    SELECT count(*) FROM integration.outbox_redrives
                    WHERE tenant_id = %s AND command_id = %s
                    """,
                    (tenant_id, command.command_id),
                )
            ).fetchone()
        )[0]
    assert after == before
    assert audit_count == 1
    assert redrive_count == 1

    with pytest.raises(ProviderOperationsConflictError, match="request_id_conflict"):
        await repository.redrive_outbox(
            scope,
            command.command_id,
            _request("ops:support-case-same", "manual_retry_approved"),
        )


async def test_concurrent_different_requests_recover_once(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    _case, command = await _support_case_command(pool, tenant_id, suffix="80113")
    repository = PostgresProviderOperationsRepository(pool)
    scope = _supervisor(tenant_id)

    results = await asyncio.gather(
        repository.redrive_outbox(scope, command.command_id, _request("ops:race-a")),
        repository.redrive_outbox(scope, command.command_id, _request("ops:race-b")),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    conflicts = [result for result in results if isinstance(result, Exception)]
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], ProviderOperationsConflictError)
    assert conflicts[0].code == "status_not_redrivable"


async def test_operation_redrive_coordinates_operation_case_event_and_outbox(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    case, operation, command = await _manual_review_operation_command(pool, tenant_id)
    repository = PostgresProviderOperationsRepository(pool)

    result = await repository.redrive_outbox(
        _supervisor(tenant_id),
        command.command_id,
        _request("ops:operation-review", "manual_retry_approved"),
    )

    assert result.new_cycle == 2
    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT status, requires_manual_review, review_case_type,
                       review_priority, support_case_id, version
                FROM case_management.order_operations
                WHERE tenant_id = %s AND operation_id = %s
                """,
                (tenant_id, operation.operation_id),
            )
            updated = await cursor.fetchone()
            await cursor.execute(
                """
                SELECT status, priority, assigned_agent_id, version
                FROM case_management.support_cases
                WHERE tenant_id = %s AND case_id = %s
                """,
                (tenant_id, case.case_id),
            )
            unchanged_case = await cursor.fetchone()
            await cursor.execute(
                """
                SELECT count(*) AS count
                FROM case_management.order_operation_events
                WHERE tenant_id = %s AND operation_id = %s
                  AND event_type = 'status_changed'
                  AND previous_status = 'manual_review'
                  AND current_status = 'queued'
                """,
                (tenant_id, operation.operation_id),
            )
            operation_events = (await cursor.fetchone())["count"]
            await cursor.execute(
                """
                SELECT status, delivery_cycle, attempts_in_cycle
                FROM integration.outbox_messages
                WHERE tenant_id = %s AND command_id = %s
                """,
                (tenant_id, command.command_id),
            )
            outbox = await cursor.fetchone()
    assert updated == {
        "status": "queued",
        "requires_manual_review": False,
        "review_case_type": None,
        "review_priority": None,
        "support_case_id": None,
        "version": operation.version + 1,
    }
    assert unchanged_case == {
        "status": case.status,
        "priority": case.priority,
        "assigned_agent_id": case.assigned_agent_id,
        "version": case.version,
    }
    assert operation_events == 1
    assert outbox == {
        "status": "retry_scheduled",
        "delivery_cycle": 2,
        "attempts_in_cycle": 0,
    }


async def test_future_outbox_timestamps_remain_monotonic_through_recovery(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    base_case = _delivery_case(tenant_id=tenant_id, order_id="ORD-80116")
    case = base_case.model_copy(
        update={"created_at": FUTURE_NOW, "updated_at": FUTURE_NOW}
    )
    event = _case_created_event(case).model_copy(update={"created_at": FUTURE_NOW})
    command = _delivery_envelope(case).model_copy(update={"created_at": FUTURE_NOW})
    integration = PostgresIntegrationRepository(pool)
    await PostgresCaseRepository(pool).create_case_with_event_and_command(
        _scope(tenant_id), case=case, event=event, command=command
    )
    await _make_dead_outbox(pool, command.command_id)

    await PostgresProviderOperationsRepository(pool).redrive_outbox(
        _supervisor(tenant_id), command.command_id, _request("ops:future-outbox")
    )
    redriven = await integration.get_outbox_message(command.command_id)
    assert redriven is not None
    assert redriven.status == "retry_scheduled"
    assert redriven.updated_at >= FUTURE_NOW

    claims = await integration.claim_due_outbox(
        worker_id="future-outbox-worker", batch_size=20, lease_seconds=60
    )
    claimed = next(item for item in claims if item.command_id == command.command_id)
    assert claimed.status == "processing"
    assert claimed.delivery_cycle == 2
    assert claimed.attempts_in_cycle == 1
    assert claimed.updated_at >= redriven.updated_at
    assert await integration.renew_outbox_lease(
        command_id=command.command_id,
        lease_id=claimed.lease_id,
        lease_owner="future-outbox-worker",
        lease_seconds=120,
    )
    renewed = await integration.get_outbox_message(command.command_id)
    assert renewed is not None
    assert renewed.updated_at >= claimed.updated_at

    async with pool.connection() as connection:
        await connection.execute(
            """
            UPDATE integration.outbox_messages
            SET lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE tenant_id = %s AND command_id = %s AND lease_id = %s
            """,
            (tenant_id, command.command_id, claimed.lease_id),
        )
    assert await integration.recover_expired_outbox_leases(batch_size=20) == 1
    retried = await integration.get_outbox_message(command.command_id)
    assert retried is not None
    assert retried.status == "retry_scheduled"
    assert retried.updated_at >= renewed.updated_at

    claims = await integration.claim_due_outbox(
        worker_id="future-outbox-worker", batch_size=20, lease_seconds=60
    )
    final_claim = next(item for item in claims if item.command_id == command.command_id)
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                UPDATE integration.outbox_delivery_attempts
                SET attempt_number = 8
                WHERE command_id = %s AND lease_id = %s
                """,
                (command.command_id, final_claim.lease_id),
            )
            await connection.execute(
                """
                UPDATE integration.outbox_messages
                SET attempts_in_cycle = 8,
                    lease_expires_at = clock_timestamp() - interval '1 second'
                WHERE tenant_id = %s AND command_id = %s AND lease_id = %s
                """,
                (tenant_id, command.command_id, final_claim.lease_id),
            )
    assert await integration.recover_expired_outbox_leases(batch_size=20) == 1
    exhausted = await integration.get_outbox_message(command.command_id)
    assert exhausted is not None
    assert exhausted.status == "dead"
    assert exhausted.updated_at >= final_claim.updated_at
    assert exhausted.dead_at is not None
    assert exhausted.dead_at >= FUTURE_NOW
    async with pool.connection() as connection:
        terminal_attempt = await (
            await connection.execute(
                """
                SELECT finished_at, started_at
                FROM integration.outbox_delivery_attempts
                WHERE command_id = %s AND lease_id = %s
                """,
                (command.command_id, final_claim.lease_id),
            )
        ).fetchone()
    assert terminal_attempt[0] >= terminal_attempt[1] >= FUTURE_NOW


async def test_future_outbox_acceptance_finalization_is_monotonic(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    base_case = _delivery_case(tenant_id=tenant_id, order_id="ORD-80118")
    case = base_case.model_copy(
        update={"created_at": FUTURE_NOW, "updated_at": FUTURE_NOW}
    )
    event = _case_created_event(case).model_copy(update={"created_at": FUTURE_NOW})
    command = _delivery_envelope(case).model_copy(update={"created_at": FUTURE_NOW})
    await PostgresCaseRepository(pool).create_case_with_event_and_command(
        _scope(tenant_id), case=case, event=event, command=command
    )
    async with pool.connection() as connection:
        await connection.execute(
            """
            UPDATE integration.outbox_messages
            SET available_at = clock_timestamp()
            WHERE command_id = %s
            """,
            (command.command_id,),
        )
    integration = PostgresIntegrationRepository(pool)
    claimed = (
        await integration.claim_due_outbox(
            worker_id="future-finalizer-worker", batch_size=1, lease_seconds=60
        )
    )[0]
    await PostgresOutboxFinalizer(pool).accepted(
        claimed=claimed,
        result=ProviderCommandResult(
            command_id=command.command_id,
            status="accepted",
            provider_reference="future-safe-reference",
            received_at=datetime.now(UTC),
        ),
    )
    published = await integration.get_outbox_message(command.command_id)
    assert published is not None
    assert published.status == "published"
    assert published.updated_at >= FUTURE_NOW
    assert published.published_at is not None
    assert published.published_at >= FUTURE_NOW
    async with pool.connection() as connection:
        attempt = await (
            await connection.execute(
                """
                SELECT finished_at, started_at
                FROM integration.outbox_delivery_attempts
                WHERE attempt_id = %s
                """,
                (claimed.attempt.attempt_id,),
            )
        ).fetchone()
    assert attempt[0] >= attempt[1] >= FUTURE_NOW


async def test_provider_rejection_is_never_redrivable(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    case = _delivery_case(tenant_id=tenant_id, order_id="ORD-80114")
    command = _delivery_envelope(case)
    await PostgresCaseRepository(pool).create_case_with_event_and_command(
        _scope(tenant_id), case=case, event=_case_created_event(case), command=command
    )
    await _make_dead_outbox(
        pool,
        command.command_id,
        failure_kind="provider_rejection",
        outcome="provider_rejected",
    )

    with pytest.raises(ProviderOperationsConflictError) as raised:
        await PostgresProviderOperationsRepository(pool).redrive_outbox(
            _supervisor(tenant_id), command.command_id, _request("ops:rejected")
        )
    assert raised.value.code == "provider_rejection"


async def test_operation_event_conflict_rolls_back_all_recovery_changes(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    case, operation, command = await _manual_review_operation_command(pool, tenant_id)
    request = _request("ops:forced-event-conflict")
    conflict_key = (
        f"provider-redrive:{tenant_id}:{command.command_id}:{request.request_id}"
    )
    async with pool.connection() as connection:
        await connection.execute(
            f"""
            INSERT INTO case_management.order_operation_events ({_OPERATION_EVENT_COLUMNS})
            VALUES ({", ".join(["%s"] * 12)})
            """,
            (
                uuid4(),
                conflict_key,
                operation.operation_id,
                "operation_created",
                None,
                None,
                None,
                None,
                "system",
                operation.customer_id,
                tenant_id,
                operation.created_at,
            ),
        )

    with pytest.raises(ProviderOperationsConflictError) as raised:
        await PostgresProviderOperationsRepository(pool).redrive_outbox(
            _supervisor(tenant_id), command.command_id, request
        )
    assert raised.value.code == "audit_conflict"

    async with pool.connection() as connection:
        operation_row = await (
            await connection.execute(
                """
                SELECT status, requires_manual_review, support_case_id
                FROM case_management.order_operations
                WHERE tenant_id = %s AND operation_id = %s
                """,
                (tenant_id, operation.operation_id),
            )
        ).fetchone()
        outbox_row = await (
            await connection.execute(
                """
                SELECT status, delivery_cycle FROM integration.outbox_messages
                WHERE tenant_id = %s AND command_id = %s
                """,
                (tenant_id, command.command_id),
            )
        ).fetchone()
        case_audits = (
            await (
                await connection.execute(
                    """
                    SELECT count(*) FROM case_management.support_case_events
                    WHERE tenant_id = %s AND case_id = %s
                      AND event_type = 'provider_redrive'
                    """,
                    (tenant_id, case.case_id),
                )
            ).fetchone()
        )[0]
        redrives = (
            await (
                await connection.execute(
                    """
                    SELECT count(*) FROM integration.outbox_redrives
                    WHERE tenant_id = %s AND command_id = %s
                    """,
                    (tenant_id, command.command_id),
                )
            ).fetchone()
        )[0]
    assert operation_row == ("manual_review", True, case.case_id)
    assert outbox_row == ("dead", 1)
    assert case_audits == 0
    assert redrives == 0


async def test_inbox_redrive_preserves_callback_and_reclaims_in_new_cycle(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    integration = PostgresIntegrationRepository(pool)
    repository = PostgresProviderOperationsRepository(pool)
    inbox_id = uuid4()
    command_id = uuid4()
    aggregate_id = uuid4()
    event = ProviderWebhookEventData(
        command_id=command_id,
        aggregate_type="order_operation",
        aggregate_id=aggregate_id,
        command_status="processing",
        provider_operation_id="sensitive-provider-operation",
        provider_reference="sensitive-provider-reference",
        order_id="ORD-80115",
        occurred_at=NOW,
    )
    body_hash = hashlib.sha256(b"immutable callback body").hexdigest()
    await integration.receive_inbox_idempotently(
        inbox_id=inbox_id,
        provider_connection_id="conn-sensitive",
        event_id="event-sensitive",
        tenant_id=tenant_id,
        event=event,
        raw_body_sha256=body_hash,
        received_at=NOW,
    )
    async with pool.connection() as connection:
        await connection.execute(
            """
            UPDATE integration.inbox_messages
            SET status = 'failed', processing_attempts = 5,
                attempts_in_cycle = 5,
                failed_at = GREATEST(clock_timestamp(), received_at),
                last_error_code = 'terminal_failure',
                last_error_message = 'sensitive diagnostic',
                updated_at = GREATEST(clock_timestamp(), received_at)
            WHERE inbox_id = %s
            """,
            (inbox_id,),
        )
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT provider_connection_id, event_id, tenant_id, schema_version,
                       event_type, command_id, aggregate_type, aggregate_id,
                       payload, raw_body_sha256, processing_attempts
                FROM integration.inbox_messages WHERE inbox_id = %s
                """,
                (inbox_id,),
            )
            original = await cursor.fetchone()

    result = await repository.redrive_inbox(
        _supervisor(tenant_id), inbox_id, _request("ops:inbox-cycle")
    )
    assert result.previous_cycle == 1 and result.new_cycle == 2
    detail = await repository.get_inbox_detail(_supervisor(tenant_id), inbox_id)
    assert detail.status == "received"
    assert detail.processing_cycle == 2
    assert detail.attempts_in_cycle == 0
    assert detail.total_attempts == 5
    assert "sensitive" not in str(detail.model_dump(mode="json"))

    async with pool.connection() as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT provider_connection_id, event_id, tenant_id, schema_version,
                       event_type, command_id, aggregate_type, aggregate_id,
                       payload, raw_body_sha256, processing_attempts
                FROM integration.inbox_messages WHERE inbox_id = %s
                """,
                (inbox_id,),
            )
            preserved = await cursor.fetchone()
    assert preserved == original

    claimed = await integration.claim_due_inbox(
        worker_id="inbox-worker-cycle-2", batch_size=20, lease_seconds=60
    )
    target = next(item for item in claimed if item.inbox_id == inbox_id)
    assert target.processing_cycle == 2
    assert target.attempts_in_cycle == 1
    assert target.processing_attempts == 6
    assert target.attempt.processing_cycle == 2
    assert target.attempt.attempt_number == 6

    replay = await repository.redrive_inbox(
        _supervisor(tenant_id), inbox_id, _request("ops:inbox-cycle")
    )
    assert replay == result
    with pytest.raises(ProviderOperationsConflictError, match="request_id_conflict"):
        await repository.redrive_inbox(
            _supervisor(tenant_id),
            inbox_id,
            _request("ops:inbox-cycle", "manual_retry_approved"),
        )
    with pytest.raises(ProviderOperationsNotFoundError):
        await repository.get_inbox_detail(_supervisor(f"{tenant_id}-other"), inbox_id)


async def test_future_inbox_timestamps_remain_monotonic_through_recovery(
    postgres_context,
) -> None:
    pool, tenant_id = postgres_context
    integration = PostgresIntegrationRepository(pool)
    inbox_id = uuid4()
    command_id = uuid4()
    event = ProviderWebhookEventData(
        command_id=command_id,
        aggregate_type="order_operation",
        aggregate_id=uuid4(),
        command_status="processing",
        provider_operation_id="future-provider-operation",
        provider_reference="future-provider-reference",
        order_id="ORD-80117",
        occurred_at=FUTURE_NOW,
    )
    await integration.receive_inbox_idempotently(
        inbox_id=inbox_id,
        provider_connection_id="future-connection",
        event_id="future-event",
        tenant_id=tenant_id,
        event=event,
        raw_body_sha256=hashlib.sha256(b"future callback").hexdigest(),
        received_at=FUTURE_NOW,
    )
    async with pool.connection() as connection:
        await connection.execute(
            """
            UPDATE integration.inbox_messages
            SET status = 'failed', processing_attempts = 5,
                attempts_in_cycle = 5, failed_at = received_at,
                last_error_code = 'terminal_failure', updated_at = received_at
            WHERE tenant_id = %s AND inbox_id = %s
            """,
            (tenant_id, inbox_id),
        )

    await PostgresProviderOperationsRepository(pool).redrive_inbox(
        _supervisor(tenant_id), inbox_id, _request("ops:future-inbox")
    )
    redriven = await integration.get_inbox_message(inbox_id)
    assert redriven is not None
    assert redriven.status == "received"
    assert redriven.processing_cycle == 2
    assert redriven.updated_at >= FUTURE_NOW

    claims = await integration.claim_due_inbox(
        worker_id="future-inbox-worker", batch_size=20, lease_seconds=60
    )
    claimed = next(item for item in claims if item.inbox_id == inbox_id)
    assert claimed.status == "processing"
    assert claimed.processing_cycle == 2
    assert claimed.attempts_in_cycle == 1
    assert claimed.updated_at >= redriven.updated_at
    assert await integration.renew_inbox_lease(
        inbox_id=inbox_id,
        lease_id=claimed.lease_id,
        lease_owner="future-inbox-worker",
        lease_seconds=120,
    )
    renewed = await integration.get_inbox_message(inbox_id)
    assert renewed is not None
    assert renewed.updated_at >= claimed.updated_at

    async with pool.connection() as connection:
        await connection.execute(
            """
            UPDATE integration.inbox_messages
            SET lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE tenant_id = %s AND inbox_id = %s AND lease_id = %s
            """,
            (tenant_id, inbox_id, claimed.lease_id),
        )
    assert await integration.recover_expired_inbox_leases(batch_size=20) == 1
    retried = await integration.get_inbox_message(inbox_id)
    assert retried is not None
    assert retried.status == "received"
    assert retried.updated_at >= renewed.updated_at

    claims = await integration.claim_due_inbox(
        worker_id="future-inbox-worker", batch_size=20, lease_seconds=60
    )
    final_claim = next(item for item in claims if item.inbox_id == inbox_id)
    async with pool.connection() as connection:
        await connection.execute(
            """
            UPDATE integration.inbox_messages
            SET attempts_in_cycle = 5,
                lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE tenant_id = %s AND inbox_id = %s AND lease_id = %s
            """,
            (tenant_id, inbox_id, final_claim.lease_id),
        )
    assert await integration.recover_expired_inbox_leases(batch_size=20) == 1
    exhausted = await integration.get_inbox_message(inbox_id)
    assert exhausted is not None
    assert exhausted.status == "failed"
    assert exhausted.updated_at >= final_claim.updated_at
    assert exhausted.failed_at is not None
    assert exhausted.failed_at >= FUTURE_NOW
    async with pool.connection() as connection:
        terminal_attempt = await (
            await connection.execute(
                """
                SELECT finished_at, started_at
                FROM integration.inbox_processing_attempts
                WHERE inbox_id = %s AND lease_id = %s
                """,
                (inbox_id, final_claim.lease_id),
            )
        ).fetchone()
    assert terminal_attempt[0] >= terminal_attempt[1] >= FUTURE_NOW


async def test_migration_0008_backfills_cycles_and_suppresses_unsafe_legacy_audit() -> (
    None
):
    """Apply 0008 after a real 0001-0007 schema in an isolated test database."""
    source = os.getenv("CASE_TEST_POSTGRES_URI")
    if not source:
        pytest.skip("CASE_TEST_POSTGRES_URI is not configured")
    settings = conninfo_to_dict(source)
    database_name = f"provider_ops_migration_{uuid4().hex}"
    admin_settings = {**settings, "dbname": "postgres"}
    admin_conninfo = make_conninfo(**admin_settings)
    test_conninfo = make_conninfo(**{**settings, "dbname": database_name})
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    try:
        migrations = discover_migrations()
        with psycopg.connect(test_conninfo, autocommit=True) as connection:
            for migration in migrations:
                if migration.version <= "0007":
                    connection.execute(migration.sql)
            command_id = uuid4()
            inbox_id = uuid4()
            unsafe_request_id = " legacy request " + ("x" * 180)
            connection.execute(
                """
                INSERT INTO integration.outbox_messages (
                    command_id, schema_version, idempotency_key, tenant_id,
                    customer_id, source_message_id, provider_connection_id,
                    provider_capability, command_type, aggregate_type,
                    aggregate_id, expected_order_version, payload, status,
                    delivery_cycle, attempts_in_cycle, available_at,
                    created_at, updated_at
                ) VALUES (%s, 1, 'legacy-key', 'tenant-legacy', 'customer-a',
                          'source-a', 'connection-a', 'order_operation',
                          'return_order', 'order_operation', %s, 1, '{}'::jsonb,
                          'pending', 1, 0, now(), now(), now())
                """,
                (command_id, uuid4()),
            )
            connection.execute(
                """
                INSERT INTO integration.outbox_redrives (
                    redrive_id, command_id, tenant_id, request_id, requested_by,
                    reason, previous_cycle, new_cycle, created_at
                ) VALUES (%s, %s, 'tenant-legacy', %s, 'legacy-user',
                          'legacy free form reason', 1, 2, now())
                """,
                (uuid4(), command_id, unsafe_request_id),
            )
            connection.execute(
                """
                INSERT INTO integration.inbox_messages (
                    inbox_id, provider_connection_id, event_id, tenant_id,
                    schema_version, event_type, command_id, aggregate_type,
                    aggregate_id, payload, raw_body_sha256, status,
                    processing_attempts, available_at, received_at, updated_at,
                    failed_at
                ) VALUES (%s, 'connection-a', 'event-a', 'tenant-legacy', 1,
                          'provider_command_status_changed', %s,
                          'order_operation', %s, '{}'::jsonb, %s, 'failed', 3,
                          now(), now(), now(), now())
                """,
                (inbox_id, command_id, uuid4(), "a" * 64),
            )
            connection.execute(
                """
                INSERT INTO integration.inbox_processing_attempts (
                    attempt_id, inbox_id, attempt_number, lease_id, worker_id,
                    started_at, finished_at, outcome
                ) VALUES (%s, %s, 3, %s, 'worker-a', now(), now(),
                          'terminal_failure')
                """,
                (uuid4(), inbox_id, uuid4()),
            )
            migration_0008 = next(item for item in migrations if item.version == "0008")
            connection.execute(migration_0008.sql)
            inbox_row = connection.execute(
                """
                SELECT processing_cycle, attempts_in_cycle, processing_attempts
                FROM integration.inbox_messages WHERE inbox_id = %s
                """,
                (inbox_id,),
            ).fetchone()
            attempt_cycle = connection.execute(
                """
                SELECT processing_cycle FROM integration.inbox_processing_attempts
                WHERE inbox_id = %s
                """,
                (inbox_id,),
            ).fetchone()[0]
            legacy = connection.execute(
                """
                SELECT reason, reason_code FROM integration.outbox_redrives
                WHERE command_id = %s
                """,
                (command_id,),
            ).fetchone()
        assert inbox_row == (1, 3, 3)
        assert attempt_cycle == 1
        assert legacy == ("legacy free form reason", None)

        pool = create_async_connection_pool(test_conninfo, min_size=1, max_size=2)
        await pool.open()
        try:
            repository = PostgresProviderOperationsRepository(pool)
            detail = await repository.get_outbox_detail(
                _supervisor("tenant-legacy"), command_id
            )
            assert len(detail.redrives) == 1
            assert detail.redrives[0].request_id is None
            assert detail.redrives[0].reason_code is None

            app = FastAPI()
            app.include_router(router)
            register_provider_operations_exception_handlers(app)
            app.state.provider_operations_service = ProviderOperationsService(
                repository
            )
            app.dependency_overrides[require_access_scope] = lambda: _supervisor(
                "tenant-legacy"
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    f"/internal/provider-operations/outbox/{command_id}"
                )
            assert response.status_code == 200
            assert response.json()["redrives"][0]["request_id"] is None
            assert unsafe_request_id not in response.text
            assert "legacy free form reason" not in response.text
        finally:
            await pool.close()
    finally:
        with psycopg.connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )
