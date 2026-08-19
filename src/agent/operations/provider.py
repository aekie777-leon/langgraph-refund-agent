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


class OrderQueryProvider(Protocol):
    """Synchronous order and inventory reads (v0.7 core read boundary)."""

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


class OrderCommandAdapter(Protocol):
    """Submit confirmed order-operation commands to a provider.

    This is the v0.7 command boundary: it sends commands to the provider over
    the transport, it does not enqueue locally. Delivery is at-least-once and
    the provider must deduplicate on the stable ``idempotency_key``. The
    LangGraph layer must not depend on this adapter directly in the long term;
    it will be driven by the outbox worker instead.
    """

    async def submit_operation(
        self,
        *,
        request: OrderOperationRequest,
        expected_order_version: int,
        idempotency_key: str,
    ) -> ExistingOperation:
        """Send one confirmed operation to the provider.

        Raise StaleOrderVersionError when the provider's order version changed
        and OrderNotAccessibleError when the order does not exist or does not
        belong to the request owner.
        """
        ...


class OrderProvider(OrderQueryProvider, OrderCommandAdapter, Protocol):
    """Transitional compatibility boundary (v0.6 -> v0.7).

    Keeps the existing graph, services, and fakes working unchanged while the
    v0.7 outbox/worker command path is introduced. New code should depend on
    ``OrderQueryProvider`` or ``OrderCommandAdapter`` instead; remove this
    composition once the command path no longer flows through the graph.
    """
