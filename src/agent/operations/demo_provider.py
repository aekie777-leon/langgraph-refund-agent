"""Provide process-local order data for the v0.5 demonstration workflow."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agent.operations.models import (
    ExistingOperation,
    OrderOperationRequest,
    OrderSnapshot,
)
from agent.operations.provider import StaleOrderVersionError


class DemoOrderProvider:
    """Store demonstration order snapshots and idempotent submissions in memory."""

    def __init__(self, *, now: datetime | None = None) -> None:
        """Create a stable local data set relative to the supplied current time."""
        current = now or datetime.now(UTC)
        self._orders = {
            "ORD-10001": self._delivered("ORD-10001", "69.99", current, 2),
            "ORD-10002": self._delivered("ORD-10002", "149.99", current, 1),
            "ORD-10003": self._delivered("ORD-10003", "59.99", current, 10),
            "ORD-10004": self._shipped("ORD-10004", "49.99", current, 24),
            "ORD-10008": OrderSnapshot(
                order_id="ORD-10008", version=1, amount=Decimal("49.99"), currency="USD",
                order_status="confirmed", payment_status="paid", fulfillment_status="unfulfilled",
                created_at=current - timedelta(days=1), return_eligible=True, exchange_eligible=True,
            ),
            "ORD-10009": OrderSnapshot(
                order_id="ORD-10009", version=1, amount=Decimal("79.99"), currency="USD",
                order_status="confirmed", payment_status="paid", fulfillment_status="processing",
                created_at=current - timedelta(days=2), return_eligible=True, exchange_eligible=True,
            ),
            "ORD-10010": self._shipped("ORD-10010", "89.99", current, 80),
            "ORD-10011": self._delivered("ORD-10011", "59.99", current, 1),
        }
        self._availability = {
            ("ORD-10001", "variant-blue"): True,
            ("ORD-10001", "variant-unavailable"): False,
        }
        self._submissions: dict[str, ExistingOperation] = {}
        self._submission_counts: defaultdict[str, int] = defaultdict(int)

    async def get_order(self, order_id: str) -> OrderSnapshot | None:
        """Return a copy of the current demonstration order snapshot."""
        order = self._orders.get(order_id)
        return order.model_copy(deep=True) if order is not None else None

    async def get_replacement_availability(
        self,
        *,
        order_id: str,
        replacement_variant_id: str,
    ) -> bool | None:
        """Return configured variant availability, or unknown for other variants."""
        return self._availability.get((order_id, replacement_variant_id))

    async def submit_operation(
        self,
        *,
        request: OrderOperationRequest,
        expected_order_version: int,
        idempotency_key: str,
    ) -> ExistingOperation:
        """Submit exactly once and advance the local order version."""
        existing = self._submissions.get(idempotency_key)
        if existing is not None:
            return existing
        order = self._orders.get(request.order_id)
        if order is None:
            raise LookupError(f"Order not found: {request.order_id}")
        if order.version != expected_order_version:
            raise StaleOrderVersionError("The order changed before submission")

        self._submission_counts[request.order_id] += 1
        operation = ExistingOperation(
            operation_id=f"demo-{request.order_id}-{self._submission_counts[request.order_id]}",
            operation_type=request.operation_type,
            status="submitted",
            provider_reference=idempotency_key,
        )
        self._submissions[idempotency_key] = operation
        self._orders[request.order_id] = order.model_copy(
            update={
                "version": order.version + 1,
                "existing_operations": (*order.existing_operations, operation),
            }
        )
        return operation

    @staticmethod
    def _delivered(
        order_id: str,
        amount: str,
        now: datetime,
        delivered_days_ago: int,
    ) -> OrderSnapshot:
        delivered_at = now - timedelta(days=delivered_days_ago)
        return OrderSnapshot(
            order_id=order_id, version=1, amount=Decimal(amount), currency="USD",
            order_status="confirmed", payment_status="paid", fulfillment_status="delivered",
            created_at=delivered_at - timedelta(days=3),
            shipped_at=delivered_at - timedelta(days=1), delivered_at=delivered_at,
            promised_delivery_at=delivered_at, last_tracking_event_at=delivered_at,
            return_eligible=True, exchange_eligible=True,
        )

    @staticmethod
    def _shipped(
        order_id: str,
        amount: str,
        now: datetime,
        tracking_hours_ago: int,
    ) -> OrderSnapshot:
        shipped_at = now - timedelta(days=4)
        return OrderSnapshot(
            order_id=order_id, version=1, amount=Decimal(amount), currency="USD",
            order_status="confirmed", payment_status="paid", fulfillment_status="shipped",
            created_at=shipped_at - timedelta(days=2), shipped_at=shipped_at,
            promised_delivery_at=now - timedelta(days=1),
            last_tracking_event_at=now - timedelta(hours=tracking_hours_ago),
            return_eligible=True, exchange_eligible=True,
        )
