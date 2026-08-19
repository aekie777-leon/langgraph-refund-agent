"""Matrix tests for deterministic Provider operations redrive eligibility."""

from dataclasses import asdict, replace

import pytest

from agent.integrations.models import (
    InboxProcessingStatus,
    OutboxDeliveryStatus,
    ProviderFailureKind,
)
from agent.integrations.provider_operations_policy import (
    InboxRedriveState,
    OutboxRedriveState,
    RedriveEligibilityCode,
    RedriveEligibilityResult,
    decide_inbox_redrive_eligibility,
    decide_outbox_redrive_eligibility,
)
from agent.integrations.retry import MAX_ATTEMPTS


def _outbox_state(**changes: object) -> OutboxRedriveState:
    state = OutboxRedriveState(
        status="dead",
        delivery_cycle=2,
        attempts_in_cycle=1,
        has_active_lease=False,
        last_failure_kind="network_error",
        terminal_attempt_cycle=2,
        terminal_attempt_number=1,
        terminal_attempt_outcome="terminal_failure",
        terminal_attempt_failure_kind="network_error",
    )
    return replace(state, **changes)


@pytest.mark.parametrize(
    "status",
    ["pending", "processing", "retry_scheduled", "published"],
)
def test_outbox_requires_dead_status(status: OutboxDeliveryStatus) -> None:
    result = decide_outbox_redrive_eligibility(_outbox_state(status=status))

    assert result.eligible is False
    assert result.reason_code is RedriveEligibilityCode.STATUS_NOT_REDRIVABLE


def test_outbox_rejects_an_active_lease() -> None:
    result = decide_outbox_redrive_eligibility(_outbox_state(has_active_lease=True))

    assert result.eligible is False
    assert result.reason_code is RedriveEligibilityCode.ACTIVE_LEASE


@pytest.mark.parametrize(
    "failure_kind",
    [
        "network_error",
        "timeout",
        "http_retryable",
        "http_client_error",
        "validation_error",
    ],
)
def test_outbox_allows_current_cycle_technical_terminal_failure(
    failure_kind: ProviderFailureKind,
) -> None:
    result = decide_outbox_redrive_eligibility(
        _outbox_state(
            last_failure_kind=failure_kind,
            terminal_attempt_failure_kind=failure_kind,
        )
    )

    assert result.eligible is True
    assert result.reason_code is RedriveEligibilityCode.ELIGIBLE


@pytest.mark.parametrize(
    "changes",
    [
        {
            "terminal_attempt_outcome": "provider_rejected",
            "terminal_attempt_failure_kind": "provider_rejection",
            "last_failure_kind": "provider_rejection",
        },
        {"terminal_attempt_failure_kind": "provider_rejection"},
        {"last_failure_kind": "provider_rejection"},
    ],
)
def test_outbox_provider_rejection_is_never_eligible(
    changes: dict[str, object],
) -> None:
    result = decide_outbox_redrive_eligibility(_outbox_state(**changes))

    assert result.eligible is False
    assert result.reason_code is RedriveEligibilityCode.PROVIDER_REJECTION


@pytest.mark.parametrize(
    ("changes", "reason_code"),
    [
        (
            {"terminal_attempt_cycle": None},
            RedriveEligibilityCode.CURRENT_CYCLE_TERMINAL_EVIDENCE_REQUIRED,
        ),
        (
            {"terminal_attempt_cycle": 1},
            RedriveEligibilityCode.CURRENT_CYCLE_TERMINAL_EVIDENCE_REQUIRED,
        ),
        (
            {"terminal_attempt_number": 2},
            RedriveEligibilityCode.CURRENT_CYCLE_TERMINAL_EVIDENCE_REQUIRED,
        ),
        (
            {"terminal_attempt_outcome": None},
            RedriveEligibilityCode.TECHNICAL_TERMINAL_FAILURE_REQUIRED,
        ),
        (
            {
                "terminal_attempt_outcome": "accepted",
                "terminal_attempt_failure_kind": None,
            },
            RedriveEligibilityCode.TECHNICAL_TERMINAL_FAILURE_REQUIRED,
        ),
        (
            {"terminal_attempt_failure_kind": None},
            RedriveEligibilityCode.TECHNICAL_TERMINAL_FAILURE_REQUIRED,
        ),
        (
            {"terminal_attempt_failure_kind": "unclassified_failure"},
            RedriveEligibilityCode.TECHNICAL_TERMINAL_FAILURE_REQUIRED,
        ),
        (
            {
                "terminal_attempt_outcome": "retry_scheduled",
                "terminal_attempt_failure_kind": "network_error",
            },
            RedriveEligibilityCode.TECHNICAL_TERMINAL_FAILURE_REQUIRED,
        ),
    ],
)
def test_outbox_requires_current_cycle_terminal_evidence(
    changes: dict[str, object],
    reason_code: RedriveEligibilityCode,
) -> None:
    result = decide_outbox_redrive_eligibility(_outbox_state(**changes))

    assert result.eligible is False
    assert result.reason_code is reason_code


def test_outbox_allows_only_attempt_exhausting_lease_expiry() -> None:
    exhausted = _outbox_state(
        attempts_in_cycle=MAX_ATTEMPTS,
        terminal_attempt_number=MAX_ATTEMPTS,
        terminal_attempt_outcome="lease_expired",
        terminal_attempt_failure_kind=None,
    )

    eligible = decide_outbox_redrive_eligibility(exhausted)
    below_limit = decide_outbox_redrive_eligibility(
        replace(
            exhausted,
            attempts_in_cycle=MAX_ATTEMPTS - 1,
            terminal_attempt_number=MAX_ATTEMPTS - 1,
        )
    )
    above_limit = decide_outbox_redrive_eligibility(
        replace(
            exhausted,
            attempts_in_cycle=MAX_ATTEMPTS + 1,
            terminal_attempt_number=MAX_ATTEMPTS + 1,
        )
    )

    assert eligible.eligible is True
    assert eligible.reason_code is RedriveEligibilityCode.ELIGIBLE
    for ineligible in (below_limit, above_limit):
        assert ineligible.eligible is False
        assert (
            ineligible.reason_code
            is RedriveEligibilityCode.LEASE_EXPIRY_NOT_ATTEMPT_EXHAUSTING
        )


def test_outbox_rejects_inconsistent_lease_expiry_failure_kind() -> None:
    result = decide_outbox_redrive_eligibility(
        _outbox_state(
            attempts_in_cycle=MAX_ATTEMPTS,
            terminal_attempt_number=MAX_ATTEMPTS,
            terminal_attempt_outcome="lease_expired",
            terminal_attempt_failure_kind="network_error",
        )
    )

    assert result.eligible is False
    assert (
        result.reason_code is RedriveEligibilityCode.TECHNICAL_TERMINAL_FAILURE_REQUIRED
    )


@pytest.mark.parametrize(
    ("status", "has_active_lease", "eligible", "reason_code"),
    [
        ("received", False, False, RedriveEligibilityCode.STATUS_NOT_REDRIVABLE),
        ("processing", True, False, RedriveEligibilityCode.STATUS_NOT_REDRIVABLE),
        ("processed", False, False, RedriveEligibilityCode.STATUS_NOT_REDRIVABLE),
        ("failed", True, False, RedriveEligibilityCode.ACTIVE_LEASE),
        ("failed", False, True, RedriveEligibilityCode.ELIGIBLE),
    ],
)
def test_inbox_is_eligible_only_when_failed_without_a_lease(
    status: InboxProcessingStatus,
    has_active_lease: bool,
    eligible: bool,
    reason_code: RedriveEligibilityCode,
) -> None:
    result = decide_inbox_redrive_eligibility(
        InboxRedriveState(status=status, has_active_lease=has_active_lease)
    )

    assert result.eligible is eligible
    assert result.reason_code is reason_code


def test_eligibility_result_contains_no_identifier_or_message() -> None:
    result = decide_outbox_redrive_eligibility(_outbox_state())

    assert asdict(result) == {
        "eligible": True,
        "reason_code": RedriveEligibilityCode.ELIGIBLE,
    }


def test_eligibility_result_rejects_an_inconsistent_boolean_and_code() -> None:
    with pytest.raises(ValueError, match="eligible must match"):
        RedriveEligibilityResult(True, RedriveEligibilityCode.ACTIVE_LEASE)


def test_outbox_rejects_invalid_attempt_limit_configuration() -> None:
    with pytest.raises(ValueError, match="max_attempts must be positive"):
        decide_outbox_redrive_eligibility(_outbox_state(), max_attempts=0)
