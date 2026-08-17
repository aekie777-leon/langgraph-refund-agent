"""Define persistence boundaries for refund requests."""

from typing import Protocol

from agent.auth.models import AccessScope
from agent.refunds.models import RefundRequest


class DuplicateRefundError(RuntimeError):
    """Report that a refund request already exists for an order."""


class RefundPersistenceError(RuntimeError):
    """Report an unexpected refund persistence failure."""


class RefundRepository(Protocol):
    """Define storage operations required by the refund service."""

    async def get_by_order_id(
        self,
        scope: AccessScope,
        order_id: str,
    ) -> RefundRequest | None:
        """Return the refund request owned by the caller for one order."""
        ...

    async def create(
        self,
        scope: AccessScope,
        *,
        refund: RefundRequest,
    ) -> bool:
        """Create one refund request; return False when it already exists."""
        ...
