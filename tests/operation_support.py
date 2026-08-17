"""Reusable in-memory persistence helpers for operation workflow tests."""

from uuid import UUID

from agent.operations.models import OrderOperation, OrderOperationEvent
from agent.operations.repository import (
    ActiveOrderOperationConflictError,
    ConcurrentOperationUpdateError,
    DuplicateOperationIdempotencyError,
    DuplicateOperationSourceMessageError,
)

_ACTIVE_STATUSES = {"pending_confirmation", "submitted", "processing", "manual_review"}


class InMemoryOperationRepository:
    """Store operations and immutable events for offline graph tests."""

    def __init__(self) -> None:
        self.operations: dict[UUID, OrderOperation] = {}
        self.events: list[OrderOperationEvent] = []

    async def get_operation(self, operation_id: UUID) -> OrderOperation | None:
        return self.operations.get(operation_id)

    async def find_by_source_message(self, *, thread_id: str, source_message_id: str):
        return next(
            (operation for operation in self.operations.values() if operation.thread_id == thread_id and operation.source_message_id == source_message_id),
            None,
        )

    async def find_event_by_idempotency_key(self, idempotency_key: str):
        return next((event for event in self.events if event.idempotency_key == idempotency_key), None)

    async def find_active_by_order_id(self, order_id: str):
        return next(
            (operation for operation in self.operations.values() if operation.order_id == order_id and operation.status in _ACTIVE_STATUSES),
            None,
        )

    async def create_operation_with_events(self, *, operation: OrderOperation, events: tuple[OrderOperationEvent, ...]) -> None:
        if await self.find_by_source_message(thread_id=operation.thread_id, source_message_id=operation.source_message_id):
            raise DuplicateOperationSourceMessageError(operation.source_message_id)
        if await self.find_active_by_order_id(operation.order_id):
            raise ActiveOrderOperationConflictError(operation.order_id)
        for event in events:
            if await self.find_event_by_idempotency_key(event.idempotency_key):
                raise DuplicateOperationIdempotencyError(operation.idempotency_key)
        self.operations[operation.operation_id] = operation
        self.events.extend(events)

    async def update_operation_with_events(self, *, operation: OrderOperation, events: tuple[OrderOperationEvent, ...], expected_version: int) -> None:
        current = self.operations.get(operation.operation_id)
        if current is None or current.version != expected_version:
            raise ConcurrentOperationUpdateError(str(operation.operation_id))
        for event in events:
            if await self.find_event_by_idempotency_key(event.idempotency_key):
                raise DuplicateOperationIdempotencyError(operation.idempotency_key)
        self.operations[operation.operation_id] = operation
        self.events.extend(events)
