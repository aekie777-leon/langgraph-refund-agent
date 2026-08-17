"""Define persistence boundaries for order operations."""

from typing import Protocol
from uuid import UUID

from agent.auth.models import AccessScope
from agent.operations.models import OrderOperation, OrderOperationEvent


class DuplicateOperationSourceMessageError(RuntimeError):
    """Report that a source message already created an operation."""


class DuplicateOperationIdempotencyError(RuntimeError):
    """Report that an operation event idempotency key already exists."""


class ActiveOrderOperationConflictError(RuntimeError):
    """Report a concurrent unresolved operation for the same order."""


class ConcurrentOperationUpdateError(RuntimeError):
    """Report an optimistic-lock version conflict."""


class OperationNotFoundError(LookupError):
    """Report that an order operation does not exist."""


class OperationPersistenceError(RuntimeError):
    """Report an unexpected order-operation persistence failure."""


class OperationRepository(Protocol):
    """Define storage operations required by the order-operation service."""

    async def get_operation(
        self,
        scope: AccessScope,
        operation_id: UUID,
    ) -> OrderOperation | None:
        """Return an operation by ID within the caller's access scope."""
        ...

    async def find_by_source_message(
        self,
        scope: AccessScope,
        *,
        thread_id: str,
        source_message_id: str,
    ) -> OrderOperation | None:
        """Find an operation already associated with a source message."""
        ...

    async def find_event_by_idempotency_key(
        self,
        scope: AccessScope,
        idempotency_key: str,
    ) -> OrderOperationEvent | None:
        """Find a previously recorded immutable operation event."""
        ...

    async def find_active_by_order_id(
        self,
        scope: AccessScope,
        order_id: str,
    ) -> OrderOperation | None:
        """Find the unresolved operation currently blocking an order."""
        ...

    async def create_operation_with_events(
        self,
        scope: AccessScope,
        *,
        operation: OrderOperation,
        events: tuple[OrderOperationEvent, ...],
    ) -> None:
        """Atomically create an operation and its initial immutable events."""
        ...

    async def update_operation_with_events(
        self,
        scope: AccessScope,
        *,
        operation: OrderOperation,
        events: tuple[OrderOperationEvent, ...],
        expected_version: int,
    ) -> None:
        """Atomically update an operation and append immutable events."""
        ...
