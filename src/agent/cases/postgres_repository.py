"""Persist support cases and immutable events in PostgreSQL."""

from collections.abc import Mapping, Sequence
from typing import Any, NoReturn
from uuid import UUID

import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.cases.models import (
    CaseListQuery,
    CaseType,
    SupportCase,
    SupportCaseEvent,
    SupportCaseEventPage,
    SupportCasePage,
)
from agent.cases.repository import (
    ActiveCaseConflictError,
    CasePersistenceError,
    CaseRepository,
    ConcurrentCaseUpdateError,
    DuplicateIdempotencyKeyError,
    DuplicateSourceMessageError,
)

_ACTIVE_CASE_CONSTRAINT = "uq_support_cases_active_thread_type"
_EVENT_IDEMPOTENCY_CONSTRAINT = "uq_support_case_events_idempotency"

_CASE_COLUMNS = """
    case_id,
    thread_id,
    source_message_id,
    order_id,
    case_type,
    priority,
    status,
    risk_level,
    risk_categories,
    reason_codes,
    display_reason,
    triggering_message_excerpt,
    on_hold_reason,
    created_at,
    updated_at,
    version
"""

_EVENT_COLUMNS = """
    event_id,
    idempotency_key,
    case_id,
    event_type,
    source_message_id,
    order_id,
    risk_level,
    risk_categories,
    reason_codes,
    triggering_message_excerpt,
    previous_priority,
    current_priority,
    previous_status,
    current_status,
    on_hold_reason,
    actor,
    created_at
"""

_INSERT_CASE = f"""
    INSERT INTO case_management.support_cases ({_CASE_COLUMNS})
    VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s
    )
"""

_INSERT_EVENT = f"""
    INSERT INTO case_management.support_case_events ({_EVENT_COLUMNS})
    VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s
    )
"""

_UPDATE_CASE = """
    UPDATE case_management.support_cases
    SET
        order_id = %s,
        priority = %s,
        status = %s,
        risk_level = %s,
        risk_categories = %s,
        reason_codes = %s,
        display_reason = %s,
        on_hold_reason = %s,
        updated_at = %s,
        version = %s
    WHERE case_id = %s AND version = %s
"""


def _case_from_row(row: Mapping[str, Any]) -> SupportCase:
    """Validate a database row as a support-case domain model."""
    return SupportCase.model_validate(row)


def _event_from_row(row: Mapping[str, Any]) -> SupportCaseEvent:
    """Validate a database row as an immutable case event."""
    return SupportCaseEvent.model_validate(row)


def _case_values(case: SupportCase) -> tuple[Any, ...]:
    """Return SQL parameters in the insert column order."""
    return (
        case.case_id,
        case.thread_id,
        case.source_message_id,
        case.order_id,
        case.case_type,
        case.priority,
        case.status,
        case.risk_level,
        list(case.risk_categories),
        list(case.reason_codes),
        case.display_reason,
        case.triggering_message_excerpt,
        case.on_hold_reason,
        case.created_at,
        case.updated_at,
        case.version,
    )


def _event_values(event: SupportCaseEvent) -> tuple[Any, ...]:
    """Return SQL parameters in the event insert column order."""
    return (
        event.event_id,
        event.idempotency_key,
        event.case_id,
        event.event_type,
        event.source_message_id,
        event.order_id,
        event.risk_level,
        list(event.risk_categories),
        list(event.reason_codes),
        event.triggering_message_excerpt,
        event.previous_priority,
        event.current_priority,
        event.previous_status,
        event.current_status,
        event.on_hold_reason,
        event.actor,
        event.created_at,
    )


def _update_values(
    case: SupportCase,
    expected_version: int,
) -> tuple[Any, ...]:
    """Return SQL parameters for one optimistic-lock update."""
    return (
        case.order_id,
        case.priority,
        case.status,
        case.risk_level,
        list(case.risk_categories),
        list(case.reason_codes),
        case.display_reason,
        case.on_hold_reason,
        case.updated_at,
        case.version,
        case.case_id,
        expected_version,
    )


def _raise_unique_violation(
    error: errors.UniqueViolation,
    event: SupportCaseEvent,
) -> NoReturn:
    """Translate named PostgreSQL constraints into repository exceptions."""
    constraint = error.diag.constraint_name
    if constraint == _ACTIVE_CASE_CONSTRAINT:
        raise ActiveCaseConflictError(str(event.case_id)) from error
    if constraint == _EVENT_IDEMPOTENCY_CONSTRAINT:
        if event.source_message_id is not None:
            raise DuplicateSourceMessageError(event.source_message_id) from error
        raise DuplicateIdempotencyKeyError(event.idempotency_key) from error
    raise CasePersistenceError(
        f"Unexpected unique constraint violation: {constraint}"
    ) from error


def _case_filter_sql(
    query: CaseListQuery,
) -> tuple[str, list[Any]]:
    """Build a parameterized WHERE clause from validated filter fields."""
    clauses: list[str] = []
    parameters: list[Any] = []

    for column, value in (
        ("status", query.status),
        ("priority", query.priority),
        ("case_type", query.case_type),
        ("thread_id", query.thread_id),
        ("order_id", query.order_id),
    ):
        if value is not None:
            clauses.append(f"{column} = %s")
            parameters.append(value)

    if not clauses:
        return "", parameters
    return f"WHERE {' AND '.join(clauses)}", parameters


class PostgresCaseRepository(CaseRepository):
    """Implement atomic support-case persistence using an async connection pool."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Store a pool whose lifecycle is owned by the application."""
        self._pool = pool

    async def get_case(self, case_id: UUID) -> SupportCase | None:
        """Return a case by ID."""
        query = f"""
            SELECT {_CASE_COLUMNS}
            FROM case_management.support_cases
            WHERE case_id = %s
        """
        return await self._fetch_case(query, (case_id,))

    async def find_by_source_message(
        self,
        *,
        thread_id: str,
        source_message_id: str,
    ) -> SupportCase | None:
        """Find the case already associated with a triggering message."""
        query = f"""
            SELECT {", ".join(f"cases.{column.strip()}" for column in _CASE_COLUMNS.split(","))}
            FROM case_management.support_cases AS cases
            JOIN case_management.support_case_events AS events
              ON events.case_id = cases.case_id
            WHERE cases.thread_id = %s
              AND events.source_message_id = %s
            ORDER BY events.created_at, events.event_id
            LIMIT 1
        """
        return await self._fetch_case(query, (thread_id, source_message_id))

    async def find_event_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> SupportCaseEvent | None:
        """Find a previously recorded operation event."""
        query = f"""
            SELECT {_EVENT_COLUMNS}
            FROM case_management.support_case_events
            WHERE idempotency_key = %s
        """
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(query, (idempotency_key,))
                    row = await cursor.fetchone()
        except (psycopg.Error, PoolTimeout) as error:
            raise CasePersistenceError("Failed to read support-case event") from error
        return None if row is None else _event_from_row(row)

    async def find_unresolved_case(
        self,
        *,
        thread_id: str,
        case_type: CaseType,
    ) -> SupportCase | None:
        """Find an unresolved case with the same thread and type."""
        query = f"""
            SELECT {_CASE_COLUMNS}
            FROM case_management.support_cases
            WHERE thread_id = %s
              AND case_type = %s
              AND status IN ('open', 'in_progress', 'on_hold')
            ORDER BY created_at, case_id
            LIMIT 1
        """
        return await self._fetch_case(query, (thread_id, case_type))

    async def list_cases(
        self,
        query: CaseListQuery,
    ) -> SupportCasePage:
        """Return a filtered page ordered as an operational work queue."""
        where_sql, parameters = _case_filter_sql(query)
        count_query = f"""
            SELECT COUNT(*) AS total
            FROM case_management.support_cases
            {where_sql}
        """
        page_query = f"""
            SELECT {_CASE_COLUMNS}
            FROM case_management.support_cases
            {where_sql}
            ORDER BY
                CASE priority
                    WHEN 'p0' THEN 0
                    WHEN 'p1' THEN 1
                    WHEN 'p2' THEN 2
                    WHEN 'p3' THEN 3
                END,
                created_at,
                case_id
            LIMIT %s OFFSET %s
        """

        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(count_query, parameters)
                    count_row = await cursor.fetchone()
                    await cursor.execute(
                        page_query,
                        (*parameters, query.limit, query.offset),
                    )
                    rows = await cursor.fetchall()
        except (psycopg.Error, PoolTimeout) as error:
            raise CasePersistenceError("Failed to list support cases") from error

        total = int(count_row["total"]) if count_row is not None else 0
        return SupportCasePage(
            items=tuple(_case_from_row(row) for row in rows),
            total=total,
            limit=query.limit,
            offset=query.offset,
        )

    async def list_case_events(
        self,
        *,
        case_id: UUID,
        limit: int,
        offset: int,
    ) -> SupportCaseEventPage:
        """Return a stable page of events for one support case."""
        count_query = """
            SELECT COUNT(*) AS total
            FROM case_management.support_case_events
            WHERE case_id = %s
        """
        page_query = f"""
            SELECT {_EVENT_COLUMNS}
            FROM case_management.support_case_events
            WHERE case_id = %s
            ORDER BY created_at, event_id
            LIMIT %s OFFSET %s
        """

        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(count_query, (case_id,))
                    count_row = await cursor.fetchone()
                    await cursor.execute(
                        page_query,
                        (case_id, limit, offset),
                    )
                    rows = await cursor.fetchall()
        except (psycopg.Error, PoolTimeout) as error:
            raise CasePersistenceError("Failed to list support-case events") from error

        total = int(count_row["total"]) if count_row is not None else 0
        return SupportCaseEventPage(
            items=tuple(_event_from_row(row) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    async def create_case_with_event(
        self,
        *,
        case: SupportCase,
        event: SupportCaseEvent,
    ) -> None:
        """Atomically create a case and its first event."""
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    await connection.execute(_INSERT_CASE, _case_values(case))
                    await connection.execute(_INSERT_EVENT, _event_values(event))
        except errors.UniqueViolation as error:
            _raise_unique_violation(error, event)
        except (psycopg.Error, PoolTimeout) as error:
            raise CasePersistenceError("Failed to create support case") from error

    async def update_case_with_event(
        self,
        *,
        case: SupportCase,
        event: SupportCaseEvent,
        expected_version: int,
    ) -> None:
        """Atomically update a case and append an event."""
        if case.version != expected_version + 1:
            raise ValueError("case.version must equal expected_version + 1")

        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    result = await connection.execute(
                        _UPDATE_CASE,
                        _update_values(case, expected_version),
                    )
                    if result.rowcount != 1:
                        raise ConcurrentCaseUpdateError(str(case.case_id))
                    await connection.execute(_INSERT_EVENT, _event_values(event))
        except errors.UniqueViolation as error:
            _raise_unique_violation(error, event)
        except ConcurrentCaseUpdateError:
            raise
        except (psycopg.Error, PoolTimeout) as error:
            raise CasePersistenceError("Failed to update support case") from error

    async def _fetch_case(
        self,
        query: str,
        parameters: Sequence[Any],
    ) -> SupportCase | None:
        """Run a single-row case query and validate its result."""
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(query, parameters)
                    row = await cursor.fetchone()
        except (psycopg.Error, PoolTimeout) as error:
            raise CasePersistenceError("Failed to read support case") from error
        return None if row is None else _case_from_row(row)
