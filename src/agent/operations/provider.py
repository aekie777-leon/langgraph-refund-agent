"""Define the external order-system boundary without choosing an implementation."""

from typing import Protocol

from agent.operations.models import (
    ExistingOperation,
    OrderOperationRequest,
    OrderSnapshot,
)


class StaleOrderVersionError(RuntimeError):
    """Report that an operation was submitted against an old order version."""


class OrderNotAccessibleError(RuntimeError):
    """Report that an order does not exist or does not belong to the caller."""


class OrderProvider(Protocol):
    """Read current order facts and submit confirmed customer operations."""

    async def get_order_for_customer(
        self,
        *,
        order_id: str,
        customer_id: str,
        tenant_id: str,
    ) -> OrderSnapshot | None:
        """Return the caller's snapshot, or None when the order is inaccessible."""
        ...

    async def get_replacement_availability(
        self,
        *,
        order_id: str,
        customer_id: str,
        tenant_id: str,
        replacement_variant_id: str,
    ) -> bool | None:
        """Return availability; None means unknown for an accessible order.

        Raise OrderNotAccessibleError when the order does not exist or does not
        belong to the caller.
        """
        ...

    async def submit_operation(
        self,
        *,
        request: OrderOperationRequest,
        expected_order_version: int,
        idempotency_key: str,
    ) -> ExistingOperation:
        """Submit one confirmed operation or raise StaleOrderVersionError.

        Raise OrderNotAccessibleError when the order does not exist or does not
        belong to the request owner.
        """
        ...
