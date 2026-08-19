"""Unit tests for the queued order-operation status and atomic outbox queueing."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from agent.integrations.models import ProviderCommandEnvelope
from agent.operations.models import (
    OperationDecision,
    OrderOperation,
    OrderOperationEvent,
    OrderOperationRequest,
    OrderSnapshot,
)
from agent.operations.service import (
    InvalidOperationStatusTransition,
    OperationService,
)
from tests.fakes.identity import make_scope
from tests.operation_support import InMemoryOperationRepository

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
SCOPE = make_scope("customer")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _snapshot() -> OrderSnapshot:
    return OrderSnapshot(
        order_id="ORD-10001",
        version=3,
        amount=Decimal("69.99"),
        currency="USD",
        order_status="confirmed",
        payment_status="paid",
        fulfillment_status="delivered",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        shipped_at=datetime(2026, 8, 2, tzinfo=UTC),
        delivered_at=datetime(2026, 8, 10, tzinfo=UTC),
        return_eligible=True,
        exchange_eligible=True,
        customer_id="customer-a",
        tenant_id="tenant-demo",
    )


def _request() -> OrderOperationRequest:
    return OrderOperationRequest(
        thread_id="thread-1",
        source_message_id="message-1",
        order_id="ORD-10001",
        operation_type="return",
        reason="damaged_item",
        customer_id="customer-a",
        tenant_id="tenant-demo",
    )


def _decision() -> OperationDecision:
    return OperationDecision(
        outcome="eligible",
        operation_type="return",
        requires_confirmation=True,
        reason_codes=("return_eligible",),
        display_reason="This order is eligible for return.",
    )


def _service() -> OperationService:
    return OperationService(InMemoryOperationRepository(), clock=lambda: NOW)


async def _pending_operation(service: OperationService):
    return await service.create_pending_operation(
        SCOPE,
        request=_request(),
        snapshot=_snapshot(),
        decision=_decision(),
        request_excerpt="Return this item.",
    )


async def test_pending_confirmation_can_transition_to_queued() -> None:
    service = _service()
    created = await _pending_operation(service)

    result = await service.update_operation_status(
        SCOPE,
        operation_id=created.operation.operation_id,
        target_status="queued",
        request_id="queue-1",
        actor="system",
    )

    assert result.action == "status_changed"
    assert result.operation.status == "queued"


async def test_queued_can_transition_forward() -> None:
    service = _service()
    created = await _pending_operation(service)
    await service.update_operation_status(
        SCOPE,
        operation_id=created.operation.operation_id,
        target_status="queued",
        request_id="queue-1",
        actor="system",
    )

    submitted = await service.update_operation_status(
        SCOPE,
        operation_id=created.operation.operation_id,
        target_status="submitted",
        request_id="submit-1",
        actor="system",
    )
    assert submitted.operation.status == "submitted"


async def test_queued_cannot_return_to_pending_confirmation() -> None:
    service = _service()
    created = await _pending_operation(service)
    await service.update_operation_status(
        SCOPE,
        operation_id=created.operation.operation_id,
        target_status="queued",
        request_id="queue-1",
        actor="system",
    )

    with pytest.raises(InvalidOperationStatusTransition):
        await service.update_operation_status(
            SCOPE,
            operation_id=created.operation.operation_id,
            target_status="pending_confirmation",
            request_id="back-1",
            actor="system",
        )


async def test_cancelled_operation_cannot_be_queued() -> None:
    service = _service()
    created = await _pending_operation(service)
    await service.cancel_pending_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="cancel-1",
    )

    with pytest.raises(InvalidOperationStatusTransition):
        await service.update_operation_status(
            SCOPE,
            operation_id=created.operation.operation_id,
            target_status="queued",
            request_id="queue-1",
            actor="system",
        )


async def test_queue_operation_records_command_in_memory() -> None:
    repository = InMemoryOperationRepository()
    service = OperationService(repository, clock=lambda: NOW)
    created = await _pending_operation(service)

    queued = created.operation.model_copy(
        update={
            "status": "queued",
            "version": created.operation.version + 1,
            "updated_at": NOW,
        }
    )
    event = OrderOperationEvent(
        event_id=uuid4(),
        idempotency_key=f"operation:{queued.operation_id}:status:queue-1",
        operation_id=queued.operation_id,
        event_type="status_changed",
        previous_status="pending_confirmation",
        current_status="queued",
        actor="system",
        customer_id="customer-a",
        tenant_id="tenant-demo",
        created_at=NOW,
    )
    command = ProviderCommandEnvelope.for_order_operation(
        operation=queued,
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )

    await repository.queue_operation_with_events_and_command(
        SCOPE,
        operation=queued,
        events=(event,),
        command=command,
        expected_version=created.operation.version,
    )

    stored = repository.operations[queued.operation_id]
    assert stored.status == "queued"
    assert stored.version == 2
    assert repository.outbox_commands[command.command_id] == command
    assert command.aggregate_type == "order_operation"
    assert command.expected_order_version == 3


async def test_queue_operation_rejects_non_queued_operation() -> None:
    repository = InMemoryOperationRepository()
    created = await _pending_operation(OperationService(repository, clock=lambda: NOW))

    with pytest.raises(ValueError, match="queued"):
        await repository.queue_operation_with_events_and_command(
            SCOPE,
            operation=created.operation,
            events=(),
            command=ProviderCommandEnvelope.for_order_operation(
                operation=created.operation,
                connection_id="conn-1",
                command_id=uuid4(),
                created_at=NOW,
            ),
            expected_version=created.operation.version,
        )


async def _queued_pair() -> tuple[InMemoryOperationRepository, OrderOperation, ProviderCommandEnvelope, OrderOperationEvent]:
    repository = InMemoryOperationRepository()
    created = await _pending_operation(OperationService(repository, clock=lambda: NOW))
    queued = created.operation.model_copy(
        update={
            "status": "queued",
            "version": created.operation.version + 1,
            "updated_at": NOW,
        }
    )
    event = OrderOperationEvent(
        event_id=uuid4(),
        idempotency_key=f"operation:{queued.operation_id}:status:queue-1",
        operation_id=queued.operation_id,
        event_type="status_changed",
        previous_status="pending_confirmation",
        current_status="queued",
        actor="system",
        customer_id="customer-a",
        tenant_id="tenant-demo",
        created_at=NOW,
    )
    command = ProviderCommandEnvelope.for_order_operation(
        operation=queued,
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )
    return repository, queued, command, event


async def test_queue_rejects_mismatched_aggregate_id() -> None:
    repository, queued, command, event = await _queued_pair()
    other_id = uuid4()
    # The tampered envelope stays internally valid (idempotency key follows
    # the new aggregate id) so the association check itself is exercised.
    mismatched = command.model_copy(
        update={
            "aggregate_id": other_id,
            "idempotency_key": f"order-operation:{other_id}",
        }
    )

    with pytest.raises(ValueError, match="aggregate_id"):
        await repository.queue_operation_with_events_and_command(
            SCOPE,
            operation=queued,
            events=(event,),
            command=mismatched,
            expected_version=1,
        )
    assert repository.outbox_commands == {}
    assert repository.operations[queued.operation_id].status == "pending_confirmation"


async def test_queue_rejects_mismatched_tenant_id() -> None:
    repository, queued, command, event = await _queued_pair()
    mismatched = command.model_copy(update={"tenant_id": "tenant-other"})

    with pytest.raises(ValueError, match="tenant_id"):
        await repository.queue_operation_with_events_and_command(
            SCOPE,
            operation=queued,
            events=(event,),
            command=mismatched,
            expected_version=1,
        )
    assert repository.outbox_commands == {}


async def test_queue_rejects_mismatched_payload_order_id() -> None:
    repository, queued, command, event = await _queued_pair()
    mismatched = command.model_copy(
        update={"payload": command.payload.model_copy(update={"order_id": "ORD-99999"})}
    )

    with pytest.raises(ValueError, match="order_id"):
        await repository.queue_operation_with_events_and_command(
            SCOPE,
            operation=queued,
            events=(event,),
            command=mismatched,
            expected_version=1,
        )
    assert repository.outbox_commands == {}


async def test_queue_rejects_version_mismatch() -> None:
    repository, queued, command, event = await _queued_pair()

    with pytest.raises(ValueError, match="version"):
        await repository.queue_operation_with_events_and_command(
            SCOPE,
            operation=queued,
            events=(event,),
            command=command,
            expected_version=99,
        )
    assert repository.outbox_commands == {}


async def test_queue_rejects_mismatched_event_operation_id() -> None:
    repository, queued, command, event = await _queued_pair()
    mismatched_event = event.model_copy(update={"operation_id": uuid4()})

    with pytest.raises(ValueError, match="event.operation_id"):
        await repository.queue_operation_with_events_and_command(
            SCOPE,
            operation=queued,
            events=(mismatched_event,),
            command=command,
            expected_version=1,
        )
    assert repository.outbox_commands == {}


async def test_queue_rejects_tampered_idempotency_key() -> None:
    repository, queued, command, event = await _queued_pair()
    tampered = command.model_copy(
        update={"idempotency_key": "order-operation:00000000-0000-0000-0000-000000000000"}
    )

    with pytest.raises(ValueError, match="command envelope is invalid"):
        await repository.queue_operation_with_events_and_command(
            SCOPE,
            operation=queued,
            events=(event,),
            command=tampered,
            expected_version=1,
        )
    assert repository.outbox_commands == {}


async def test_queue_rejects_tampered_command_type() -> None:
    repository, queued, command, event = await _queued_pair()
    # The payload is a return payload; cancel_order no longer matches it.
    tampered = command.model_copy(update={"command_type": "cancel_order"})

    with pytest.raises(ValueError, match="command envelope is invalid"):
        await repository.queue_operation_with_events_and_command(
            SCOPE,
            operation=queued,
            events=(event,),
            command=tampered,
            expected_version=1,
        )
    assert repository.outbox_commands == {}


async def test_queue_rejects_tampered_aggregate_type() -> None:
    repository, queued, command, event = await _queued_pair()
    tampered = command.model_copy(update={"aggregate_type": "support_case"})

    with pytest.raises(ValueError, match="command envelope is invalid"):
        await repository.queue_operation_with_events_and_command(
            SCOPE,
            operation=queued,
            events=(event,),
            command=tampered,
            expected_version=1,
        )
    assert repository.outbox_commands == {}


async def test_queue_rejects_delivery_payload_in_order_command() -> None:
    from agent.integrations.models import DeliveryInvestigationCommandPayload

    repository, queued, command, event = await _queued_pair()
    tampered = command.model_copy(
        update={
            "payload": DeliveryInvestigationCommandPayload(
                order_id="ORD-10001",
                issue_type="tracking_stalled",
            )
        }
    )

    with pytest.raises(ValueError, match="command envelope is invalid"):
        await repository.queue_operation_with_events_and_command(
            SCOPE,
            operation=queued,
            events=(event,),
            command=tampered,
            expected_version=1,
        )
    assert repository.outbox_commands == {}
