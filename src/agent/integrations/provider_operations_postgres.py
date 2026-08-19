"""Tenant-scoped PostgreSQL Provider operations repository and coordinators."""

import re
from collections.abc import Mapping
from typing import Any, NoReturn
from uuid import UUID, uuid4

from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.auth.models import AccessScope, ProviderOperationsPermission
from agent.auth.rbac import has_provider_operations_permission
from agent.auth.visibility import ForbiddenError
from agent.cases.models import SupportCaseEvent
from agent.cases.postgres_repository import _EVENT_COLUMNS as _CASE_EVENT_COLUMNS
from agent.cases.postgres_repository import _event_values as _case_event_values
from agent.integrations.provider_operations_contracts import (
    ProviderInboxAttemptView,
    ProviderInboxDetail,
    ProviderInboxQueueSummary,
    ProviderOutboxAttemptView,
    ProviderOutboxDetail,
    ProviderOutboxQueueSummary,
    ProviderQueueOverview,
    ProviderRedriveRequest,
    ProviderRedriveView,
)
from agent.integrations.provider_operations_policy import (
    InboxRedriveState,
    OutboxRedriveState,
    decide_inbox_redrive_eligibility,
    decide_outbox_redrive_eligibility,
)
from agent.integrations.provider_operations_repository import (
    ProviderOperationsConflictError,
    ProviderOperationsNotFoundError,
    ProviderOperationsPersistenceError,
    ProviderOperationsRepository,
)
from agent.operations.models import OrderOperationEvent
from agent.operations.postgres_repository import (
    _EVENT_COLUMNS as _OPERATION_EVENT_COLUMNS,
)
from agent.operations.postgres_repository import (
    _event_values as _operation_event_values,
)

_OUTBOX_AUDIT_CONSTRAINT = "uq_outbox_redrives_tenant_request"
_INBOX_AUDIT_CONSTRAINT = "uq_inbox_redrives_tenant_request"
_HISTORY_LIMIT_MAX = 100
_SAFE_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SAFE_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _authorize(scope: AccessScope, permission: ProviderOperationsPermission) -> None:
    """Repeat authorization at the persistence boundary as defense in depth."""
    if not has_provider_operations_permission(scope, permission):
        raise ForbiddenError("the caller cannot access Provider operations")


def _validate_history_limit(limit: int) -> None:
    if not isinstance(limit, int) or not 1 <= limit <= _HISTORY_LIMIT_MAX:
        raise ValueError("history_limit must be between 1 and 100")


def _redrive_view(row: Mapping[str, Any]) -> ProviderRedriveView:
    return ProviderRedriveView(
        request_id=_safe_request_id(row["request_id"]),
        reason_code=row["reason_code"],
        actor=row["requested_by"],
        previous_cycle=row["previous_cycle"],
        new_cycle=row["new_cycle"],
        created_at=row["created_at"],
    )


def _safe_request_id(value: Any) -> str | None:
    """Suppress v0.7 request ids that are not legal in the v0.8 contract."""
    if isinstance(value, str) and _SAFE_REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return None


def _safe_error_code(value: Any) -> str | None:
    """Suppress legacy values that do not meet the safe-code contract."""
    if isinstance(value, str) and _SAFE_ERROR_CODE_PATTERN.fullmatch(value):
        return value
    return None


def _outbox_attempt_view(row: Mapping[str, Any]) -> ProviderOutboxAttemptView:
    values = dict(row)
    values["safe_error_code"] = _safe_error_code(row["safe_error_code"])
    status = row["http_status"]
    values["http_status"] = (
        status if isinstance(status, int) and 100 <= status <= 599 else None
    )
    return ProviderOutboxAttemptView.model_validate(values)


def _inbox_attempt_view(row: Mapping[str, Any]) -> ProviderInboxAttemptView:
    values = dict(row)
    values["safe_error_code"] = _safe_error_code(row["safe_error_code"])
    return ProviderInboxAttemptView.model_validate(values)


def _not_found() -> NoReturn:
    raise ProviderOperationsNotFoundError("provider_resource_not_found")


def _conflict(code: str) -> NoReturn:
    raise ProviderOperationsConflictError(code)


class PostgresProviderOperationsRepository(ProviderOperationsRepository):
    """Implement safe reads and atomic manual recovery on PostgreSQL."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Store a pool whose lifecycle is managed by the application."""
        self._pool = pool

    async def get_queue_overview(self, scope: AccessScope) -> ProviderQueueOverview:
        """Aggregate both queues using tenant-scoped, payload-free projections."""
        _authorize(scope, "provider_ops:read")
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        SELECT status, count(*) AS count,
                               min(available_at) AS oldest_available_at
                        FROM integration.outbox_messages
                        WHERE tenant_id = %s
                        GROUP BY status
                        ORDER BY status
                        """,
                        (scope.tenant_id,),
                    )
                    outbox_rows = await cursor.fetchall()
                    await cursor.execute(
                        """
                        SELECT status, count(*) AS count,
                               min(available_at) AS oldest_available_at
                        FROM integration.inbox_messages
                        WHERE tenant_id = %s
                        GROUP BY status
                        ORDER BY status
                        """,
                        (scope.tenant_id,),
                    )
                    inbox_rows = await cursor.fetchall()
                    await cursor.execute("SELECT clock_timestamp() AS generated_at")
                    now = await cursor.fetchone()
        except (errors.DatabaseError, PoolTimeout) as error:
            raise ProviderOperationsPersistenceError(
                "provider_operations_read_failed"
            ) from error
        if now is None:
            raise ProviderOperationsPersistenceError("provider_operations_read_failed")
        return ProviderQueueOverview(
            outbox=tuple(
                ProviderOutboxQueueSummary.model_validate(row) for row in outbox_rows
            ),
            inbox=tuple(
                ProviderInboxQueueSummary.model_validate(row) for row in inbox_rows
            ),
            generated_at=now["generated_at"],
        )

    async def get_outbox_detail(
        self, scope: AccessScope, command_id: UUID, *, history_limit: int = 50
    ) -> ProviderOutboxDetail:
        """Return one tenant-scoped Outbox row and bounded safe histories."""
        _authorize(scope, "provider_ops:read")
        _validate_history_limit(history_limit)
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        SELECT command_id, aggregate_type, aggregate_id, status,
                               delivery_cycle, attempts_in_cycle, available_at,
                               last_failure_kind, last_error_code, created_at,
                               updated_at, published_at, dead_at
                        FROM integration.outbox_messages
                        WHERE tenant_id = %s AND command_id = %s
                        """,
                        (scope.tenant_id, command_id),
                    )
                    message = await cursor.fetchone()
                    if message is None:
                        _not_found()
                    await cursor.execute(
                        """
                        SELECT a.delivery_cycle, a.attempt_number, a.outcome,
                               a.failure_kind, a.http_status, a.safe_error_code,
                               a.started_at, a.finished_at, a.next_available_at
                        FROM integration.outbox_delivery_attempts AS a
                        JOIN integration.outbox_messages AS m
                          ON m.command_id = a.command_id
                        WHERE m.tenant_id = %s AND a.command_id = %s
                        ORDER BY a.delivery_cycle DESC, a.attempt_number DESC,
                                 a.attempt_id DESC
                        LIMIT %s
                        """,
                        (scope.tenant_id, command_id, history_limit),
                    )
                    attempt_rows = await cursor.fetchall()
                    await cursor.execute(
                        """
                        SELECT request_id, reason_code, requested_by,
                               previous_cycle, new_cycle, created_at
                        FROM integration.outbox_redrives
                        WHERE tenant_id = %s AND command_id = %s
                        ORDER BY created_at DESC, redrive_id DESC
                        LIMIT %s
                        """,
                        (scope.tenant_id, command_id, history_limit),
                    )
                    audit_rows = await cursor.fetchall()
        except (ProviderOperationsNotFoundError, ValueError):
            raise
        except (errors.DatabaseError, PoolTimeout) as error:
            raise ProviderOperationsPersistenceError(
                "provider_operations_read_failed"
            ) from error
        message_values = dict(message)
        message_values["last_error_code"] = _safe_error_code(message["last_error_code"])
        return ProviderOutboxDetail(
            **message_values,
            attempts=tuple(_outbox_attempt_view(row) for row in attempt_rows),
            redrives=tuple(_redrive_view(row) for row in audit_rows),
        )

    async def get_inbox_detail(
        self, scope: AccessScope, inbox_id: UUID, *, history_limit: int = 50
    ) -> ProviderInboxDetail:
        """Return one tenant-scoped Inbox row and bounded safe histories."""
        _authorize(scope, "provider_ops:read")
        _validate_history_limit(history_limit)
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(
                        """
                        SELECT inbox_id, command_id, aggregate_type, aggregate_id,
                               status, processing_cycle, attempts_in_cycle,
                               processing_attempts AS total_attempts, available_at,
                               last_error_code, received_at, updated_at,
                               processed_at, failed_at
                        FROM integration.inbox_messages
                        WHERE tenant_id = %s AND inbox_id = %s
                        """,
                        (scope.tenant_id, inbox_id),
                    )
                    message = await cursor.fetchone()
                    if message is None:
                        _not_found()
                    await cursor.execute(
                        """
                        SELECT a.processing_cycle, a.attempt_number, a.outcome,
                               a.safe_error_code, a.started_at, a.finished_at
                        FROM integration.inbox_processing_attempts AS a
                        JOIN integration.inbox_messages AS m
                          ON m.inbox_id = a.inbox_id
                        WHERE m.tenant_id = %s AND a.inbox_id = %s
                        ORDER BY a.processing_cycle DESC, a.attempt_number DESC,
                                 a.attempt_id DESC
                        LIMIT %s
                        """,
                        (scope.tenant_id, inbox_id, history_limit),
                    )
                    attempt_rows = await cursor.fetchall()
                    await cursor.execute(
                        """
                        SELECT request_id, reason_code, requested_by,
                               previous_cycle, new_cycle, created_at
                        FROM integration.inbox_redrives
                        WHERE tenant_id = %s AND inbox_id = %s
                        ORDER BY created_at DESC, redrive_id DESC
                        LIMIT %s
                        """,
                        (scope.tenant_id, inbox_id, history_limit),
                    )
                    audit_rows = await cursor.fetchall()
        except (ProviderOperationsNotFoundError, ValueError):
            raise
        except (errors.DatabaseError, PoolTimeout) as error:
            raise ProviderOperationsPersistenceError(
                "provider_operations_read_failed"
            ) from error
        message_values = dict(message)
        message_values["last_error_code"] = _safe_error_code(message["last_error_code"])
        return ProviderInboxDetail(
            **message_values,
            attempts=tuple(_inbox_attempt_view(row) for row in attempt_rows),
            redrives=tuple(_redrive_view(row) for row in audit_rows),
        )

    async def redrive_outbox(
        self,
        scope: AccessScope,
        command_id: UUID,
        request: ProviderRedriveRequest,
    ) -> ProviderRedriveView:
        """Recover one eligible command and its aggregate atomically."""
        _authorize(scope, "provider_ops:redrive")
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        replay = await self._outbox_audit_for_request(
                            cursor, scope.tenant_id, request.request_id
                        )
                        if replay is not None:
                            return self._validate_outbox_replay(
                                replay, command_id, request
                            )
                        await cursor.execute(
                            """
                            SELECT command_id, tenant_id, customer_id, command_type,
                                   aggregate_type, aggregate_id, status,
                                   delivery_cycle, attempts_in_cycle, lease_id,
                                   lease_expires_at, last_failure_kind, updated_at
                            FROM integration.outbox_messages
                            WHERE tenant_id = %s AND command_id = %s
                            FOR UPDATE
                            """,
                            (scope.tenant_id, command_id),
                        )
                        message = await cursor.fetchone()
                        if message is None:
                            _not_found()
                        # A concurrent identical request may have committed while
                        # this transaction waited for the command lock.
                        replay = await self._outbox_audit_for_request(
                            cursor, scope.tenant_id, request.request_id
                        )
                        if replay is not None:
                            return self._validate_outbox_replay(
                                replay, command_id, request
                            )
                        await cursor.execute(
                            """
                            SELECT a.delivery_cycle, a.attempt_number, a.outcome,
                                   a.failure_kind
                            FROM integration.outbox_delivery_attempts AS a
                            JOIN integration.outbox_messages AS m
                              ON m.command_id = a.command_id
                            WHERE m.tenant_id = %s AND a.command_id = %s
                              AND a.delivery_cycle = %s
                              AND a.finished_at IS NOT NULL
                            ORDER BY a.attempt_number DESC, a.attempt_id DESC
                            LIMIT 1
                            FOR UPDATE OF a
                            """,
                            (
                                scope.tenant_id,
                                command_id,
                                message["delivery_cycle"],
                            ),
                        )
                        attempt = await cursor.fetchone()
                        decision = decide_outbox_redrive_eligibility(
                            OutboxRedriveState(
                                status=message["status"],
                                delivery_cycle=message["delivery_cycle"],
                                attempts_in_cycle=message["attempts_in_cycle"],
                                has_active_lease=message["lease_id"] is not None,
                                last_failure_kind=message["last_failure_kind"],
                                terminal_attempt_cycle=(
                                    attempt["delivery_cycle"] if attempt else None
                                ),
                                terminal_attempt_number=(
                                    attempt["attempt_number"] if attempt else None
                                ),
                                terminal_attempt_outcome=(
                                    attempt["outcome"] if attempt else None
                                ),
                                terminal_attempt_failure_kind=(
                                    attempt["failure_kind"] if attempt else None
                                ),
                            )
                        )
                        if not decision.eligible:
                            _conflict(decision.reason_code.value)

                        await cursor.execute(
                            """
                            SELECT clock_timestamp() AS available_at,
                                   GREATEST(clock_timestamp(), %s) AS now
                            """,
                            (message["updated_at"],),
                        )
                        now_row = await cursor.fetchone()
                        if now_row is None:
                            raise ProviderOperationsPersistenceError(
                                "provider_operations_redrive_failed"
                            )
                        now = now_row["now"]
                        available_at = now_row["available_at"]
                        redrive_id = uuid4()
                        if message["aggregate_type"] == "order_operation":
                            await self._recover_operation(
                                cursor, scope, message, request, redrive_id, now
                            )
                        else:
                            await self._audit_support_case(
                                cursor, scope, message, request, redrive_id, now
                            )
                        previous_cycle = message["delivery_cycle"]
                        new_cycle = previous_cycle + 1
                        await cursor.execute(
                            """
                            UPDATE integration.outbox_messages
                            SET status = 'retry_scheduled', delivery_cycle = %s,
                                attempts_in_cycle = 0, available_at = %s,
                                lease_id = NULL, lease_owner = NULL,
                                lease_expires_at = NULL, last_failure_kind = NULL,
                                last_error_code = NULL, last_error_message = NULL,
                                published_at = NULL, dead_at = NULL,
                                updated_at = GREATEST(%s, updated_at, created_at)
                            WHERE tenant_id = %s AND command_id = %s
                              AND status = 'dead' AND delivery_cycle = %s
                            """,
                            (
                                new_cycle,
                                available_at,
                                now,
                                scope.tenant_id,
                                command_id,
                                previous_cycle,
                            ),
                        )
                        if cursor.rowcount != 1:
                            _conflict("redrive_state_changed")
                        await cursor.execute(
                            """
                            INSERT INTO integration.outbox_redrives (
                                redrive_id, command_id, tenant_id, request_id,
                                requested_by, reason, reason_code, previous_cycle,
                                new_cycle, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                redrive_id,
                                command_id,
                                scope.tenant_id,
                                request.request_id,
                                scope.identity,
                                request.reason_code.value,
                                request.reason_code.value,
                                previous_cycle,
                                new_cycle,
                                now,
                            ),
                        )
                        return ProviderRedriveView(
                            request_id=request.request_id,
                            reason_code=request.reason_code,
                            actor=scope.identity,
                            previous_cycle=previous_cycle,
                            new_cycle=new_cycle,
                            created_at=now,
                        )
        except (
            ForbiddenError,
            ProviderOperationsNotFoundError,
            ProviderOperationsConflictError,
        ):
            raise
        except errors.UniqueViolation as error:
            if error.diag.constraint_name == _OUTBOX_AUDIT_CONSTRAINT:
                replay = await self._load_outbox_audit(scope, request.request_id)
                if replay is not None:
                    return self._validate_outbox_replay(replay, command_id, request)
                _conflict("request_id_conflict")
            raise ProviderOperationsConflictError("audit_conflict") from error
        except (errors.DatabaseError, PoolTimeout) as error:
            raise ProviderOperationsPersistenceError(
                "provider_operations_redrive_failed"
            ) from error

    async def redrive_inbox(
        self,
        scope: AccessScope,
        inbox_id: UUID,
        request: ProviderRedriveRequest,
    ) -> ProviderRedriveView:
        """Open one new processing cycle without changing webhook content."""
        _authorize(scope, "provider_ops:redrive")
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor(row_factory=dict_row) as cursor:
                        replay = await self._inbox_audit_for_request(
                            cursor, scope.tenant_id, request.request_id
                        )
                        if replay is not None:
                            return self._validate_inbox_replay(
                                replay, inbox_id, request
                            )
                        await cursor.execute(
                            """
                            SELECT inbox_id, status, processing_cycle, lease_id,
                                   lease_expires_at, updated_at
                            FROM integration.inbox_messages
                            WHERE tenant_id = %s AND inbox_id = %s
                            FOR UPDATE
                            """,
                            (scope.tenant_id, inbox_id),
                        )
                        message = await cursor.fetchone()
                        if message is None:
                            _not_found()
                        replay = await self._inbox_audit_for_request(
                            cursor, scope.tenant_id, request.request_id
                        )
                        if replay is not None:
                            return self._validate_inbox_replay(
                                replay, inbox_id, request
                            )
                        decision = decide_inbox_redrive_eligibility(
                            InboxRedriveState(
                                status=message["status"],
                                has_active_lease=message["lease_id"] is not None,
                            )
                        )
                        if not decision.eligible:
                            _conflict(decision.reason_code.value)
                        await cursor.execute(
                            """
                            SELECT clock_timestamp() AS available_at,
                                   GREATEST(clock_timestamp(), %s) AS now
                            """,
                            (message["updated_at"],),
                        )
                        now_row = await cursor.fetchone()
                        if now_row is None:
                            raise ProviderOperationsPersistenceError(
                                "provider_operations_redrive_failed"
                            )
                        now = now_row["now"]
                        available_at = now_row["available_at"]
                        previous_cycle = message["processing_cycle"]
                        new_cycle = previous_cycle + 1
                        redrive_id = uuid4()
                        await cursor.execute(
                            """
                            UPDATE integration.inbox_messages
                            SET status = 'received', processing_cycle = %s,
                                attempts_in_cycle = 0, available_at = %s,
                                lease_id = NULL, lease_owner = NULL,
                                lease_expires_at = NULL, last_error_code = NULL,
                                last_error_message = NULL, processed_at = NULL,
                                failed_at = NULL,
                                updated_at = GREATEST(%s, updated_at, received_at)
                            WHERE tenant_id = %s AND inbox_id = %s
                              AND status = 'failed' AND processing_cycle = %s
                            """,
                            (
                                new_cycle,
                                available_at,
                                now,
                                scope.tenant_id,
                                inbox_id,
                                previous_cycle,
                            ),
                        )
                        if cursor.rowcount != 1:
                            _conflict("redrive_state_changed")
                        await cursor.execute(
                            """
                            INSERT INTO integration.inbox_redrives (
                                redrive_id, inbox_id, tenant_id, request_id,
                                requested_by, reason_code, previous_cycle,
                                new_cycle, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                redrive_id,
                                inbox_id,
                                scope.tenant_id,
                                request.request_id,
                                scope.identity,
                                request.reason_code.value,
                                previous_cycle,
                                new_cycle,
                                now,
                            ),
                        )
                        return ProviderRedriveView(
                            request_id=request.request_id,
                            reason_code=request.reason_code,
                            actor=scope.identity,
                            previous_cycle=previous_cycle,
                            new_cycle=new_cycle,
                            created_at=now,
                        )
        except (
            ForbiddenError,
            ProviderOperationsNotFoundError,
            ProviderOperationsConflictError,
        ):
            raise
        except errors.UniqueViolation as error:
            if error.diag.constraint_name == _INBOX_AUDIT_CONSTRAINT:
                replay = await self._load_inbox_audit(scope, request.request_id)
                if replay is not None:
                    return self._validate_inbox_replay(replay, inbox_id, request)
                _conflict("request_id_conflict")
            raise ProviderOperationsConflictError("audit_conflict") from error
        except (errors.DatabaseError, PoolTimeout) as error:
            raise ProviderOperationsPersistenceError(
                "provider_operations_redrive_failed"
            ) from error

    async def _recover_operation(
        self, cursor, scope, message, request, redrive_id, now
    ) -> None:
        await cursor.execute(
            """
            SELECT operation_id, customer_id, tenant_id, status,
                   requires_manual_review, review_case_type, review_priority,
                   support_case_id, version
            FROM case_management.order_operations
            WHERE tenant_id = %s AND operation_id = %s
            FOR UPDATE
            """,
            (scope.tenant_id, message["aggregate_id"]),
        )
        operation = await cursor.fetchone()
        if operation is None:
            _conflict("aggregate_association_mismatch")
        if (
            operation["customer_id"] != message["customer_id"]
            or operation["status"] != "manual_review"
            or not operation["requires_manual_review"]
            or operation["review_case_type"] != "order_operation_review"
            or operation["support_case_id"] is None
        ):
            _conflict("aggregate_state_mismatch")
        await cursor.execute(
            """
            SELECT case_id, customer_id, tenant_id, case_type
            FROM case_management.support_cases
            WHERE tenant_id = %s AND case_id = %s
            FOR UPDATE
            """,
            (scope.tenant_id, operation["support_case_id"]),
        )
        case = await cursor.fetchone()
        if (
            case is None
            or case["customer_id"] != message["customer_id"]
            or case["case_type"] != "order_operation_review"
        ):
            _conflict("review_case_association_mismatch")
        await cursor.execute(
            """
            UPDATE case_management.order_operations
            SET status = 'queued', requires_manual_review = FALSE,
                review_case_type = NULL, review_priority = NULL,
                support_case_id = NULL,
                updated_at = GREATEST(%s, updated_at, created_at),
                version = version + 1
            WHERE tenant_id = %s AND operation_id = %s
              AND status = 'manual_review' AND version = %s
            """,
            (
                now,
                scope.tenant_id,
                operation["operation_id"],
                operation["version"],
            ),
        )
        if cursor.rowcount != 1:
            _conflict("aggregate_state_changed")
        operation_event = OrderOperationEvent(
            event_id=uuid4(),
            idempotency_key=(
                f"provider-redrive:{scope.tenant_id}:{message['command_id']}:{request.request_id}"
            ),
            operation_id=operation["operation_id"],
            event_type="status_changed",
            previous_status="manual_review",
            current_status="queued",
            actor=scope.identity,
            customer_id=message["customer_id"],
            tenant_id=scope.tenant_id,
            created_at=now,
        )
        await cursor.execute(
            f"INSERT INTO case_management.order_operation_events ({_OPERATION_EVENT_COLUMNS}) "
            f"VALUES ({', '.join(['%s'] * 12)})",
            _operation_event_values(operation_event),
        )
        await self._insert_case_redrive_event(
            cursor,
            scope=scope,
            case_id=case["case_id"],
            customer_id=message["customer_id"],
            command_id=message["command_id"],
            request=request,
            redrive_id=redrive_id,
            now=now,
        )

    async def _audit_support_case(
        self, cursor, scope, message, request, redrive_id, now
    ) -> None:
        if message["command_type"] != "delivery_investigation":
            _conflict("aggregate_association_mismatch")
        await cursor.execute(
            """
            SELECT case_id, customer_id, tenant_id, case_type
            FROM case_management.support_cases
            WHERE tenant_id = %s AND case_id = %s
            FOR UPDATE
            """,
            (scope.tenant_id, message["aggregate_id"]),
        )
        case = await cursor.fetchone()
        if (
            case is None
            or case["customer_id"] != message["customer_id"]
            or case["case_type"] != "delivery_investigation"
        ):
            _conflict("aggregate_association_mismatch")
        await self._insert_case_redrive_event(
            cursor,
            scope=scope,
            case_id=case["case_id"],
            customer_id=message["customer_id"],
            command_id=message["command_id"],
            request=request,
            redrive_id=redrive_id,
            now=now,
        )

    async def _insert_case_redrive_event(
        self,
        cursor,
        *,
        scope,
        case_id,
        customer_id,
        command_id,
        request,
        redrive_id,
        now,
    ) -> None:
        event = SupportCaseEvent(
            event_id=uuid4(),
            idempotency_key=f"provider-redrive:{scope.tenant_id}:{redrive_id}",
            case_id=case_id,
            event_type="provider_redrive",
            provider_command_id=command_id,
            provider_redrive_reason_code=request.reason_code.value,
            actor=scope.identity,
            customer_id=customer_id,
            tenant_id=scope.tenant_id,
            created_at=now,
        )
        await cursor.execute(
            f"INSERT INTO case_management.support_case_events ({_CASE_EVENT_COLUMNS}) "
            f"VALUES ({', '.join(['%s'] * 25)})",
            _case_event_values(event),
        )

    @staticmethod
    async def _outbox_audit_for_request(cursor, tenant_id, request_id):
        await cursor.execute(
            """
            SELECT command_id, request_id, reason_code, requested_by,
                   previous_cycle, new_cycle, created_at
            FROM integration.outbox_redrives
            WHERE tenant_id = %s AND request_id = %s
            """,
            (tenant_id, request_id),
        )
        return await cursor.fetchone()

    @staticmethod
    async def _inbox_audit_for_request(cursor, tenant_id, request_id):
        await cursor.execute(
            """
            SELECT inbox_id, request_id, reason_code, requested_by,
                   previous_cycle, new_cycle, created_at
            FROM integration.inbox_redrives
            WHERE tenant_id = %s AND request_id = %s
            """,
            (tenant_id, request_id),
        )
        return await cursor.fetchone()

    @staticmethod
    def _validate_outbox_replay(row, command_id, request):
        if (
            row["command_id"] != command_id
            or row["reason_code"] != request.reason_code.value
        ):
            _conflict("request_id_conflict")
        return _redrive_view(row)

    @staticmethod
    def _validate_inbox_replay(row, inbox_id, request):
        if (
            row["inbox_id"] != inbox_id
            or row["reason_code"] != request.reason_code.value
        ):
            _conflict("request_id_conflict")
        return _redrive_view(row)

    async def _load_outbox_audit(self, scope, request_id):
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    return await self._outbox_audit_for_request(
                        cursor, scope.tenant_id, request_id
                    )
        except (errors.DatabaseError, PoolTimeout) as error:
            raise ProviderOperationsPersistenceError(
                "provider_operations_redrive_failed"
            ) from error

    async def _load_inbox_audit(self, scope, request_id):
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    return await self._inbox_audit_for_request(
                        cursor, scope.tenant_id, request_id
                    )
        except (errors.DatabaseError, PoolTimeout) as error:
            raise ProviderOperationsPersistenceError(
                "provider_operations_redrive_failed"
            ) from error
