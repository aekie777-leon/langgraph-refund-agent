"""Reusable in-memory persistence helpers for operation workflow tests."""

from uuid import UUID

from agent.auth.models import AccessScope
from agent.operations.models import OrderOperation, OrderOperationEvent
from agent.operations.repository import (
    ActiveOrderOperationConflictError,
    ConcurrentOperationUpdateError,
    DuplicateOperationIdempotencyError,
    DuplicateOperationSourceMessageError,
)

_ACTIVE_STATUSES = {"pending_confirmation", "submitted", "processing", "manual_review"}


def _operation_visible(scope: AccessScope, operation: OrderOperation) -> bool:
    """Return whether an operation is visible within a customer scope."""
    return (
        operation.tenant_id == scope.tenant_id
        and operation.customer_id == scope.customer_id
    )


def _event_visible(scope: AccessScope, event: OrderOperationEvent) -> bool:
    """Return whether an operation event is visible within a customer scope."""
    return (
        event.tenant_id == scope.tenant_id
        and event.customer_id == scope.customer_id
    )


class InMemoryOperationRepository:
    """Store operations and immutable events for offline graph tests."""

    def __init__(self) -> None:
        self.operations: dict[UUID, OrderOperation] = {}
        self.events: list[OrderOperationEvent] = []

    async def get_operation(
        self,
        scope: AccessScope,
        operation_id: UUID,
    ) -> OrderOperation | None:
        operation = self.operations.get(operation_id)
        if operation is None or not _operation_visible(scope, operation):
            return None
        return operation

    async def find_by_source_message(
        self,
        scope: AccessScope,
        *,
        thread_id: str,
        source_message_id: str,
    ):
        return next(
            (
                operation
                for operation in self.operations.values()
                if _operation_visible(scope, operation)
                and operation.thread_id == thread_id
                and operation.source_message_id == source_message_id
            ),
            None,
        )

    async def find_event_by_idempotency_key(
        self,
        scope: AccessScope,
        idempotency_key: str,
    ):
        return next(
            (
                event
                for event in self.events
                if _event_visible(scope, event)
                and event.idempotency_key == idempotency_key
            ),
            None,
        )

    async def find_active_by_order_id(
        self,
        scope: AccessScope,
        order_id: str,
    ):
        return next(
            (
                operation
                for operation in self.operations.values()
                if _operation_visible(scope, operation)
                and operation.order_id == order_id
                and operation.status in _ACTIVE_STATUSES
            ),
            None,
        )

    async def create_operation_with_events(
        self,
        scope: AccessScope,
        *,
        operation: OrderOperation,
        events: tuple[OrderOperationEvent, ...],
    ) -> None:
        _validate_ownership(scope, operation.customer_id, operation.tenant_id)
        if await self.find_by_source_message(
            scope,
            thread_id=operation.thread_id,
            source_message_id=operation.source_message_id,
        ):
            raise DuplicateOperationSourceMessageError(operation.source_message_id)
        if await self.find_active_by_order_id(scope, operation.order_id):
            raise ActiveOrderOperationConflictError(operation.order_id)
        for event in events:
            if await self.find_event_by_idempotency_key(scope, event.idempotency_key):
                raise DuplicateOperationIdempotencyError(operation.idempotency_key)
        self.operations[operation.operation_id] = operation
        self.events.extend(events)

    async def update_operation_with_events(
        self,
        scope: AccessScope,
        *,
        operation: OrderOperation,
        events: tuple[OrderOperationEvent, ...],
        expected_version: int,
    ) -> None:
        current = self.operations.get(operation.operation_id)
        if current is None or not _operation_visible(scope, current):
            raise ConcurrentOperationUpdateError(str(operation.operation_id))
        if current.version != expected_version:
            raise ConcurrentOperationUpdateError(str(operation.operation_id))
        for event in events:
            if await self.find_event_by_idempotency_key(scope, event.idempotency_key):
                raise DuplicateOperationIdempotencyError(operation.idempotency_key)
        self.operations[operation.operation_id] = operation
        self.events.extend(events)


def _validate_ownership(scope: AccessScope, customer_id: str, tenant_id: str) -> None:
    """Reject writes whose ownership does not match the caller scope."""
    if tenant_id != scope.tenant_id:
        raise ValueError("tenant_id must match the access scope")
    if customer_id != scope.customer_id:
        raise ValueError("customer_id must match the access scope")
