"""Define the external order-system boundary without choosing an implementation."""

from typing import Protocol

from agent.operations.models import (
    ExistingOperation,
    OrderOperationRequest,
    OrderSnapshot,
)


class StaleOrderVersionError(RuntimeError):
    """Report that an operation was submitted against an old order version."""


class OrderProvider(Protocol):
    """Read current order facts and submit confirmed customer operations."""

    async def get_order(self, order_id: str) -> OrderSnapshot | None:
        """Return the current snapshot, or None when the order does not exist."""
        ...

    async def get_replacement_availability(
        self,
        *,
        order_id: str,
        replacement_variant_id: str,
    ) -> bool | None:
        """Return availability; None means the provider cannot determine it."""
        ...

    async def submit_operation(
        self,
        *,
        request: OrderOperationRequest,
        expected_order_version: int,
        idempotency_key: str,
    ) -> ExistingOperation:
        """Submit one confirmed operation or raise StaleOrderVersionError."""
        ...
