"""Unit tests for the ownership-scoped refund service."""

from datetime import UTC, datetime
from uuid import UUID, uuid5

import pytest

from agent.refunds.service import RefundService
from tests.fakes.identity import make_scope

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)


class InMemoryRefundRepository:
    """Implement the refund repository contract without PostgreSQL."""

    def __init__(self) -> None:
        self.refunds = {}

    async def get_by_order_id(self, scope, order_id):
        refund = self.refunds.get(order_id)
        if refund is None:
            return None
        if refund.tenant_id != scope.tenant_id or refund.customer_id != scope.customer_id:
            return None
        return refund

    async def create(self, scope, *, refund):
        if refund.customer_id != scope.customer_id or refund.tenant_id != scope.tenant_id:
            raise ValueError("ownership mismatch")
        if self.refunds.get(refund.order_id) is not None:
            return False
        self.refunds[refund.order_id] = refund
        return True


def _service(repository: InMemoryRefundRepository | None = None) -> RefundService:
    counter = 0

    def id_factory() -> UUID:
        nonlocal counter
        counter += 1
        return uuid5(UUID("00000000-0000-0000-0000-000000000001"), str(counter))

    return RefundService(
        repository or InMemoryRefundRepository(),
        clock=lambda: NOW,
        id_factory=id_factory,
    )


async def test_create_refund_stamps_ownership_and_idempotency() -> None:
    repository = InMemoryRefundRepository()
    service = _service(repository)
    scope = make_scope("customer", user_id="customer-a", tenant_id="tenant-demo")

    first = await service.create_refund(scope, order_id="ORD-10001")
    second = await service.create_refund(scope, order_id="ORD-10001")

    assert first is True
    assert second is False
    refund = await service.get_by_order_id(scope, order_id="ORD-10001")
    assert refund is not None
    assert refund.customer_id == "customer-a"
    assert refund.tenant_id == "tenant-demo"
    assert refund.created_by == "tenant-demo:customer-a"
    assert refund.status == "pending"


async def test_refund_lookup_is_isolated_by_scope() -> None:
    repository = InMemoryRefundRepository()
    service = _service(repository)
    owner = make_scope("customer", user_id="customer-a", tenant_id="tenant-demo")
    other = make_scope("customer", user_id="customer-b", tenant_id="tenant-demo")

    await service.create_refund(owner, order_id="ORD-10001")

    assert await service.get_by_order_id(owner, order_id="ORD-10001") is not None
    assert await service.get_by_order_id(other, order_id="ORD-10001") is None


async def test_non_customer_scope_cannot_create_refund() -> None:
    service = _service()
    scope = make_scope("support_agent", user_id="agent-7", tenant_id="tenant-demo")

    with pytest.raises(ValueError, match="only customers"):
        await service.create_refund(scope, order_id="ORD-10001")
