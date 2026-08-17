"""Persist order operations and immutable events in PostgreSQL."""

from collections.abc import Mapping
from typing import Any, NoReturn
from uuid import UUID

from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.auth.models import AccessScope
from agent.auth.visibility import customer_owned_visibility
from agent.operations.models import OrderOperation, OrderOperationEvent
from agent.operations.repository import (
    ActiveOrderOperationConflictError,
    ConcurrentOperationUpdateError,
    DuplicateOperationIdempotencyError,
    DuplicateOperationSourceMessageError,
    OperationPersistenceError,
    OperationRepository,
)

_ACTIVE_OPERATION_CONSTRAINT = "uq_order_operations_active_order"
_SOURCE_MESSAGE_CONSTRAINT = "uq_order_operations_source_message"
_OPERATION_IDEMPOTENCY_CONSTRAINT = "order_operations_idempotency_key_key"
_EVENT_IDEMPOTENCY_CONSTRAINT = "order_operation_events_idempotency_key_key"

_OPERATION_COLUMNS = """
    operation_id, idempotency_key, thread_id, source_message_id, order_id,
    operation_type, request_reason_code, policy_reason_codes, display_reason,
    replacement_variant_id, request_excerpt, order_version, amount, currency,
    requires_manual_review, review_case_type, review_priority, support_case_id,
    provider_reference, status, created_at, updated_at, version,
    customer_id, tenant_id, created_by
"""
_EVENT_COLUMNS = """
    event_id, idempotency_key, operation_id, event_type, previous_status,
    current_status, provider_reference, support_case_id, actor,
    customer_id, tenant_id, created_at
"""


def _operation_from_row(row: Mapping[str, Any]) -> OrderOperation:
    """Validate one database row as an order-operation aggregate."""
    return OrderOperation.model_validate(row)


def _event_from_row(row: Mapping[str, Any]) -> OrderOperationEvent:
    """Validate one database row as an immutable operation event."""
    return OrderOperationEvent.model_validate(row)


def _operation_values(operation: OrderOperation) -> tuple[Any, ...]:
    """Return SQL parameters in the operation insert-column order."""
    return (
        operation.operation_id,
        operation.idempotency_key,
        operation.thread_id,
        operation.source_message_id,
        operation.order_id,
        operation.operation_type,
        operation.request_reason_code,
        list(operation.policy_reason_codes),
        operation.display_reason,
        operation.replacement_variant_id,
        operation.request_excerpt,
        operation.order_version,
        operation.amount,
        operation.currency,
        operation.requires_manual_review,
        operation.review_case_type,
        operation.review_priority,
        operation.support_case_id,
        operation.provider_reference,
        operation.status,
        operation.created_at,
        operation.updated_at,
        operation.version,
        operation.customer_id,
        operation.tenant_id,
        operation.created_by,
    )


def _event_values(event: OrderOperationEvent) -> tuple[Any, ...]:
    """Return SQL parameters in the event insert-column order."""
    return (
        event.event_id,
        event.idempotency_key,
        event.operation_id,
        event.event_type,
        event.previous_status,
        event.current_status,
        event.provider_reference,
        event.support_case_id,
        event.actor,
        event.customer_id,
        event.tenant_id,
        event.created_at,
    )


def _raise_unique_violation(error: errors.UniqueViolation) -> NoReturn:
    """Translate named PostgreSQL unique constraints into domain errors."""
    constraint = error.diag.constraint_name
    if constraint == _ACTIVE_OPERATION_CONSTRAINT:
        raise ActiveOrderOperationConflictError(constraint) from error
    if constraint == _SOURCE_MESSAGE_CONSTRAINT:
        raise DuplicateOperationSourceMessageError(constraint) from error
    if constraint in (_OPERATION_IDEMPOTENCY_CONSTRAINT, _EVENT_IDEMPOTENCY_CONSTRAINT):
        raise DuplicateOperationIdempotencyError(constraint) from error
    raise OperationPersistenceError(
        f"Unexpected unique constraint violation: {constraint}"
    ) from error


def _validate_operation_ownership(
    scope: AccessScope,
    operation: OrderOperation,
) -> None:
    """Reject an operation whose ownership does not match the caller scope."""
    if operation.tenant_id != scope.tenant_id:
        raise ValueError("operation tenant_id must match the access scope")
    if operation.customer_id != scope.customer_id:
        raise ValueError("operation customer_id must match the access scope")


def _validate_event_ownership(scope: AccessScope, event: OrderOperationEvent) -> None:
    """Reject an event whose ownership does not match the caller scope."""
    if event.tenant_id != scope.tenant_id:
        raise ValueError("event tenant_id must match the access scope")
    if event.customer_id != scope.customer_id:
        raise ValueError("event customer_id must match the access scope")


class PostgresOrderOperationRepository(OperationRepository):
    """Implement atomic order-operation persistence with an async pool."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Store a pool whose lifecycle is managed by the application."""
        self._pool = pool

    async def get_operation(
        self,
        scope: AccessScope,
        operation_id: UUID,
    ) -> OrderOperation | None:
        """Return an operation by ID within the caller's access scope."""
        visibility, parameters = customer_owned_visibility(scope)
        query = f"""
            SELECT {_OPERATION_COLUMNS}
            FROM case_management.order_operations
            WHERE operation_id = %s AND {visibility}
        """
        return await self._fetch_operation(query, (operation_id, *parameters))

    async def find_by_source_message(
        self,
        scope: AccessScope,
        *,
        thread_id: str,
        source_message_id: str,
    ) -> OrderOperation | None:
        """Find an operation previously created by one source message."""
        visibility, parameters = customer_owned_visibility(scope)
        query = f"""
            SELECT {_OPERATION_COLUMNS}
            FROM case_management.order_operations
            WHERE thread_id = %s AND source_message_id = %s AND {visibility}
        """
        return await self._fetch_operation(
            query,
            (thread_id, source_message_id, *parameters),
        )

    async def find_event_by_idempotency_key(
        self,
        scope: AccessScope,
        idempotency_key: str,
    ) -> OrderOperationEvent | None:
        """Find one immutable event by its stable idempotency key."""
        visibility, parameters = customer_owned_visibility(scope)
        query = f"""
            SELECT {_EVENT_COLUMNS}
            FROM case_management.order_operation_events
            WHERE idempotency_key = %s AND {visibility}
        """
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(query, (idempotency_key, *parameters))
                    row = await cursor.fetchone()
        except PoolTimeout as error:
            raise OperationPersistenceError("Timed out acquiring PostgreSQL connection") from error
        except errors.DatabaseError as error:
            raise OperationPersistenceError("Could not read operation event") from error
        return _event_from_row(row) if row is not None else None

    async def find_active_by_order_id(
        self,
        scope: AccessScope,
        order_id: str,
    ) -> OrderOperation | None:
        """Find an unresolved operation for one order."""
        visibility, parameters = customer_owned_visibility(scope)
        query = f"""
            SELECT {_OPERATION_COLUMNS}
            FROM case_management.order_operations
            WHERE order_id = %s
              AND status IN ('pending_confirmation', 'submitted', 'processing', 'manual_review')
              AND {visibility}
            ORDER BY created_at, operation_id
            LIMIT 1
        """
        return await self._fetch_operation(query, (order_id, *parameters))

    async def create_operation_with_events(
        self,
        scope: AccessScope,
        *,
        operation: OrderOperation,
        events: tuple[OrderOperationEvent, ...],
    ) -> None:
        """Create one operation and all supplied initial events atomically."""
        _validate_operation_ownership(scope, operation)
        for event in events:
            _validate_event_ownership(scope, event)
        operation_sql = f"""
            INSERT INTO case_management.order_operations ({_OPERATION_COLUMNS})
            VALUES ({', '.join(['%s'] * 26)})
        """
        event_sql = f"""
            INSERT INTO case_management.order_operation_events ({_EVENT_COLUMNS})
            VALUES ({', '.join(['%s'] * 12)})
        """
        await self._write(
            operation_sql=operation_sql,
            operation_values=_operation_values(operation),
            event_sql=event_sql,
            events=events,
        )

    async def update_operation_with_events(
        self,
        scope: AccessScope,
        *,
        operation: OrderOperation,
        events: tuple[OrderOperationEvent, ...],
        expected_version: int,
    ) -> None:
        """Optimistically update one operation and append events atomically."""
        _validate_operation_ownership(scope, operation)
        for event in events:
            _validate_event_ownership(scope, event)
        visibility, visibility_params = customer_owned_visibility(scope)
        operation_sql = f"""
            UPDATE case_management.order_operations
            SET policy_reason_codes = %s, display_reason = %s,
                review_case_type = %s, review_priority = %s,
                support_case_id = %s, provider_reference = %s, status = %s,
                updated_at = %s, version = %s
            WHERE operation_id = %s AND version = %s AND {visibility}
        """
        values = (
            list(operation.policy_reason_codes),
            operation.display_reason,
            operation.review_case_type,
            operation.review_priority,
            operation.support_case_id,
            operation.provider_reference,
            operation.status,
            operation.updated_at,
            operation.version,
            operation.operation_id,
            expected_version,
            *visibility_params,
        )
        event_sql = f"""
            INSERT INTO case_management.order_operation_events ({_EVENT_COLUMNS})
            VALUES ({', '.join(['%s'] * 12)})
        """
        await self._write(
            operation_sql=operation_sql,
            operation_values=values,
            event_sql=event_sql,
            events=events,
            expect_update=True,
        )

    async def _fetch_operation(
        self,
        query: str,
        values: tuple[Any, ...],
    ) -> OrderOperation | None:
        """Read one operation and translate database failures."""
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(query, values)
                    row = await cursor.fetchone()
        except PoolTimeout as error:
            raise OperationPersistenceError("Timed out acquiring PostgreSQL connection") from error
        except errors.DatabaseError as error:
            raise OperationPersistenceError("Could not read order operation") from error
        return _operation_from_row(row) if row is not None else None

    async def _write(
        self,
        *,
        operation_sql: str,
        operation_values: tuple[Any, ...],
        event_sql: str,
        events: tuple[OrderOperationEvent, ...],
        expect_update: bool = False,
    ) -> None:
        """Execute an aggregate write and event inserts in one transaction."""
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor() as cursor:
                        await cursor.execute(operation_sql, operation_values)
                        if expect_update and cursor.rowcount != 1:
                            raise ConcurrentOperationUpdateError("Operation version changed")
                        for event in events:
                            await cursor.execute(event_sql, _event_values(event))
        except ConcurrentOperationUpdateError:
            raise
        except errors.UniqueViolation as error:
            _raise_unique_violation(error)
        except errors.DatabaseError as error:
            raise OperationPersistenceError("Could not persist order operation") from error
        except PoolTimeout as error:
            raise OperationPersistenceError("Timed out acquiring PostgreSQL connection") from error
