"""Unit tests for demo provider order-ownership verification."""

from datetime import UTC, datetime

import pytest

from agent.operations.demo_provider import DemoOrderProvider
from agent.operations.models import OrderOperationRequest
from agent.operations.provider import OrderNotAccessibleError

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def _provider() -> DemoOrderProvider:
    return DemoOrderProvider(now=NOW)


async def test_owner_reads_own_order() -> None:
    provider = _provider()

    snapshot = await provider.get_order_for_customer(
        order_id="ORD-10001",
        customer_id="customer-a",
        tenant_id="tenant-demo",
    )

    assert snapshot is not None
    assert snapshot.order_id == "ORD-10001"


async def test_non_owner_read_returns_none() -> None:
    provider = _provider()

    assert (
        await provider.get_order_for_customer(
            order_id="ORD-10001",
            customer_id="customer-b",
            tenant_id="tenant-demo",
        )
        is None
    )


async def test_cross_tenant_read_returns_none() -> None:
    provider = _provider()

    assert (
        await provider.get_order_for_customer(
            order_id="ORD-10001",
            customer_id="customer-a",
            tenant_id="tenant-other",
        )
        is None
    )


async def test_customer_b_reads_own_safety_order() -> None:
    provider = _provider()

    assert (
        await provider.get_order_for_customer(
            order_id="ORD-20001",
            customer_id="customer-b",
            tenant_id="tenant-demo",
        )
        is not None
    )
    assert (
        await provider.get_order_for_customer(
            order_id="ORD-20001",
            customer_id="customer-a",
            tenant_id="tenant-demo",
        )
        is None
    )


async def test_customer_c_reads_own_other_tenant_order() -> None:
    provider = _provider()

    assert (
        await provider.get_order_for_customer(
            order_id="ORD-30001",
            customer_id="customer-c",
            tenant_id="tenant-other",
        )
        is not None
    )
    assert (
        await provider.get_order_for_customer(
            order_id="ORD-30001",
            customer_id="customer-a",
            tenant_id="tenant-demo",
        )
        is None
    )


async def test_availability_raises_for_inaccessible_order() -> None:
    provider = _provider()

    with pytest.raises(OrderNotAccessibleError):
        await provider.get_replacement_availability(
            order_id="ORD-10001",
            customer_id="customer-b",
            tenant_id="tenant-demo",
            replacement_variant_id="variant-blue",
        )


async def test_submit_raises_for_inaccessible_order() -> None:
    provider = _provider()
    request = OrderOperationRequest(
        thread_id="thread-1",
        source_message_id="message-1",
        order_id="ORD-10001",
        operation_type="return",
        reason="damaged_item",
        customer_id="customer-b",
        tenant_id="tenant-demo",
    )

    with pytest.raises(OrderNotAccessibleError):
        await provider.submit_operation(
            request=request,
            expected_order_version=1,
            idempotency_key="key-1",
        )
