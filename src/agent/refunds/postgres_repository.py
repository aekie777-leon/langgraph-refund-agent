"""Persist refund requests in PostgreSQL using the application pool."""

from collections.abc import Mapping
from typing import Any

from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.auth.models import AccessScope
from agent.auth.visibility import customer_owned_visibility
from agent.refunds.models import RefundRequest
from agent.refunds.repository import RefundPersistenceError, RefundRepository

_COLUMNS = "refund_id, order_id, status, customer_id, tenant_id, created_by, created_at"


def _refund_from_row(row: Mapping[str, Any]) -> RefundRequest:
    """Validate a database row as a refund-request domain model."""
    return RefundRequest.model_validate(row)


def _refund_values(refund: RefundRequest) -> tuple[Any, ...]:
    """Return SQL parameters in the insert column order."""
    return (
        refund.refund_id,
        refund.order_id,
        refund.status,
        refund.customer_id,
        refund.tenant_id,
        refund.created_by,
        refund.created_at,
    )


class PostgresRefundRepository(RefundRepository):
    """Implement idempotent refund persistence with an async connection pool."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Store a pool whose lifecycle is owned by the application."""
        self._pool = pool

    async def get_by_order_id(
        self,
        scope: AccessScope,
        order_id: str,
    ) -> RefundRequest | None:
        """Return the caller-scoped refund request for one order."""
        visibility, parameters = customer_owned_visibility(scope)
        query = f"""
            SELECT {_COLUMNS}
            FROM refund_requests
            WHERE order_id = %s AND {visibility}
        """
        try:
            async with self._pool.connection() as connection:
                async with connection.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(query, (order_id, *parameters))
                    row = await cursor.fetchone()
        except (errors.DatabaseError, PoolTimeout) as error:
            raise RefundPersistenceError("Failed to read refund request") from error
        return None if row is None else _refund_from_row(row)

    async def create(
        self,
        scope: AccessScope,
        *,
        refund: RefundRequest,
    ) -> bool:
        """Insert one refund request idempotently for the caller's order."""
        customer_owned_visibility(scope)
        if refund.customer_id != scope.customer_id or refund.tenant_id != scope.tenant_id:
            raise ValueError("refund ownership must match the access scope")
        query = f"""
            INSERT INTO refund_requests ({_COLUMNS})
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (order_id) DO NOTHING
            RETURNING refund_id
        """
        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    async with connection.cursor() as cursor:
                        await cursor.execute(query, _refund_values(refund))
                        return await cursor.fetchone() is not None
        except (errors.DatabaseError, PoolTimeout) as error:
            raise RefundPersistenceError("Failed to create refund request") from error
