"""Define persistence boundaries for order operations."""

from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from agent.auth.models import AccessScope
from agent.integrations.models import (
    OrderOperationCommandPayload,
    ProviderCommandEnvelope,
)
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


def _revalidate_command_envelope(
    command: ProviderCommandEnvelope,
) -> ProviderCommandEnvelope:
    """Re-run the full envelope validation from the serialized form.

    Pydantic may return an existing model instance unchanged from
    ``model_validate(command)``, so a ``model_copy``-tampered envelope could
    bypass the validators. Serializing first forces every validator to run
    again. The original ``ValidationError`` is preserved as the cause.
    """
    try:
        return ProviderCommandEnvelope.model_validate(
            command.model_dump(mode="python")
        )
    except ValidationError as error:
        raise ValueError("command envelope is invalid") from error


def validate_operation_command_association(
    *,
    operation: OrderOperation,
    events: tuple[OrderOperationEvent, ...],
    command: ProviderCommandEnvelope,
    expected_version: int,
) -> None:
    """Reject atomic queue writes whose parts do not describe the same aggregate.

    Called before any connection or transaction is acquired; a failure leaves
    no domain row, event, or outbox write behind. Association is verified on
    typed identifiers only, never on display text.
    """
    command = _revalidate_command_envelope(command)
    if operation.status != "queued":
        raise ValueError("queue_operation requires a queued operation")
    if operation.version != expected_version + 1:
        raise ValueError("operation.version must equal expected_version + 1")
    if command.aggregate_type != "order_operation":
        raise ValueError("command.aggregate_type must be 'order_operation'")
    if command.aggregate_id != operation.operation_id:
        raise ValueError("command.aggregate_id must match the operation_id")
    if command.tenant_id != operation.tenant_id:
        raise ValueError("command.tenant_id must match the operation tenant_id")
    if command.customer_id != operation.customer_id:
        raise ValueError("command.customer_id must match the operation customer_id")
    if command.source_message_id != operation.source_message_id:
        raise ValueError(
            "command.source_message_id must match the operation source_message_id"
        )
    if command.expected_order_version != operation.order_version:
        raise ValueError(
            "command.expected_order_version must match the operation order_version"
        )
    if not isinstance(command.payload, OrderOperationCommandPayload):
        raise ValueError("command payload must be an order-operation payload")
    if command.payload.order_id != operation.order_id:
        raise ValueError("command payload order_id must match the operation order_id")
    if command.payload.operation_type != operation.operation_type:
        raise ValueError(
            "command payload operation_type must match the operation operation_type"
        )
    for event in events:
        if event.operation_id != operation.operation_id:
            raise ValueError("event.operation_id must match the operation_id")
        if event.tenant_id != operation.tenant_id:
            raise ValueError("event.tenant_id must match the operation tenant_id")
        if event.customer_id != operation.customer_id:
            raise ValueError("event.customer_id must match the operation customer_id")


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

    async def queue_operation_with_events_and_command(
        self,
        scope: AccessScope,
        *,
        operation: OrderOperation,
        events: tuple[OrderOperationEvent, ...],
        command: ProviderCommandEnvelope,
        expected_version: int,
    ) -> None:
        """Atomically update the operation, append events, and enqueue the command.

        The operation update, its domain events, and the outbox row are written
        in one PostgreSQL transaction; any failure rolls back all three.
        """
        ...
