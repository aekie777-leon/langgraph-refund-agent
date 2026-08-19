"""Pure, fail-closed eligibility policy for manual Provider redrive."""

from dataclasses import dataclass
from enum import StrEnum

from agent.integrations.models import (
    InboxProcessingStatus,
    OutboxDeliveryStatus,
    ProviderFailureKind,
)
from agent.integrations.persistence_models import DeliveryAttemptOutcome
from agent.integrations.retry import MAX_ATTEMPTS


class RedriveEligibilityCode(StrEnum):
    """Enumerate safe outcomes suitable for a future conflict response."""

    ELIGIBLE = "eligible"
    STATUS_NOT_REDRIVABLE = "status_not_redrivable"
    ACTIVE_LEASE = "active_lease"
    PROVIDER_REJECTION = "provider_rejection"
    CURRENT_CYCLE_TERMINAL_EVIDENCE_REQUIRED = (
        "current_cycle_terminal_evidence_required"
    )
    TECHNICAL_TERMINAL_FAILURE_REQUIRED = "technical_terminal_failure_required"
    LEASE_EXPIRY_NOT_ATTEMPT_EXHAUSTING = "lease_expiry_not_attempt_exhausting"


@dataclass(frozen=True, slots=True)
class RedriveEligibilityResult:
    """Return only a decision and a stable, non-sensitive reason code."""

    eligible: bool
    reason_code: RedriveEligibilityCode

    def __post_init__(self) -> None:
        """Keep the boolean decision and machine-readable code consistent."""
        if self.eligible != (self.reason_code is RedriveEligibilityCode.ELIGIBLE):
            raise ValueError("eligible must match the eligibility reason code")


@dataclass(frozen=True, slots=True)
class OutboxRedriveState:
    """Carry the minimum locked Outbox state needed for eligibility."""

    status: OutboxDeliveryStatus
    delivery_cycle: int
    attempts_in_cycle: int
    has_active_lease: bool
    last_failure_kind: ProviderFailureKind | None
    terminal_attempt_cycle: int | None
    terminal_attempt_number: int | None
    terminal_attempt_outcome: DeliveryAttemptOutcome | None
    terminal_attempt_failure_kind: ProviderFailureKind | None


@dataclass(frozen=True, slots=True)
class InboxRedriveState:
    """Carry the minimum locked Inbox state needed for eligibility."""

    status: InboxProcessingStatus
    has_active_lease: bool


_ELIGIBLE = RedriveEligibilityResult(True, RedriveEligibilityCode.ELIGIBLE)
_TECHNICAL_FAILURE_KINDS: frozenset[ProviderFailureKind] = frozenset(
    {
        "network_error",
        "timeout",
        "http_retryable",
        "http_client_error",
        "validation_error",
    }
)


def _ineligible(reason_code: RedriveEligibilityCode) -> RedriveEligibilityResult:
    """Build a consistently shaped fail-closed result."""
    return RedriveEligibilityResult(False, reason_code)


def decide_outbox_redrive_eligibility(
    state: OutboxRedriveState,
    *,
    max_attempts: int = MAX_ATTEMPTS,
) -> RedriveEligibilityResult:
    """Allow only dead Outbox messages with current technical terminal evidence."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if state.status != "dead":
        return _ineligible(RedriveEligibilityCode.STATUS_NOT_REDRIVABLE)
    if state.has_active_lease:
        return _ineligible(RedriveEligibilityCode.ACTIVE_LEASE)
    if (
        state.last_failure_kind == "provider_rejection"
        or state.terminal_attempt_outcome == "provider_rejected"
        or state.terminal_attempt_failure_kind == "provider_rejection"
    ):
        return _ineligible(RedriveEligibilityCode.PROVIDER_REJECTION)
    if (
        state.delivery_cycle < 1
        or state.attempts_in_cycle < 1
        or state.terminal_attempt_cycle != state.delivery_cycle
        or state.terminal_attempt_number != state.attempts_in_cycle
    ):
        return _ineligible(
            RedriveEligibilityCode.CURRENT_CYCLE_TERMINAL_EVIDENCE_REQUIRED
        )
    if state.terminal_attempt_outcome == "terminal_failure":
        if state.terminal_attempt_failure_kind not in _TECHNICAL_FAILURE_KINDS:
            return _ineligible(
                RedriveEligibilityCode.TECHNICAL_TERMINAL_FAILURE_REQUIRED
            )
        return _ELIGIBLE
    if state.terminal_attempt_outcome == "lease_expired":
        if state.terminal_attempt_failure_kind is not None:
            return _ineligible(
                RedriveEligibilityCode.TECHNICAL_TERMINAL_FAILURE_REQUIRED
            )
        if state.attempts_in_cycle != max_attempts:
            return _ineligible(
                RedriveEligibilityCode.LEASE_EXPIRY_NOT_ATTEMPT_EXHAUSTING
            )
        return _ELIGIBLE
    return _ineligible(RedriveEligibilityCode.TECHNICAL_TERMINAL_FAILURE_REQUIRED)


def decide_inbox_redrive_eligibility(
    state: InboxRedriveState,
) -> RedriveEligibilityResult:
    """Allow only failed Inbox messages that do not carry a processing lease."""
    if state.status != "failed":
        return _ineligible(RedriveEligibilityCode.STATUS_NOT_REDRIVABLE)
    if state.has_active_lease:
        return _ineligible(RedriveEligibilityCode.ACTIVE_LEASE)
    return _ELIGIBLE
