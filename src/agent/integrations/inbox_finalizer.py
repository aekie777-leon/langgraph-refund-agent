"""Stable Inbox finalization contract, independent of PostgreSQL."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from agent.integrations.models import ProviderAggregateType
from agent.integrations.persistence_models import ClaimedInboxMessage
from agent.operations.models import OperationRecordStatus

InboxFinalizationAction = Literal[
    "applied", "duplicate", "stale", "retry_scheduled", "failed"
]


@dataclass(frozen=True)
class InboxFinalizationResult:
    """Safe summary returned after one atomically fenced Inbox finalization."""

    action: InboxFinalizationAction
    aggregate_type: ProviderAggregateType
    previous_status: OperationRecordStatus | None = None
    current_status: OperationRecordStatus | None = None
    safe_error_code: str | None = None


class InboxFinalizer(Protocol):
    """Finalize one claimed Inbox message without exposing database details."""

    async def finalize_order_operation(
        self, *, claimed: ClaimedInboxMessage, retry_available_at: datetime
    ) -> InboxFinalizationResult:
        """Apply or safely complete an order-operation webhook callback."""
        ...

    async def finalize_support_case(
        self, *, claimed: ClaimedInboxMessage, retry_available_at: datetime
    ) -> InboxFinalizationResult:
        """Append one delivery-investigation provider update atomically."""
        ...
