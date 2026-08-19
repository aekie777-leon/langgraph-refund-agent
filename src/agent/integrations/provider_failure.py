"""Atomic manual-review fallback for unavailable outbound provider configuration."""

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID, uuid4

from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.auth.models import AccessScope
from agent.cases.models import SupportCase, SupportCaseEvent
from agent.cases.postgres_repository import (
    _CASE_COLUMNS,
    _case_from_row,
    _case_values,
)
from agent.cases.postgres_repository import (
    _EVENT_COLUMNS as CASE_EVENT_COLUMNS,
)
from agent.cases.postgres_repository import (
    _event_values as case_event_values,
)
from agent.operations.models import OrderOperation, OrderOperationEvent
from agent.operations.postgres_repository import (
    _EVENT_COLUMNS as OP_EVENT_COLUMNS,
)
from agent.operations.postgres_repository import (
    _OPERATION_COLUMNS,
    _event_values,
    _operation_from_row,
)
from agent.operations.repository import (
    ConcurrentOperationUpdateError,
    OperationNotFoundError,
    OperationPersistenceError,
)


@dataclass(frozen=True)
class ProviderQueueFailureResult:
    """Return the atomically persisted operation and review case."""

    operation: OrderOperation
    support_case: SupportCase
    action: Literal["created", "reused", "duplicate_ignored"]


class ProviderQueueFailureCoordinator(Protocol):
    """Define the narrow cross-aggregate provider-failure boundary."""

    async def move_to_manual_review(
        self, scope: AccessScope, *, operation_id: UUID, request_id: str
    ) -> ProviderQueueFailureResult:
        """Persist a confirmed provider-configuration fallback."""
        ...


class PostgresProviderQueueFailureCoordinator:
    """Coordinate operation, review-case, and audit writes in one transaction."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Store the application-owned database pool."""
        self._pool = pool

    async def move_to_manual_review(
        self, scope: AccessScope, *, operation_id: UUID, request_id: str
    ) -> ProviderQueueFailureResult:
        """Move one pending operation to review with a fenced case transaction."""
        request_id = request_id.strip()
        if not request_id:
            raise ValueError("request_id must be non-empty")
        for attempt in range(3):
            try:
                return await self._move_once(
                    scope, operation_id=operation_id, request_id=request_id
                )
            except errors.UniqueViolation as error:
                if (
                    error.diag.constraint_name
                    not in {
                        "uq_support_cases_active_thread_type",
                    }
                    or attempt == 2
                ):
                    raise OperationPersistenceError(
                        "Could not create provider manual-review case"
                    ) from error
            except PoolTimeout as error:
                raise OperationPersistenceError(
                    "Timed out acquiring PostgreSQL connection"
                ) from error
            except errors.DatabaseError as error:
                raise OperationPersistenceError(
                    "Could not persist provider manual-review fallback"
                ) from error
        raise AssertionError("provider manual-review retry loop exhausted")

    async def _move_once(
        self, scope: AccessScope, *, operation_id: UUID, request_id: str
    ) -> ProviderQueueFailureResult:
        """Execute one fully atomic attempt, allowing the caller to retry races."""
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        confirmation_key = (
            f"provider-queue-failure:{operation_id}:confirmed:{request_id}"
        )
        async with self._pool.connection() as connection:
            async with connection.transaction():
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        f"SELECT {_OPERATION_COLUMNS} FROM case_management.order_operations WHERE operation_id = %s FOR UPDATE",
                        (operation_id,),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise OperationNotFoundError(str(operation_id))
                    operation = _operation_from_row(row)
                    if (
                        operation.tenant_id != scope.tenant_id
                        or operation.customer_id != scope.customer_id
                    ):
                        raise OperationNotFoundError(str(operation_id))
                    await cursor.execute(
                        "SELECT 1 FROM case_management.order_operation_events WHERE idempotency_key = %s",
                        (confirmation_key,),
                    )
                    replay = await cursor.fetchone() is not None
                    if (
                        operation.status == "manual_review"
                        and operation.support_case_id is not None
                    ):
                        await cursor.execute(
                            f"SELECT {_CASE_COLUMNS} FROM case_management.support_cases WHERE case_id = %s",
                            (operation.support_case_id,),
                        )
                        case_row = await cursor.fetchone()
                        if case_row is None:
                            raise ValueError(
                                "operation manual-review case is unavailable"
                            )
                        case = _case_from_row(case_row)
                        if (
                            case.tenant_id != operation.tenant_id
                            or case.customer_id != operation.customer_id
                            or case.case_type != "order_operation_review"
                        ):
                            raise ValueError(
                                "operation manual-review case association is invalid"
                            )
                        return ProviderQueueFailureResult(
                            operation, case, "duplicate_ignored"
                        )
                    if operation.status != "pending_confirmation":
                        raise ValueError(
                            "operation is not eligible for provider manual review"
                        )
                    await cursor.execute(
                        f"SELECT {_CASE_COLUMNS} FROM case_management.support_cases WHERE tenant_id = %s AND thread_id = %s AND case_type = 'order_operation_review' AND status IN ('open','in_progress','on_hold') FOR UPDATE",
                        (operation.tenant_id, operation.thread_id),
                    )
                    case_row = await cursor.fetchone()
                    action: Literal["created", "reused", "duplicate_ignored"]
                    if case_row is None:
                        case = SupportCase(
                            case_id=uuid4(),
                            thread_id=operation.thread_id,
                            source_message_id=operation.source_message_id,
                            order_id=operation.order_id,
                            case_type="order_operation_review",
                            priority="p1",
                            reason_codes=("provider_delivery_failed",),
                            display_reason="Provider delivery failed and requires human review.",
                            triggering_message_excerpt=operation.request_excerpt,
                            created_at=now,
                            updated_at=now,
                            customer_id=operation.customer_id,
                            tenant_id=operation.tenant_id,
                            created_by="system",
                        )
                        case_event = SupportCaseEvent(
                            event_id=uuid4(),
                            idempotency_key=f"provider-queue-failure:{operation_id}:case:{request_id}",
                            case_id=case.case_id,
                            event_type="case_created",
                            source_message_id=case.source_message_id,
                            order_id=case.order_id,
                            reason_codes=case.reason_codes,
                            triggering_message_excerpt=case.triggering_message_excerpt,
                            current_priority="p1",
                            current_status="open",
                            customer_id=case.customer_id,
                            tenant_id=case.tenant_id,
                            created_at=now,
                        )
                        await cursor.execute(
                            f"INSERT INTO case_management.support_cases ({_CASE_COLUMNS}) VALUES ({', '.join(['%s'] * 20)})",
                            _case_values(case),
                        )
                        await cursor.execute(
                            f"INSERT INTO case_management.support_case_events ({CASE_EVENT_COLUMNS}) VALUES ({', '.join(['%s'] * 25)})",
                            case_event_values(case_event),
                        )
                        action = "created"
                    else:
                        existing = _case_from_row(case_row)
                        reasons = tuple(
                            dict.fromkeys(
                                (*existing.reason_codes, "provider_delivery_failed")
                            )
                        )
                        case = existing.model_copy(
                            update={
                                "priority": "p1",
                                "reason_codes": reasons,
                                "updated_at": now,
                                "version": existing.version + 1,
                            }
                        )
                        await cursor.execute(
                            "UPDATE case_management.support_cases SET priority=%s, reason_codes=%s, updated_at=%s, version=%s WHERE case_id=%s AND version=%s",
                            (
                                case.priority,
                                list(case.reason_codes),
                                now,
                                case.version,
                                case.case_id,
                                existing.version,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise ConcurrentOperationUpdateError(
                                str(operation.operation_id)
                            )
                        case_event = SupportCaseEvent(
                            event_id=uuid4(),
                            idempotency_key=f"provider-queue-failure:{operation_id}:case:{request_id}",
                            case_id=case.case_id,
                            event_type="trigger_appended",
                            source_message_id=operation.source_message_id,
                            order_id=operation.order_id,
                            reason_codes=("provider_delivery_failed",),
                            triggering_message_excerpt=operation.request_excerpt,
                            previous_priority=existing.priority,
                            current_priority=case.priority,
                            current_status=case.status,
                            actor="system",
                            customer_id=case.customer_id,
                            tenant_id=case.tenant_id,
                            created_at=now,
                        )
                        await cursor.execute(
                            f"INSERT INTO case_management.support_case_events ({CASE_EVENT_COLUMNS}) VALUES ({', '.join(['%s'] * 25)})",
                            case_event_values(case_event),
                        )
                        action = "reused"
                    updated = operation.model_copy(
                        update={
                            "status": "manual_review",
                            "requires_manual_review": True,
                            "review_case_type": "order_operation_review",
                            "review_priority": "p1",
                            "support_case_id": case.case_id,
                            "updated_at": now,
                            "version": operation.version + 1,
                        }
                    )
                    await cursor.execute(
                        "UPDATE case_management.order_operations SET requires_manual_review=%s, review_case_type=%s, review_priority=%s, support_case_id=%s, status=%s, updated_at=%s, version=%s WHERE operation_id=%s AND version=%s",
                        (
                            True,
                            "order_operation_review",
                            "p1",
                            case.case_id,
                            "manual_review",
                            now,
                            updated.version,
                            operation.operation_id,
                            operation.version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConcurrentOperationUpdateError(
                            str(operation.operation_id)
                        )
                    if not replay:
                        events = (
                            OrderOperationEvent(
                                event_id=uuid4(),
                                idempotency_key=confirmation_key,
                                operation_id=operation.operation_id,
                                event_type="confirmation_recorded",
                                actor=scope.identity,
                                customer_id=operation.customer_id,
                                tenant_id=operation.tenant_id,
                                created_at=now,
                            ),
                            OrderOperationEvent(
                                event_id=uuid4(),
                                idempotency_key=f"provider-queue-failure:{operation_id}:status:{request_id}",
                                operation_id=operation.operation_id,
                                event_type="status_changed",
                                previous_status=operation.status,
                                current_status="manual_review",
                                actor="system",
                                customer_id=operation.customer_id,
                                tenant_id=operation.tenant_id,
                                created_at=now,
                            ),
                            OrderOperationEvent(
                                event_id=uuid4(),
                                idempotency_key=f"provider-queue-failure:{operation_id}:case-attached:{request_id}",
                                operation_id=operation.operation_id,
                                event_type="support_case_attached",
                                support_case_id=case.case_id,
                                actor="system",
                                customer_id=operation.customer_id,
                                tenant_id=operation.tenant_id,
                                created_at=now,
                            ),
                        )
                        for event in events:
                            await cursor.execute(
                                f"INSERT INTO case_management.order_operation_events ({OP_EVENT_COLUMNS}) VALUES ({', '.join(['%s'] * 12)})",
                                _event_values(event),
                            )
                    return ProviderQueueFailureResult(updated, case, action)
