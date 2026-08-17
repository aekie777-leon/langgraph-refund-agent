"""Provide application-scoped dependencies for order-operation graph nodes."""

from uuid import UUID

from agent.operations.models import (
    ExistingOperation,
    OrderOperationRequest,
    OrderSnapshot,
)
from agent.operations.provider import OrderProvider
from agent.operations.service import OperationService

_order_provider: OrderProvider | None = None
_operation_service: OperationService | None = None


def configure_operation_dependencies(
    *,
    order_provider: OrderProvider,
    operation_service: OperationService,
) -> None:
    """Register dependencies once during application startup."""
    global _order_provider, _operation_service
    if _order_provider is not None or _operation_service is not None:
        raise RuntimeError("Order-operation dependencies have already been configured")
    _order_provider = order_provider
    _operation_service = operation_service


def get_order_provider() -> OrderProvider:
    """Return the configured order provider."""
    if _order_provider is None:
        raise RuntimeError("Order provider is unavailable because startup has not completed")
    return _order_provider


def get_operation_service() -> OperationService:
    """Return the configured operation service."""
    if _operation_service is None:
        raise RuntimeError("Operation service is unavailable because startup has not completed")
    return _operation_service


def clear_operation_dependencies() -> None:
    """Clear application-scoped operation dependencies during shutdown."""
    global _order_provider, _operation_service
    _order_provider = None
    _operation_service = None


class RuntimeOrderProvider:
    """Resolve the configured provider lazily when a graph node executes."""

    async def get_order(self, order_id: str) -> OrderSnapshot | None:
        """Delegate the snapshot read after application startup."""
        return await get_order_provider().get_order(order_id)

    async def get_replacement_availability(
        self,
        *,
        order_id: str,
        replacement_variant_id: str,
    ) -> bool | None:
        """Delegate replacement availability after application startup."""
        return await get_order_provider().get_replacement_availability(
            order_id=order_id,
            replacement_variant_id=replacement_variant_id,
        )

    async def submit_operation(
        self,
        *,
        request: OrderOperationRequest,
        expected_order_version: int,
        idempotency_key: str,
    ) -> ExistingOperation:
        """Delegate submission after application startup."""
        return await get_order_provider().submit_operation(
            request=request,
            expected_order_version=expected_order_version,
            idempotency_key=idempotency_key,
        )


class RuntimeOperationService:
    """Resolve the configured operation service lazily when a node executes."""

    async def create_pending_operation(self, **kwargs):
        """Delegate pending-operation creation after application startup."""
        return await get_operation_service().create_pending_operation(**kwargs)

    async def submit_confirmed_operation(self, **kwargs):
        """Delegate automatic submission after application startup."""
        return await get_operation_service().submit_confirmed_operation(**kwargs)

    async def confirm_operation(self, **kwargs):
        """Delegate manual confirmation after application startup."""
        return await get_operation_service().confirm_operation(**kwargs)

    async def cancel_pending_operation(self, **kwargs):
        """Delegate pending cancellation after application startup."""
        return await get_operation_service().cancel_pending_operation(**kwargs)

    async def update_operation_status(self, **kwargs):
        """Delegate status updates after application startup."""
        return await get_operation_service().update_operation_status(**kwargs)

    async def attach_support_case(
        self,
        *,
        operation_id: UUID,
        support_case_id: UUID,
        request_id: str,
        actor: str,
    ):
        """Delegate case attachment after application startup."""
        return await get_operation_service().attach_support_case(
            operation_id=operation_id,
            support_case_id=support_case_id,
            request_id=request_id,
            actor=actor,
        )
