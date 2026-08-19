"""PostgreSQL atomic finalization for dispatched provider commands."""

from typing import Literal
from uuid import uuid4

from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.cases.models import SupportCase, SupportCaseEvent
from agent.cases.postgres_repository import (
    _CASE_COLUMNS,
    _case_from_row,
    _case_values,
)
from agent.cases.postgres_repository import (
    _EVENT_COLUMNS as _CASE_EVENT_COLUMNS,
)
from agent.cases.postgres_repository import (
    _event_values as _case_event_values,
)
from agent.integrations.models import ProviderCommandResult
from agent.integrations.outbox_worker import OutboxFinalizer
from agent.integrations.persistence_models import ClaimedOutboxMessage
from agent.integrations.postgres_writes import (
    finish_delivery_attempt,
    update_outbox_transition,
)
from agent.integrations.repository import (
    IntegrationPersistenceError,
    LeaseConflictError,
)
from agent.operations.models import OrderOperationEvent
from agent.operations.postgres_repository import (
    _EVENT_COLUMNS as _OPERATION_EVENT_COLUMNS,
)
from agent.operations.postgres_repository import (
    _OPERATION_COLUMNS,
    _operation_from_row,
)
from agent.operations.postgres_repository import (
    _event_values as _operation_event_values,
)


class PostgresOutboxFinalizer(OutboxFinalizer):
    """Finalize domain aggregates, attempts, and outbox rows in one transaction."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Store the application-owned PostgreSQL pool."""
        self._pool = pool

    @staticmethod
    async def _lock_claimed_command(cursor, claimed):
        """Lock Outbox then attempt and return one monotonic database time.

        Provider-operations redrive takes the same lock order.  Validating the
        persisted delivery cycle before touching the aggregate fences a stale
        finalizer after a manual redrive has opened a newer cycle.
        """
        await cursor.execute(
            """
            SELECT status, delivery_cycle, attempts_in_cycle, lease_id,
                   lease_owner, lease_expires_at, created_at, updated_at
            FROM integration.outbox_messages
            WHERE command_id = %s
            FOR UPDATE
            """,
            (claimed.command_id,),
        )
        message = await cursor.fetchone()
        await cursor.execute("SELECT clock_timestamp() AS now")
        clock_row = await cursor.fetchone()
        if message is None or clock_row is None:
            raise LeaseConflictError(str(claimed.command_id))
        database_now = clock_row["now"]
        if (
            message["status"] != "processing"
            or message["delivery_cycle"] != claimed.delivery_cycle
            or message["attempts_in_cycle"] != claimed.attempts_in_cycle
            or message["lease_id"] != claimed.lease_id
            or message["lease_owner"] != claimed.lease_owner
            or message["lease_expires_at"] is None
            or message["lease_expires_at"] <= database_now
        ):
            raise LeaseConflictError(str(claimed.command_id))
        await cursor.execute(
            """
            SELECT delivery_cycle, attempt_number, lease_id, worker_id,
                   started_at
            FROM integration.outbox_delivery_attempts
            WHERE attempt_id = %s AND command_id = %s AND finished_at IS NULL
            FOR UPDATE
            """,
            (claimed.attempt.attempt_id, claimed.command_id),
        )
        attempt = await cursor.fetchone()
        if (
            attempt is None
            or attempt["delivery_cycle"] != claimed.delivery_cycle
            or attempt["delivery_cycle"] != claimed.attempt.delivery_cycle
            or attempt["attempt_number"] != claimed.attempts_in_cycle
            or attempt["attempt_number"] != claimed.attempt.attempt_number
            or attempt["lease_id"] != claimed.lease_id
            or attempt["worker_id"] != claimed.lease_owner
        ):
            raise LeaseConflictError(str(claimed.command_id))
        return max(
            database_now,
            message["created_at"],
            message["updated_at"],
            attempt["started_at"],
        )

    async def accepted(
        self, *, claimed: ClaimedOutboxMessage, result: ProviderCommandResult
    ) -> None:
        """Persist immediate provider acceptance and publish the outbox command."""
        await self._finalize(
            claimed=claimed,
            target="published",
            provider_result=result,
            failure_kind=None,
            error_code=None,
            error_message=None,
        )

    async def rejected(
        self, *, claimed: ClaimedOutboxMessage, result: ProviderCommandResult
    ) -> None:
        """Persist an explicit business rejection as a terminal provider result."""
        await self._finalize(
            claimed=claimed,
            target="dead",
            provider_result=result,
            failure_kind="provider_rejection",
            error_code="provider_rejected",
            error_message="Provider rejected the command.",
        )

    async def terminal_failure(
        self,
        *,
        claimed: ClaimedOutboxMessage,
        failure_kind: str,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """Persist a terminal technical failure and its required human handoff."""
        await self._finalize(
            claimed=claimed,
            target="dead",
            provider_result=None,
            failure_kind=failure_kind,
            error_code=error_code,
            error_message=error_message,
        )

    async def _finalize(
        self,
        *,
        claimed: ClaimedOutboxMessage,
        target: Literal["published", "dead"],
        provider_result: ProviderCommandResult | None,
        failure_kind: str | None,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """Apply one fenced aggregate transition and attempt/outbox completion."""
        assert claimed.lease_id is not None and claimed.lease_owner is not None
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        now = await self._lock_claimed_command(cursor, claimed)
                        if claimed.aggregate_type == "order_operation":
                            await self._finalize_operation(
                                cursor=cursor,
                                claimed=claimed,
                                target=target,
                                provider_result=provider_result,
                                failure_kind=failure_kind,
                                now=now,
                            )
                        else:
                            await self._finalize_case(
                                cursor=cursor,
                                claimed=claimed,
                                target=target,
                                provider_result=provider_result,
                                failure_kind=failure_kind,
                                now=now,
                            )
                        outcome: Literal[
                            "accepted", "provider_rejected", "terminal_failure"
                        ] = (
                            "accepted"
                            if target == "published"
                            else (
                                "provider_rejected"
                                if failure_kind == "provider_rejection"
                                else "terminal_failure"
                            )
                        )
                        attempt_affected = await finish_delivery_attempt(
                            cursor,
                            attempt_id=claimed.attempt.attempt_id,
                            command_id=claimed.command_id,
                            lease_id=claimed.lease_id,
                            worker_id=claimed.lease_owner or "",
                            finished_at=now,
                            outcome=outcome,
                            failure_kind=failure_kind,
                            safe_error_code=error_code,
                            safe_error_message=error_message,
                            provider_operation_id=(
                                provider_result.provider_operation_id
                                if provider_result is not None
                                else None
                            ),
                            provider_reference=(
                                provider_result.provider_reference
                                if provider_result is not None
                                else None
                            ),
                        )
                        if attempt_affected != 1:
                            raise LeaseConflictError(str(claimed.command_id))
                        affected = await update_outbox_transition(
                            cursor,
                            command_id=claimed.command_id,
                            expected_status="processing",
                            expected_lease_id=claimed.lease_id,
                            expected_lease_owner=claimed.lease_owner or "",
                            target_status=target,
                            updated_at=now,
                            published_at=now if target == "published" else None,
                            dead_at=now if target == "dead" else None,
                            last_failure_kind=failure_kind,
                            last_error_code=error_code,
                            last_error_message=error_message,
                        )
                        if affected != 1:
                            raise LeaseConflictError(str(claimed.command_id))
        except LeaseConflictError:
            raise
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError(
                "Failed to finalize provider command"
            ) from error

    async def _finalize_operation(
        self, *, cursor, claimed, target, provider_result, failure_kind, now
    ) -> None:
        """Update an order operation, and create its technical-review case when needed."""
        await cursor.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM case_management.order_operations WHERE operation_id = %s FOR UPDATE",
            (claimed.aggregate_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("provider command operation was not found")
        operation = _operation_from_row(row)
        if operation.tenant_id != claimed.tenant_id or operation.status != "queued":
            raise ValueError("provider command operation association is invalid")
        if target == "published":
            new_status: Literal["submitted", "rejected", "manual_review"] = "submitted"
            support_case_id = operation.support_case_id
            requires_manual = operation.requires_manual_review
            review_case_type = operation.review_case_type
            review_priority = operation.review_priority
        elif failure_kind == "provider_rejection":
            new_status = "rejected"
            support_case_id = operation.support_case_id
            requires_manual = operation.requires_manual_review
            review_case_type = operation.review_case_type
            review_priority = operation.review_priority
        else:
            new_status = "manual_review"
            requires_manual = True
            review_case_type = "order_operation_review"
            review_priority = "p1"
            support_case_id = operation.support_case_id
            if support_case_id is None:
                support_case_id = await self._ensure_technical_review_case(
                    cursor=cursor,
                    claimed=claimed,
                    operation=operation,
                    now=now,
                )
        reference = (
            provider_result.provider_reference
            if provider_result is not None
            else operation.provider_reference
        )
        await cursor.execute(
            """
            UPDATE case_management.order_operations
            SET requires_manual_review = %s, review_case_type = %s, review_priority = %s,
                support_case_id = %s, provider_reference = %s, status = %s,
                updated_at = GREATEST(%s, updated_at, created_at), version = %s
            WHERE operation_id = %s AND version = %s
            """,
            (
                requires_manual,
                review_case_type,
                review_priority,
                support_case_id,
                reference,
                new_status,
                now,
                operation.version + 1,
                operation.operation_id,
                operation.version,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseConflictError(str(claimed.command_id))
        event = OrderOperationEvent(
            event_id=uuid4(),
            idempotency_key=(
                f"provider-command:{claimed.command_id}:cycle:"
                f"{claimed.delivery_cycle}:status"
            ),
            operation_id=operation.operation_id,
            event_type="status_changed",
            previous_status=operation.status,
            current_status=new_status,
            provider_reference=reference,
            actor="system",
            customer_id=operation.customer_id,
            tenant_id=operation.tenant_id,
            created_at=now,
        )
        await cursor.execute(
            f"INSERT INTO case_management.order_operation_events ({_OPERATION_EVENT_COLUMNS}) VALUES ({', '.join(['%s'] * 12)})",
            _operation_event_values(event),
        )
        if new_status == "manual_review" and support_case_id is not None:
            attached = OrderOperationEvent(
                event_id=uuid4(),
                idempotency_key=(
                    f"provider-command:{claimed.command_id}:cycle:"
                    f"{claimed.delivery_cycle}:case-attached"
                ),
                operation_id=operation.operation_id,
                event_type="support_case_attached",
                support_case_id=support_case_id,
                actor="system",
                customer_id=operation.customer_id,
                tenant_id=operation.tenant_id,
                created_at=now,
            )
            await cursor.execute(
                f"INSERT INTO case_management.order_operation_events ({_OPERATION_EVENT_COLUMNS}) VALUES ({', '.join(['%s'] * 12)})",
                _operation_event_values(attached),
            )

    async def _ensure_technical_review_case(self, *, cursor, claimed, operation, now):
        """Create or reuse the active technical-review case under the operation lock."""
        await cursor.execute(
            f"""
            SELECT {_CASE_COLUMNS}
            FROM case_management.support_cases
            WHERE tenant_id = %s AND thread_id = %s
              AND case_type = 'order_operation_review'
              AND status IN ('open', 'in_progress', 'on_hold')
            FOR UPDATE
            """,
            (operation.tenant_id, operation.thread_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            existing = _case_from_row(row)
            reason_codes = tuple(
                dict.fromkeys((*existing.reason_codes, "provider_delivery_failed"))
            )
            await cursor.execute(
                """
                UPDATE case_management.support_cases
                SET priority = 'p1', reason_codes = %s,
                    updated_at = GREATEST(%s, updated_at, created_at), version = %s
                WHERE case_id = %s AND version = %s
                """,
                (
                    list(reason_codes),
                    now,
                    existing.version + 1,
                    existing.case_id,
                    existing.version,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseConflictError(str(claimed.command_id))
            event = SupportCaseEvent(
                event_id=uuid4(),
                idempotency_key=(
                    f"provider-command:{claimed.command_id}:cycle:"
                    f"{claimed.delivery_cycle}:technical-review"
                ),
                case_id=existing.case_id,
                event_type="trigger_appended",
                source_message_id=operation.source_message_id,
                order_id=operation.order_id,
                reason_codes=("provider_delivery_failed",),
                triggering_message_excerpt=operation.request_excerpt,
                previous_priority=existing.priority,
                current_priority="p1",
                current_status=existing.status,
                actor="system",
                customer_id=existing.customer_id,
                tenant_id=existing.tenant_id,
                created_at=now,
            )
            await cursor.execute(
                f"INSERT INTO case_management.support_case_events ({_CASE_EVENT_COLUMNS}) VALUES ({', '.join(['%s'] * 25)}) ON CONFLICT (idempotency_key) DO NOTHING",
                _case_event_values(event),
            )
            return existing.case_id

        case_id = uuid4()
        """Create the required P1 technical-review case in the same transaction."""
        case = SupportCase(
            case_id=case_id,
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
        event = SupportCaseEvent(
            event_id=uuid4(),
            idempotency_key=(
                f"provider-command:{claimed.command_id}:cycle:"
                f"{claimed.delivery_cycle}:technical-review"
            ),
            case_id=case.case_id,
            event_type="case_created",
            source_message_id=case.source_message_id,
            order_id=case.order_id,
            reason_codes=case.reason_codes,
            triggering_message_excerpt=case.triggering_message_excerpt,
            current_priority=case.priority,
            current_status=case.status,
            customer_id=case.customer_id,
            tenant_id=case.tenant_id,
            created_at=now,
        )
        await cursor.execute(
            f"INSERT INTO case_management.support_cases ({_CASE_COLUMNS}) VALUES ({', '.join(['%s'] * 20)})",
            _case_values(case),
        )
        await cursor.execute(
            f"INSERT INTO case_management.support_case_events ({_CASE_EVENT_COLUMNS}) VALUES ({', '.join(['%s'] * 25)})",
            _case_event_values(event),
        )
        return case_id

    async def _finalize_case(
        self, *, cursor, claimed, target, provider_result, failure_kind, now
    ) -> None:
        """Append a provider update to the delivery-investigation aggregate."""
        await cursor.execute(
            f"SELECT {_CASE_COLUMNS} FROM case_management.support_cases WHERE case_id = %s FOR UPDATE",
            (claimed.aggregate_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("provider command case was not found")
        case = _case_from_row(row)
        if (
            case.tenant_id != claimed.tenant_id
            or case.case_type != "delivery_investigation"
        ):
            raise ValueError("provider command case association is invalid")
        status: Literal["accepted", "rejected"] = (
            "accepted" if target == "published" else "rejected"
        )
        reason_codes = case.reason_codes
        if target == "dead" and "provider_delivery_failed" not in reason_codes:
            reason_codes = (*reason_codes, "provider_delivery_failed")
            await cursor.execute(
                """
                UPDATE case_management.support_cases
                SET reason_codes = %s,
                    updated_at = GREATEST(%s, updated_at, created_at), version = %s
                WHERE case_id = %s AND version = %s
                """,
                (list(reason_codes), now, case.version + 1, case.case_id, case.version),
            )
            if cursor.rowcount != 1:
                raise LeaseConflictError(str(claimed.command_id))
        event = SupportCaseEvent(
            event_id=uuid4(),
            idempotency_key=(
                f"provider-command:{claimed.command_id}:cycle:"
                f"{claimed.delivery_cycle}:{status}"
            ),
            case_id=case.case_id,
            event_type="provider_update",
            provider_command_id=claimed.command_id,
            provider_command_status=status,
            provider_reference=(
                provider_result.provider_reference
                if provider_result is not None
                else None
            ),
            actor="system",
            customer_id=case.customer_id,
            tenant_id=case.tenant_id,
            created_at=now,
        )
        await cursor.execute(
            f"INSERT INTO case_management.support_case_events ({_CASE_EVENT_COLUMNS}) VALUES ({', '.join(['%s'] * 25)})",
            _case_event_values(event),
        )
