"""Unit tests for durable confirmation-gated order-operation behavior."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid5

import pytest

from agent.operations.models import (
    CaseRecommendation,
    OperationDecision,
    OrderOperationRequest,
    OrderSnapshot,
)
from agent.operations.repository import OperationPersistenceError
from agent.operations.service import (
    InvalidOperationStatusTransition,
    OperationService,
)
from tests.fakes.identity import make_scope
from tests.fakes.operations import InMemoryOrderProvider
from tests.operation_support import InMemoryOperationRepository

NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
SCOPE = make_scope("customer")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def repository() -> InMemoryOperationRepository:
    return InMemoryOperationRepository()


@pytest.fixture
def service(repository: InMemoryOperationRepository) -> OperationService:
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
        customer_id="customer-a",
        tenant_id="tenant-demo",
    )


def _request(*, message_id: str = "message-1") -> OrderOperationRequest:
    return OrderOperationRequest(
        thread_id="thread-1",
        source_message_id=message_id,
        order_id="ORD-10001",
        operation_type="return",
        reason="damaged_item",
        customer_id="customer-a",
        tenant_id="tenant-demo",
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
    repository: InMemoryOperationRepository,
) -> None:
    result = await service.create_pending_operation(
        SCOPE,
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
    assert result.operation.customer_id == "customer-a"
    assert result.operation.tenant_id == "tenant-demo"
    assert result.operation.created_by == "tenant-demo:customer-a"
    assert result.events[0].event_type == "operation_created"
    assert result.events[0].actor == "system"
    assert len(repository.operations) == 1


@pytest.mark.anyio
async def test_duplicate_source_message_does_not_create_a_second_operation(
    service: OperationService,
    repository: InMemoryOperationRepository,
) -> None:
    first = await service.create_pending_operation(
        SCOPE,
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    duplicate = await service.create_pending_operation(
        SCOPE,
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
        SCOPE,
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    result = await service.create_pending_operation(
        SCOPE,
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
    repository: InMemoryOperationRepository,
) -> None:
    created = await service.create_pending_operation(
        SCOPE,
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )

    with pytest.raises(ValueError, match="submit_confirmed_operation"):
        await service.confirm_operation(
            SCOPE,
            operation_id=created.operation.operation_id,
            request_id="confirm-1",
        )

    assert len(repository.events) == 1


@pytest.mark.anyio
async def test_manual_operation_confirmation_moves_to_manual_review(
    service: OperationService,
) -> None:
    created = await service.create_pending_operation(
        SCOPE,
        request=_request(),
        snapshot=_snapshot(amount="150.00"),
        decision=_manual_decision(),
        request_excerpt="Please return this expensive item.",
    )

    result = await service.confirm_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
    )

    assert result.operation.status == "manual_review"
    assert result.operation.review_case_type == "order_operation_review"
    assert result.operation.review_priority == "p1"


@pytest.mark.anyio
async def test_confirm_request_is_idempotent(
    service: OperationService,
    repository: InMemoryOperationRepository,
) -> None:
    created = await service.create_pending_operation(
        SCOPE,
        request=_request(),
        snapshot=_snapshot(amount="150.00"),
        decision=_manual_decision(),
        request_excerpt="Please return this expensive item.",
    )
    first = await service.confirm_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
    )
    duplicate = await service.confirm_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
    )

    assert first.action == "confirmed"
    assert duplicate.action == "status_unchanged"
    assert len(repository.events) == 3


@pytest.mark.anyio
async def test_provider_failure_rejects_blank_request_id_before_coordinator() -> None:
    service = OperationService(InMemoryOperationRepository())

    with pytest.raises(ValueError, match="request_id"):
        await service.move_to_provider_manual_review(
            SCOPE, operation_id=uuid5(UUID(int=0), "provider-failure"), request_id="  "
        )


@pytest.mark.anyio
async def test_provider_failure_requires_configured_coordinator() -> None:
    service = OperationService(InMemoryOperationRepository())

    with pytest.raises(RuntimeError, match="coordinator"):
        await service.move_to_provider_manual_review(
            SCOPE, operation_id=uuid5(UUID(int=0), "provider-failure"), request_id="request-1"
        )


class _RecordingProviderFailureCoordinator:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    async def move_to_manual_review(self, scope, *, operation_id, request_id):
        self.calls.append((scope, operation_id, request_id))
        return self.result


@pytest.mark.anyio
async def test_provider_failure_normalizes_request_id_and_delegates() -> None:
    expected = object()
    coordinator = _RecordingProviderFailureCoordinator(expected)
    service = OperationService(InMemoryOperationRepository(), provider_queue_failure_coordinator=coordinator)
    operation_id = uuid5(UUID(int=0), "provider-failure-delegate")

    result = await service.move_to_provider_manual_review(SCOPE, operation_id=operation_id, request_id=" request-1 ")

    assert result is expected
    assert coordinator.calls == [(SCOPE, operation_id, "request-1")]


@pytest.mark.anyio
async def test_provider_failure_propagates_persistence_error() -> None:
    class FailingCoordinator:
        async def move_to_manual_review(self, *_args, **_kwargs):
            raise OperationPersistenceError("safe persistence failure")

    service = OperationService(InMemoryOperationRepository(), provider_queue_failure_coordinator=FailingCoordinator())
    with pytest.raises(OperationPersistenceError, match="safe persistence failure"):
        await service.move_to_provider_manual_review(SCOPE, operation_id=uuid5(UUID(int=0), "provider-failure-error"), request_id="request-1")


@pytest.mark.anyio
async def test_customer_can_cancel_only_before_confirmation(
    service: OperationService,
    repository: InMemoryOperationRepository,
) -> None:
    created = await service.create_pending_operation(
        SCOPE,
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )

    result = await service.cancel_pending_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="cancel-1",
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
        SCOPE,
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    snapshot = _snapshot()
    await service.submit_confirmed_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
        provider=InMemoryOrderProvider(orders=(snapshot,)),
    )

    with pytest.raises(InvalidOperationStatusTransition):
        await service.cancel_pending_operation(
            SCOPE,
            operation_id=created.operation.operation_id,
            request_id="cancel-1",
        )


@pytest.mark.anyio
async def test_declined_operation_cannot_later_be_submitted(
    service: OperationService,
) -> None:
    snapshot = _snapshot()
    created = await service.create_pending_operation(
        SCOPE,
        request=_request(),
        snapshot=snapshot,
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    await service.cancel_pending_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="cancel-1",
    )
    provider = InMemoryOrderProvider(orders=(snapshot,))

    with pytest.raises(InvalidOperationStatusTransition):
        await service.submit_confirmed_operation(
            SCOPE,
            operation_id=created.operation.operation_id,
            request_id="submit-1",
            provider=provider,
        )

    provider_order = await provider.get_order_for_customer(
        order_id=snapshot.order_id,
        customer_id="customer-a",
        tenant_id="tenant-demo",
    )
    assert provider_order is not None
    assert provider_order.version == snapshot.version


@pytest.mark.anyio
async def test_manual_operation_can_link_exactly_one_support_case(
    service: OperationService,
) -> None:
    created = await service.create_pending_operation(
        SCOPE,
        request=_request(),
        snapshot=_snapshot(amount="150.00"),
        decision=_manual_decision(),
        request_excerpt="Please return this expensive item.",
    )
    await service.confirm_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
    )
    case_id = UUID("00000000-0000-0000-0000-000000000999")

    result = await service.attach_support_case(
        SCOPE,
        operation_id=created.operation.operation_id,
        support_case_id=case_id,
        request_id="case-1",
    )

    assert result.action == "support_case_attached"
    assert result.operation.support_case_id == case_id
    assert result.events[0].event_type == "support_case_attached"


@pytest.mark.anyio
async def test_update_status_records_provider_reference(service: OperationService) -> None:
    created = await service.create_pending_operation(
        SCOPE,
        request=_request(),
        snapshot=_snapshot(),
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    snapshot = _snapshot()
    await service.submit_confirmed_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="confirm-1",
        provider=InMemoryOrderProvider(orders=(snapshot,)),
    )

    result = await service.update_operation_status(
        SCOPE,
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
    repository: InMemoryOperationRepository,
) -> None:
    snapshot = _snapshot()
    created = await service.create_pending_operation(
        SCOPE,
        request=_request(),
        snapshot=snapshot,
        decision=_eligible_decision(),
        request_excerpt="Return this item.",
    )
    provider = InMemoryOrderProvider(orders=(snapshot,))

    first = await service.submit_confirmed_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="submit-1",
        provider=provider,
    )
    duplicate = await service.submit_confirmed_operation(
        SCOPE,
        operation_id=created.operation.operation_id,
        request_id="submit-1",
        provider=provider,
    )

    assert first.action == "submitted"
    assert first.operation.status == "submitted"
    assert first.operation.provider_reference is not None
    assert duplicate.action == "status_unchanged"
    assert len(repository.events) == 3
