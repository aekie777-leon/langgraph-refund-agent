"""Deterministic, database-independent Inbox transition decisions."""

from dataclasses import dataclass
from typing import Literal

from agent.integrations.models import (
    OutboxDeliveryStatus,
    ProviderCommandExecutionStatus,
)
from agent.operations.models import OperationRecordStatus

InboxAction = Literal["apply", "duplicate", "stale", "conflict"]
OutboxReadiness = Literal["ready", "retry", "dead"]


@dataclass(frozen=True)
class InboxDecision:
    """Describe one safe aggregate transition decision."""

    action: InboxAction
    target_status: OperationRecordStatus | None = None


_TARGETS: dict[ProviderCommandExecutionStatus, OperationRecordStatus] = {
    "accepted": "submitted", "processing": "processing", "completed": "completed", "rejected": "rejected",
}


def provider_reference_is_compatible(*, current: str | None, incoming: str | None) -> bool:
    """Allow an unset reference to be filled, but never silently replaced."""
    return current is None or incoming is None or current == incoming


def decide_inbox_outbox_readiness(*, outbox_status: OutboxDeliveryStatus) -> OutboxReadiness:
    """Separate outbound completion ordering from local status policy."""
    if outbox_status in {"pending", "processing", "retry_scheduled"}:
        return "retry"
    if outbox_status == "published":
        return "ready"
    return "dead"


def decide_order_operation_callback(*, local_status: OperationRecordStatus, provider_status: ProviderCommandExecutionStatus, current_provider_reference: str | None, incoming_provider_reference: str | None) -> InboxDecision:
    """Apply the explicitly enumerated Provider-to-operation status matrix."""
    if not provider_reference_is_compatible(current=current_provider_reference, incoming=incoming_provider_reference):
        return InboxDecision("conflict")
    target = _TARGETS[provider_status]
    if local_status in {"pending_confirmation", "queued", "manual_review", "cancelled_by_customer"}:
        return InboxDecision("conflict")
    if local_status == target:
        return InboxDecision("duplicate", target)
    if local_status == "submitted":
        return InboxDecision("stale", target) if provider_status == "accepted" else InboxDecision("apply", target)
    if local_status == "processing":
        if provider_status == "accepted":
            return InboxDecision("stale", target)
        return InboxDecision("duplicate", target) if provider_status == "processing" else InboxDecision("apply", target)
    if local_status == "completed":
        return InboxDecision("stale", target) if provider_status in {"accepted", "processing"} else InboxDecision("conflict")
    if local_status == "rejected":
        return InboxDecision("conflict")
    raise ValueError("unsupported local operation status")
