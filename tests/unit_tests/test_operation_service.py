"""Unit tests for durable confirmation-gated order-operation behavior."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid5

import pytest

from agent.operations.models import (
    CaseRecommendation,
    OperationDecision,
    OrderOperation,
    OrderOperationEvent,
    OrderOperationRequest,
    OrderSnapshot,
)
from agent.operations.repository import (
    ActiveOrderOperationConflictError,
    ConcurrentOperationUpdateError,
    DuplicateOperationIdempotencyError,
    DuplicateOperationSourceMessageError,
)
from agent.operations.service import (
    InvalidOperationStatusTransition,
    OperationService,
)
from tests.fakes.operations import InMemoryOrderProvider

NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
_ACTIVE_STATUSES = {
    "pending_confirmation",
    "submitted",
    "processing",
    "manual_review",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _InMemoryOperationRepository:
    """Implement the operation repository contract without PostgreSQL."""

    def __init__(self) -> None:
        self.operations: dict[UUID, OrderOperation] = {}
        self.events: list[OrderOperationEvent] = []

    async def get_operation(self, operation_id: UUID) -> OrderOperation | None:
        return self.operations.get(operation_id)

    async def find_by_source_message(
        self,
        *,
        thread_id: str,
        source_message_id: str,
    ) -> OrderOperation | None:
        return next(
            (
                operation
                for operation in self.operations.values()
                if operation.thread_id == thread_id
                and operation.source_message_id == source_message_id
            ),
            None,
        )

    async def find_event_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> OrderOperationEvent | None:
        return next(
            (event for event in self.events if event.idempotency_key == idempotency_key),
            None,
        )

    async def find_active_by_order_id(self, order_id: str) -> OrderOperation | None:
        return next(
            (
                operation
                for operation in self.operations.values()
                if operation.order_id == order_id and operation.status in _ACTIVE_STATUSES
            ),
            None,
        )

    async def create_operation_with_events(
        self,
        *,
        operation: OrderOperation,
        events: tuple[OrderOperationEvent, ...],
    ) -> None:
        if await self.find_by_source_message(
            thread_id=operation.thread_id,
            source_message_id=operation.source_message_id,
        ):
            raise DuplicateOperationSourceMessageError(operation.source_message_id)
        if await self.find_active_by_order_id(operation.order_id):
            raise ActiveOrderOperationConflictError(operation.order_id)
        for event in events:
            if await self.find_event_by_idempotency_key(event.idempotency_key):
                raise DuplicateOperationIdempotencyError(event.idempotency_key)
        self.operations[operation.operation_id] = operation
        self.events.extend(events)

    async def update_operation_with_events(
        self,
        *,
        operation: OrderOperation,
        events: tuple[OrderOperationEvent, ...],
        expected_version: int,
    ) -> None:
        current = self.operations.get(operation.operation_id)
        if current is None or current.version != expected_version:
            raise ConcurrentOperationUpdateError(str(operation.operation_id))
        for event in events:
            if await self.find_event_by_idempotency_key(event.idempotency_key):
                raise DuplicateOperationIdempotencyError(event.idempotency_key)
        self.operations[operation.operation_id] = operation
        self.events.extend(events)


@pytest.fixture
def repository() -> _InMemoryOperationRepository:
    return _InMemoryOperationRepository()


@pytest.fixture
def service(repository: _InMemoryOperationRepository) -> OperationService:
    counter = 0

    def id_factory() -> UUID:
        nonlocal counter
        counter += 1
        return uuid5(UUID("00000000-0000-0000-0000-000000000001"), str(counter))

    return OperationService(repository, clock=lambda: NOW, id_factory=id_factory)


def _snapshot(*, amount: str = "69.99") -> OrderSnapshot:
    return OrderSnapshot(
        order_id="ORD-10001",
        version=3,
        amount=Decimal(amount),
        currency="USD",
        order_status="confirmed",
        payment_status="paid",
        fulfillment_status="delivered",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        shipped_at=datetime(2026, 8, 2, tzinfo=UTC),
        delivered_at=datetime(2026, 8, 10, tzinfo=UTC),
        return_eligible=True,
        exchange_eligible=True,
    )


def _request(*, message_id: str = "message-1") -> OrderOperationRequest:
    return OrderOperationRequest(
        thread_id="thread-1",
        source_message_id=message_id,
        order_id="ORD-10001",
        operation_type="return",
        reason="damaged_item",
    )


def _eligible_decision() -> OperationDecision:
    return OperationDecision(
        outcome="eligible",
        operation_type="return",
        requires_confirmation=True,
        reason_codes=("return_eligible",),
        display_reason="This order is eligible for return.",
    )


def _manual_decision() -> OperationDecision:
    return OperationDecision(
        outcome="manual_review",
        operation_type="return",
        requires_confirmation=True,
        reason_codes=("return_manual_amount_review",),
        display_reason="This return amount needs manual review.",
        case_recommendation=CaseRecommendation(
            case_type="order_operation_review",
            priority="p1",
            reason_codes=("return_manual_amount_review",),
        ),
    )


@pytest.mark.anyio
async def test_create_pending_operation_records_auditable_snapshot(
    service: OperationService,
    repository: _InMemoryOperationRepository,
) -> None:
    result = await service.create_pending_operation(
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="I would like to return the damaged item.",
    )

    assert result.action == "created"
    assert result.operation.status == "pending_confirmation"
    assert result.operation.order_version == 3
    assert result.operation.amount == Decimal("69.99")
    assert result.operation.request_reason_code == "damaged_item"
    assert result.operation.policy_reason_codes == ("return_eligible",)
    assert result.events[0].event_type == "operation_created"
    assert len(repository.operations) == 1


@pytest.mark.anyio
async def test_duplicate_source_message_does_not_create_a_second_operation(
    service: OperationService,
    repository: _InMemoryOperationRepository,
) -> None:
    first = await service.create_pending_operation(
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    duplicate = await service.create_pending_operation(
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )

    assert duplicate.action == "duplicate_ignored"
    assert duplicate.operation == first.operation
    assert len(repository.operations) == 1


@pytest.mark.anyio
async def test_active_operation_blocks_a_second_source_message(
    service: OperationService,
) -> None:
    await service.create_pending_operation(
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    result = await service.create_pending_operation(
        request=_request(message_id="message-2"),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="I still want to return it.",
    )

    assert result.action == "duplicate_ignored"
    assert result.operation.source_message_id == "message-1"


@pytest.mark.anyio
async def test_automatic_operation_requires_provider_submission(
    service: OperationService,
    repository: _InMemoryOperationRepository,
) -> None:
    created = await service.create_pending_operation(
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )

    with pytest.raises(ValueError, match="submit_confirmed_operation"):
        await service.confirm_operation(
            operation_id=created.operation.operation_id,
            request_id="confirm-1",
            actor="customer",
        )

    assert len(repository.events) == 1


@pytest.mark.anyio
async def test_manual_operation_confirmation_moves_to_manual_review(
    service: OperationService,
) -> None:
    created = await service.create_pending_operation(
        request=_request(),
        snapshot=_snapshot(amount="150.00"),
        decision=_manual_decision(),
        request_excerpt="Please return this expensive item.",
    )

    result = await service.confirm_operation(
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
        actor="customer",
    )

    assert result.operation.status == "manual_review"
    assert result.operation.review_case_type == "order_operation_review"
    assert result.operation.review_priority == "p1"


@pytest.mark.anyio
async def test_confirm_request_is_idempotent(
    service: OperationService,
    repository: _InMemoryOperationRepository,
) -> None:
    created = await service.create_pending_operation(
        request=_request(),
        snapshot=_snapshot(amount="150.00"),
        decision=_manual_decision(),
        request_excerpt="Please return this expensive item.",
    )
    first = await service.confirm_operation(
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
        actor="customer",
    )
    duplicate = await service.confirm_operation(
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
        actor="customer",
    )

    assert first.action == "confirmed"
    assert duplicate.action == "status_unchanged"
    assert len(repository.events) == 3


@pytest.mark.anyio
async def test_customer_can_cancel_only_before_confirmation(
    service: OperationService,
    repository: _InMemoryOperationRepository,
) -> None:
    created = await service.create_pending_operation(
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )

    result = await service.cancel_pending_operation(
        operation_id=created.operation.operation_id,
        request_id="cancel-1",
        actor="customer",
    )

    assert result.action == "cancelled"
    assert result.operation.status == "cancelled_by_customer"
    assert result.events[0].event_type == "status_changed"
    assert len(repository.events) == 2


@pytest.mark.anyio
async def test_invalid_transition_after_confirmation_is_rejected(
    service: OperationService,
) -> None:
    created = await service.create_pending_operation(
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    snapshot = _snapshot()
    await service.submit_confirmed_operation(
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
        actor="customer",
        provider=InMemoryOrderProvider(orders=(snapshot,)),
    )

    with pytest.raises(InvalidOperationStatusTransition):
        await service.cancel_pending_operation(
            operation_id=created.operation.operation_id,
            request_id="cancel-1",
            actor="customer",
        )


@pytest.mark.anyio
async def test_declined_operation_cannot_later_be_submitted(
    service: OperationService,
) -> None:
    snapshot = _snapshot()
    created = await service.create_pending_operation(
        request=_request(),
        snapshot=snapshot,
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    await service.cancel_pending_operation(
        operation_id=created.operation.operation_id,
        request_id="cancel-1",
        actor="customer",
    )
    provider = InMemoryOrderProvider(orders=(snapshot,))

    with pytest.raises(InvalidOperationStatusTransition):
        await service.submit_confirmed_operation(
            operation_id=created.operation.operation_id,
            request_id="submit-1",
            actor="customer",
            provider=provider,
        )

    provider_order = await provider.get_order(snapshot.order_id)
    assert provider_order is not None
    assert provider_order.version == snapshot.version


@pytest.mark.anyio
async def test_manual_operation_can_link_exactly_one_support_case(
    service: OperationService,
) -> None:
    created = await service.create_pending_operation(
        request=_request(),
        snapshot=_snapshot(amount="150.00"),
        decision=_manual_decision(),
        request_excerpt="Please return this expensive item.",
    )
    await service.confirm_operation(
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
        actor="customer",
    )
    case_id = UUID("00000000-0000-0000-0000-000000000999")

    result = await service.attach_support_case(
        operation_id=created.operation.operation_id,
        support_case_id=case_id,
        request_id="case-1",
        actor="system",
    )

    assert result.action == "support_case_attached"
    assert result.operation.support_case_id == case_id
    assert result.events[0].event_type == "support_case_attached"


@pytest.mark.anyio
async def test_update_status_records_provider_reference(service: OperationService) -> None:
    created = await service.create_pending_operation(
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    snapshot = _snapshot()
    await service.submit_confirmed_operation(
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
        actor="customer",
        provider=InMemoryOrderProvider(orders=(snapshot,)),
    )

    result = await service.update_operation_status(
        operation_id=created.operation.operation_id,
        target_status="processing",
        request_id="provider-1",
        actor="order-provider",
        provider_reference="provider-return-42",
    )

    assert result.action == "status_changed"
    assert result.operation.status == "processing"
    assert result.operation.provider_reference == "provider-return-42"


@pytest.mark.anyio
async def test_confirmed_automatic_operation_submits_to_provider_once(
    service: OperationService,
    repository: _InMemoryOperationRepository,
) -> None:
    snapshot = _snapshot()
    created = await service.create_pending_operation(
        request=_request(),
        snapshot=snapshot,
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    provider = InMemoryOrderProvider(orders=(snapshot,))

    first = await service.submit_confirmed_operation(
        operation_id=created.operation.operation_id,
        request_id="submit-1",
        actor="customer",
        provider=provider,
    )
    duplicate = await service.submit_confirmed_operation(
        operation_id=created.operation.operation_id,
        request_id="submit-1",
        actor="customer",
        provider=provider,
    )

    assert first.action == "submitted"
    assert first.operation.status == "submitted"
    assert first.operation.provider_reference is not None
    assert duplicate.action == "status_unchanged"
    assert len(repository.events) == 3
