"""Provide an in-memory implementation of the order-provider contract."""

from collections import defaultdict

from agent.operations.models import (
    ExistingOperation,
    OrderOperationRequest,
    OrderSnapshot,
)
from agent.operations.provider import StaleOrderVersionError


class InMemoryOrderProvider:
    """Store order snapshots and confirmed operations only in test memory."""

    def __init__(
        self,
        *,
        orders: tuple[OrderSnapshot, ...] = (),
        replacement_availability: dict[tuple[str, str], bool | None] | None = None,
    ) -> None:
        self._orders = {order.order_id: order for order in orders}
        self._replacement_availability = replacement_availability or {}
        self._operations_by_key: dict[str, ExistingOperation] = {}
        self._operation_count: defaultdict[str, int] = defaultdict(int)

    async def get_order(self, order_id: str) -> OrderSnapshot | None:
        """Return a snapshot by ID without exposing mutable internal state."""
        order = self._orders.get(order_id)
        return order.model_copy(deep=True) if order is not None else None

    async def get_replacement_availability(
        self,
        *,
        order_id: str,
        replacement_variant_id: str,
    ) -> bool | None:
        """Return the configured availability for an order and replacement variant."""
        return self._replacement_availability.get((order_id, replacement_variant_id))

    async def submit_operation(
        self,
        *,
        request: OrderOperationRequest,
        expected_order_version: int,
        idempotency_key: str,
    ) -> ExistingOperation:
        """Submit once by idempotency key and reject stale order versions."""
        existing = self._operations_by_key.get(idempotency_key)
        if existing is not None:
            return existing

        order = self._orders.get(request.order_id)
        if order is None:
            raise LookupError(f"Order not found: {request.order_id}")
        if order.version != expected_order_version:
            raise StaleOrderVersionError(
                f"Expected order version {expected_order_version}, got {order.version}"
            )

        self._operation_count[request.order_id] += 1
        operation = ExistingOperation(
            operation_id=(
                f"operation-{request.order_id}-{self._operation_count[request.order_id]}"
            ),
            operation_type=request.operation_type,
            status="submitted",
            provider_reference=idempotency_key,
        )
        self._operations_by_key[idempotency_key] = operation
        self._orders[request.order_id] = order.model_copy(
            update={
                "version": order.version + 1,
                "existing_operations": (*order.existing_operations, operation),
            }
        )
        return operation
