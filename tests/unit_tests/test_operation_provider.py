"""Unit tests for the offline order-provider fake."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agent.operations.models import OrderOperationRequest, OrderSnapshot
from agent.operations.provider import StaleOrderVersionError
from tests.fakes.operations import InMemoryOrderProvider


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _snapshot() -> OrderSnapshot:
    return OrderSnapshot(
        order_id="ORD-10001",
        version=1,
        amount=Decimal("69.99"),
        currency="USD",
        order_status="confirmed",
        payment_status="paid",
        fulfillment_status="unfulfilled",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _request() -> OrderOperationRequest:
    return OrderOperationRequest(
        thread_id="thread-1",
        source_message_id="message-1",
        order_id="ORD-10001",
        operation_type="cancellation",
        reason="no_longer_needed",
    )


@pytest.mark.anyio
async def test_fake_provider_submission_is_idempotent() -> None:
    provider = InMemoryOrderProvider(orders=(_snapshot(),))
    request = _request()

    first = await provider.submit_operation(
        request=request,
        expected_order_version=1,
        idempotency_key="request-1",
    )
    duplicate = await provider.submit_operation(
        request=request,
        expected_order_version=1,
        idempotency_key="request-1",
    )

    assert duplicate == first
    order = await provider.get_order("ORD-10001")
    assert order is not None
    assert order.version == 2
    assert order.existing_operations == (first,)


@pytest.mark.anyio
async def test_fake_provider_rejects_stale_order_version() -> None:
    provider = InMemoryOrderProvider(orders=(_snapshot(),))

    with pytest.raises(StaleOrderVersionError, match="Expected order version"):
        await provider.submit_operation(
            request=_request(),
            expected_order_version=2,
            idempotency_key="request-1",
        )


@pytest.mark.anyio
async def test_fake_provider_returns_configured_replacement_availability() -> None:
    provider = InMemoryOrderProvider(
        orders=(_snapshot(),),
        replacement_availability={("ORD-10001", "variant-blue"): True},
    )

    assert await provider.get_replacement_availability(
        order_id="ORD-10001",
        replacement_variant_id="variant-blue",
    ) is True
    assert await provider.get_replacement_availability(
        order_id="ORD-10001",
        replacement_variant_id="variant-red",
    ) is None
