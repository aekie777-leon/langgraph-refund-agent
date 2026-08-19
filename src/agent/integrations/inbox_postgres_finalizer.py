"""PostgreSQL atomic finalization for provider callback aggregates."""

from datetime import datetime
from uuid import uuid4

from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.cases.models import SupportCaseEvent
from agent.cases.postgres_repository import (
    _CASE_COLUMNS,
    _case_from_row,
)
from agent.cases.postgres_repository import (
    _EVENT_COLUMNS as _CASE_EVENT_COLUMNS,
)
from agent.cases.postgres_repository import (
    _event_values as _case_event_values,
)
from agent.integrations.inbox_finalizer import InboxFinalizationResult
from agent.integrations.inbox_policy import (
    decide_inbox_outbox_readiness,
    decide_order_operation_callback,
)
from agent.integrations.models import (
    DeliveryInvestigationCommandPayload,
    OrderOperationCommandPayload,
)
from agent.integrations.persistence_models import ClaimedInboxMessage
from agent.integrations.postgres_repository import (
    _INBOX_COLUMNS,
    _OUTBOX_COLUMNS,
    _inbox_from_row,
    _outbox_from_row,
)
from agent.integrations.postgres_writes import (
    finish_inbox_attempt,
    mark_inbox_failed,
    mark_inbox_processed,
    schedule_inbox_retry,
)
from agent.integrations.repository import (
    IntegrationPersistenceError,
    LeaseConflictError,
)
from agent.operations.models import OrderOperationEvent
from agent.operations.postgres_repository import (
    _EVENT_COLUMNS,
    _OPERATION_COLUMNS,
    _event_values,
    _operation_from_row,
)


class PostgresInboxFinalizer:
    """Fence and atomically apply one provider Inbox callback."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Store the application-owned PostgreSQL pool."""
        self._pool = pool

    async def finalize_order_operation(
        self, *, claimed: ClaimedInboxMessage, retry_available_at: datetime
    ) -> InboxFinalizationResult:
        """Finalize this claimed callback in one transaction."""
        if claimed.lease_id is None or claimed.lease_owner is None:
            raise LeaseConflictError(str(claimed.inbox_id))
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        inbox, now = await self._lock_inbox(cursor, claimed)
                        if inbox.aggregate_type != "order_operation":
                            raise ValueError(
                                "order-operation finalizer requires order_operation Inbox"
                            )
                        if not self._payload_association_matches(inbox):
                            return await self._fail(
                                cursor,
                                claimed,
                                now,
                                "inbox_payload_association_mismatch",
                            )
                        if not self._claimed_business_fields_match(claimed, inbox):
                            raise LeaseConflictError(str(claimed.inbox_id))
                        await cursor.execute(
                            f"SELECT {_OUTBOX_COLUMNS} FROM integration.outbox_messages WHERE command_id=%s FOR UPDATE",
                            (inbox.command_id,),
                        )
                        outbox_row = await cursor.fetchone()
                        if outbox_row is None:
                            return await self._fail(
                                cursor,
                                claimed,
                                now,
                                "outbox_not_found",
                            )
                        outbox = _outbox_from_row(outbox_row)
                        if not isinstance(outbox.payload, OrderOperationCommandPayload):
                            return await self._fail(
                                cursor,
                                claimed,
                                now,
                                "outbox_association_mismatch",
                            )
                        envelope = outbox.to_envelope()
                        if (
                            outbox.tenant_id != inbox.tenant_id
                            or outbox.provider_connection_id
                            != inbox.provider_connection_id
                            or outbox.aggregate_type != inbox.aggregate_type
                            or outbox.aggregate_id != inbox.aggregate_id
                            or outbox.command_id != inbox.command_id
                            or envelope.command_id != inbox.command_id
                        ):
                            return await self._fail(
                                cursor,
                                claimed,
                                now,
                                "outbox_association_mismatch",
                            )
                        readiness = decide_inbox_outbox_readiness(
                            outbox_status=outbox.status
                        )
                        if readiness == "retry":
                            if (
                                retry_available_at.tzinfo is None
                                or retry_available_at.utcoffset() is None
                                or retry_available_at <= now
                            ):
                                raise ValueError(
                                    "retry_available_at must be aware and later than database time"
                                )
                            return await self._retry_or_fail(
                                cursor,
                                claimed,
                                inbox.attempts_in_cycle,
                                now,
                                retry_available_at,
                            )
                        await cursor.execute(
                            f"SELECT {_OPERATION_COLUMNS} FROM case_management.order_operations WHERE operation_id=%s FOR UPDATE",
                            (inbox.aggregate_id,),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            return await self._fail(
                                cursor, claimed, now, "operation_not_found"
                            )
                        operation = _operation_from_row(row)
                        event = inbox.payload
                        if (
                            operation.tenant_id != inbox.tenant_id
                            or operation.customer_id != outbox.customer_id
                            or operation.operation_id != outbox.aggregate_id
                            or operation.order_id != outbox.payload.order_id
                            or operation.operation_type != outbox.payload.operation_type
                            or operation.order_version != outbox.expected_order_version
                            or operation.source_message_id != outbox.source_message_id
                            or (
                                event.order_id is not None
                                and event.order_id != operation.order_id
                            )
                        ):
                            return await self._fail(
                                cursor, claimed, now, "order_association_mismatch"
                            )
                        incoming_reference = (
                            event.provider_reference or event.provider_operation_id
                        )
                        decision = decide_order_operation_callback(
                            local_status=operation.status,
                            provider_status=event.command_status,
                            current_provider_reference=operation.provider_reference,
                            incoming_provider_reference=incoming_reference,
                        )
                        if decision.action == "conflict":
                            code = (
                                "provider_reference_conflict"
                                if operation.provider_reference
                                and incoming_reference
                                and operation.provider_reference != incoming_reference
                                else "terminal_status_conflict"
                            )
                            return await self._fail(cursor, claimed, now, code)
                        if readiness == "dead" and decision.action != "duplicate":
                            return await self._fail(
                                cursor, claimed, now, "terminal_status_conflict"
                            )
                        if decision.action == "apply":
                            assert decision.target_status is not None
                            reference = (
                                incoming_reference or operation.provider_reference
                            )
                            await cursor.execute(
                                "UPDATE case_management.order_operations SET status=%s, provider_reference=%s, updated_at=GREATEST(%s, updated_at, created_at), version=%s WHERE operation_id=%s AND version=%s",
                                (
                                    decision.target_status,
                                    reference,
                                    now,
                                    operation.version + 1,
                                    operation.operation_id,
                                    operation.version,
                                ),
                            )
                            if cursor.rowcount != 1:
                                raise LeaseConflictError(str(claimed.inbox_id))
                            domain_event = OrderOperationEvent(
                                event_id=uuid4(),
                                idempotency_key=f"provider-webhook:{claimed.inbox_id}:operation-status",
                                operation_id=operation.operation_id,
                                event_type="status_changed",
                                previous_status=operation.status,
                                current_status=decision.target_status,
                                provider_reference=reference,
                                actor="system",
                                customer_id=operation.customer_id,
                                tenant_id=operation.tenant_id,
                                created_at=now,
                            )
                            await cursor.execute(
                                f"INSERT INTO case_management.order_operation_events ({_EVENT_COLUMNS}) VALUES ({', '.join(['%s'] * 12)})",
                                _event_values(domain_event),
                            )
                            await self._process(cursor, claimed, now)
                            return InboxFinalizationResult(
                                "applied",
                                "order_operation",
                                operation.status,
                                decision.target_status,
                            )
                        if (
                            decision.action == "duplicate"
                            and operation.provider_reference is None
                            and incoming_reference is not None
                        ):
                            await cursor.execute(
                                "UPDATE case_management.order_operations SET provider_reference=%s, updated_at=GREATEST(%s, updated_at, created_at), version=%s WHERE operation_id=%s AND version=%s",
                                (
                                    incoming_reference,
                                    now,
                                    operation.version + 1,
                                    operation.operation_id,
                                    operation.version,
                                ),
                            )
                            if cursor.rowcount != 1:
                                raise LeaseConflictError(str(claimed.inbox_id))
                        await self._process(cursor, claimed, now)
                        return InboxFinalizationResult(
                            decision.action,
                            "order_operation",
                            operation.status,
                            operation.status,
                        )
        except LeaseConflictError:
            raise
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError(
                "Failed to finalize provider Inbox message"
            ) from error

    async def finalize_support_case(
        self, *, claimed: ClaimedInboxMessage, retry_available_at: datetime
    ) -> InboxFinalizationResult:
        """Append one provider update without mutating the support-case aggregate."""
        if claimed.lease_id is None or claimed.lease_owner is None:
            raise LeaseConflictError(str(claimed.inbox_id))
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        inbox, now = await self._lock_inbox(cursor, claimed)
                        if inbox.aggregate_type != "support_case":
                            raise ValueError(
                                "support-case finalizer requires support_case Inbox"
                            )
                        if not self._payload_association_matches(inbox):
                            return await self._fail(
                                cursor,
                                claimed,
                                now,
                                "inbox_payload_association_mismatch",
                                aggregate_type="support_case",
                            )
                        if not self._claimed_business_fields_match(claimed, inbox):
                            raise LeaseConflictError(str(claimed.inbox_id))
                        await cursor.execute(
                            f"SELECT {_OUTBOX_COLUMNS} FROM integration.outbox_messages "
                            "WHERE command_id=%s FOR UPDATE",
                            (inbox.command_id,),
                        )
                        outbox_row = await cursor.fetchone()
                        if outbox_row is None:
                            return await self._fail(
                                cursor,
                                claimed,
                                now,
                                "outbox_not_found",
                                aggregate_type="support_case",
                            )
                        outbox = _outbox_from_row(outbox_row)
                        if not isinstance(
                            outbox.payload, DeliveryInvestigationCommandPayload
                        ):
                            return await self._fail(
                                cursor,
                                claimed,
                                now,
                                "outbox_association_mismatch",
                                aggregate_type="support_case",
                            )
                        envelope = outbox.to_envelope()
                        if (
                            outbox.tenant_id != inbox.tenant_id
                            or outbox.provider_connection_id
                            != inbox.provider_connection_id
                            or outbox.aggregate_type != inbox.aggregate_type
                            or outbox.aggregate_id != inbox.aggregate_id
                            or outbox.command_id != inbox.command_id
                            or envelope.command_id != inbox.command_id
                        ):
                            return await self._fail(
                                cursor,
                                claimed,
                                now,
                                "outbox_association_mismatch",
                                aggregate_type="support_case",
                            )
                        readiness = decide_inbox_outbox_readiness(
                            outbox_status=outbox.status
                        )
                        if readiness == "retry":
                            if (
                                retry_available_at.tzinfo is None
                                or retry_available_at.utcoffset() is None
                                or retry_available_at <= now
                            ):
                                raise ValueError(
                                    "retry_available_at must be aware and later than database time"
                                )
                            return await self._retry_or_fail(
                                cursor,
                                claimed,
                                inbox.attempts_in_cycle,
                                now,
                                retry_available_at,
                                aggregate_type="support_case",
                            )
                        await cursor.execute(
                            f"SELECT {_CASE_COLUMNS} FROM case_management.support_cases "
                            "WHERE case_id=%s FOR UPDATE",
                            (inbox.aggregate_id,),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            return await self._fail(
                                cursor,
                                claimed,
                                now,
                                "case_not_found",
                                aggregate_type="support_case",
                            )
                        case = _case_from_row(row)
                        event = inbox.payload
                        if (
                            case.tenant_id != inbox.tenant_id
                            or case.customer_id != outbox.customer_id
                            or case.case_id != outbox.aggregate_id
                            or case.case_type != "delivery_investigation"
                            or case.order_id != outbox.payload.order_id
                            or case.source_message_id != outbox.source_message_id
                            or (
                                event.order_id is not None
                                and event.order_id != case.order_id
                            )
                        ):
                            return await self._fail(
                                cursor,
                                claimed,
                                now,
                                "support_case_association_mismatch",
                                aggregate_type="support_case",
                            )
                        provider_update = SupportCaseEvent(
                            event_id=uuid4(),
                            idempotency_key=(
                                f"provider-webhook:{inbox.inbox_id}:case-provider-update"
                            ),
                            case_id=case.case_id,
                            event_type="provider_update",
                            provider_command_id=inbox.command_id,
                            provider_command_status=event.command_status,
                            provider_reference=(
                                event.provider_reference or event.provider_operation_id
                            ),
                            actor="system",
                            customer_id=case.customer_id,
                            tenant_id=case.tenant_id,
                            created_at=now,
                        )
                        await cursor.execute(
                            f"INSERT INTO case_management.support_case_events "
                            f"({_CASE_EVENT_COLUMNS}) VALUES ({', '.join(['%s'] * 25)})",
                            _case_event_values(provider_update),
                        )
                        await self._process(cursor, claimed, now)
                        return InboxFinalizationResult("applied", "support_case")
        except LeaseConflictError:
            raise
        except (errors.DatabaseError, PoolTimeout) as error:
            raise IntegrationPersistenceError(
                "Failed to finalize provider Inbox message"
            ) from error

    async def _lock_inbox(self, cursor, claimed: ClaimedInboxMessage):
        await cursor.execute(
            f"SELECT {_INBOX_COLUMNS} FROM integration.inbox_messages WHERE inbox_id=%s FOR UPDATE",
            (claimed.inbox_id,),
        )
        inbox = await cursor.fetchone()
        await cursor.execute("SELECT clock_timestamp() AS now")
        now_row = await cursor.fetchone()
        if now_row is None:
            raise IntegrationPersistenceError("clock_timestamp returned no row")
        now = now_row["now"]
        persisted = _inbox_from_row(inbox) if inbox is not None else None
        if (
            persisted is None
            or persisted.status != "processing"
            or persisted.lease_id != claimed.lease_id
            or persisted.lease_owner != claimed.lease_owner
            or persisted.lease_expires_at is None
            or persisted.lease_expires_at <= now
        ):
            raise LeaseConflictError(str(claimed.inbox_id))
        await cursor.execute(
            "SELECT attempt_id, processing_cycle, attempt_number, inbox_id, lease_id, worker_id, started_at "
            "FROM integration.inbox_processing_attempts WHERE attempt_id=%s "
            "AND inbox_id=%s AND lease_id=%s AND worker_id=%s AND finished_at IS NULL",
            (
                claimed.attempt.attempt_id,
                claimed.inbox_id,
                claimed.lease_id,
                claimed.lease_owner,
            ),
        )
        attempt = await cursor.fetchone()
        if (
            attempt is None
            or persisted.processing_cycle != claimed.processing_cycle
            or persisted.processing_attempts != claimed.processing_attempts
            or persisted.attempts_in_cycle != claimed.attempts_in_cycle
            or attempt["processing_cycle"] != persisted.processing_cycle
            or attempt["processing_cycle"] != claimed.attempt.processing_cycle
            or attempt["attempt_number"] != persisted.processing_attempts
            or attempt["attempt_number"] != claimed.attempt.attempt_number
            or attempt["attempt_id"] != claimed.attempt.attempt_id
            or attempt["inbox_id"] != persisted.inbox_id
            or attempt["lease_id"] != persisted.lease_id
            or attempt["worker_id"] != persisted.lease_owner
        ):
            raise LeaseConflictError(str(claimed.inbox_id))
        return persisted, max(
            now,
            persisted.received_at,
            persisted.updated_at,
            attempt["started_at"],
        )

    @staticmethod
    def _payload_association_matches(inbox) -> bool:
        """Ensure trusted Inbox columns and its typed payload agree."""
        return (
            inbox.command_id == inbox.payload.command_id
            and inbox.aggregate_type == inbox.payload.aggregate_type
            and inbox.aggregate_id == inbox.payload.aggregate_id
        )

    @staticmethod
    def _claimed_business_fields_match(claimed: ClaimedInboxMessage, inbox) -> bool:
        """Fence caller-provided business fields against the locked Inbox row."""
        return (
            claimed.command_id == inbox.command_id
            and claimed.tenant_id == inbox.tenant_id
            and claimed.provider_connection_id == inbox.provider_connection_id
            and claimed.aggregate_type == inbox.aggregate_type
            and claimed.aggregate_id == inbox.aggregate_id
            and claimed.payload == inbox.payload
        )

    async def _process(self, cursor, claimed, now):
        if (
            await finish_inbox_attempt(
                cursor,
                attempt_id=claimed.attempt.attempt_id,
                inbox_id=claimed.inbox_id,
                lease_id=claimed.lease_id,
                worker_id=claimed.lease_owner,
                finished_at=now,
                outcome="processed",
            )
            != 1
            or await mark_inbox_processed(
                cursor,
                inbox_id=claimed.inbox_id,
                lease_id=claimed.lease_id,
                lease_owner=claimed.lease_owner,
                processed_at=now,
            )
            != 1
        ):
            raise LeaseConflictError(str(claimed.inbox_id))

    async def _fail(self, cursor, claimed, now, code, aggregate_type="order_operation"):
        if (
            await finish_inbox_attempt(
                cursor,
                attempt_id=claimed.attempt.attempt_id,
                inbox_id=claimed.inbox_id,
                lease_id=claimed.lease_id,
                worker_id=claimed.lease_owner,
                finished_at=now,
                outcome="terminal_failure",
                safe_error_code=code,
                safe_error_message="Provider webhook could not be applied.",
            )
            != 1
            or await mark_inbox_failed(
                cursor,
                inbox_id=claimed.inbox_id,
                lease_id=claimed.lease_id,
                lease_owner=claimed.lease_owner,
                failed_at=now,
                error_code=code,
                error_message="Provider webhook could not be applied.",
            )
            != 1
        ):
            raise LeaseConflictError(str(claimed.inbox_id))
        return InboxFinalizationResult("failed", aggregate_type, safe_error_code=code)

    async def _retry_or_fail(
        self,
        cursor,
        claimed,
        attempts_in_cycle,
        now,
        available_at,
        aggregate_type="order_operation",
    ):
        if attempts_in_cycle >= 5:
            return await self._fail(
                cursor,
                claimed,
                now,
                "outbox_not_finalized_attempts_exhausted",
                aggregate_type=aggregate_type,
            )
        if (
            await finish_inbox_attempt(
                cursor,
                attempt_id=claimed.attempt.attempt_id,
                inbox_id=claimed.inbox_id,
                lease_id=claimed.lease_id,
                worker_id=claimed.lease_owner,
                finished_at=now,
                outcome="retry_scheduled",
                safe_error_code="outbox_not_finalized",
                safe_error_message="Provider command is not finalized.",
            )
            != 1
            or await schedule_inbox_retry(
                cursor,
                inbox_id=claimed.inbox_id,
                lease_id=claimed.lease_id,
                lease_owner=claimed.lease_owner,
                available_at=available_at,
                updated_at=now,
                error_code="outbox_not_finalized",
                error_message="Provider command is not finalized.",
            )
            != 1
        ):
            raise LeaseConflictError(str(claimed.inbox_id))
        return InboxFinalizationResult(
            "retry_scheduled", aggregate_type, safe_error_code="outbox_not_finalized"
        )
