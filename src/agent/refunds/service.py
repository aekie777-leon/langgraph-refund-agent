"""Coordinate idempotent, ownership-scoped refund-request creation."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from agent.auth.models import AccessScope
from agent.refunds.models import RefundRequest
from agent.refunds.repository import RefundRepository

Clock = Callable[[], datetime]
IdFactory = Callable[[], UUID]


def _utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


class RefundService:
    """Apply ownership stamping and idempotency to refund requests."""

    def __init__(
        self,
        repository: RefundRepository,
        *,
        clock: Clock = _utc_now,
        id_factory: IdFactory = uuid4,
    ) -> None:
        """Initialize the service with persistence and deterministic test hooks."""
        self._repository = repository
        self._clock = clock
        self._id_factory = id_factory

    async def create_refund(
        self,
        scope: AccessScope,
        *,
        order_id: str,
    ) -> bool:
        """Create one pending refund request; return False when it exists."""
        if scope.customer_id is None:
            raise ValueError("only customers may create refund requests")
        refund = RefundRequest(
            refund_id=self._id_factory(),
            order_id=order_id,
            status="pending",
            customer_id=scope.customer_id,
            tenant_id=scope.tenant_id,
            created_by=scope.identity,
            created_at=self._clock(),
        )
        return await self._repository.create(scope, refund=refund)

    async def get_by_order_id(
        self,
        scope: AccessScope,
        *,
        order_id: str,
    ) -> RefundRequest | None:
        """Return the caller-scoped refund request for one order."""
        return await self._repository.get_by_order_id(scope, order_id)
