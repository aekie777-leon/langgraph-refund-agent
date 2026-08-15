"""Unit tests for deterministic support-case handoff policy."""

import pytest
from pydantic import ValidationError

from agent.cases.models import HandoffPolicyInput
from agent.cases.policy import (
    InvalidCaseStatusTransition,
    build_display_reason,
    determine_handoff_policy,
    select_higher_priority,
    should_append_to_existing_case,
    validate_case_status_transition,
)


def test_no_handoff_facts_do_not_create_a_case() -> None:
    result = determine_handoff_policy(HandoffPolicyInput())

    assert result.should_create_case is False
    assert result.case_type is None
    assert result.priority is None
    assert result.reason_codes == ()
    assert result.display_reason == ""


def test_low_semantic_risk_does_not_create_a_case() -> None:
    result = determine_handoff_policy(
        HandoffPolicyInput(
            semantic_risk_level="low",
            semantic_risk_categories=("self_harm",),
        )
    )

    assert result.should_create_case is False


@pytest.mark.parametrize("category", ["self_harm", "violence"])
def test_hard_critical_safety_rule_creates_a_p0_case(category) -> None:
    result = determine_handoff_policy(
        HandoffPolicyInput(hard_critical_categories=(category,))
    )

    assert result.should_create_case is True
    assert result.case_type == "safety_review"
    assert result.priority == "p0"
    assert result.reason_codes == (f"hard_critical_{category}",)


@pytest.mark.parametrize("category", ["legal", "regulatory", "reputation"])
def test_hard_critical_business_risk_creates_a_p1_case(category) -> None:
    result = determine_handoff_policy(
        HandoffPolicyInput(hard_critical_categories=(category,))
    )

    assert result.case_type == "business_escalation"
    assert result.priority == "p1"


@pytest.mark.parametrize("level", ["high", "critical"])
def test_high_or_critical_semantic_safety_risk_creates_a_p0_case(level) -> None:
    result = determine_handoff_policy(
        HandoffPolicyInput(
            semantic_risk_level=level,
            semantic_risk_categories=("self_harm",),
        )
    )

    assert result.case_type == "safety_review"
    assert result.priority == "p0"


@pytest.mark.parametrize("level", ["high", "critical"])
def test_high_or_critical_business_risk_creates_a_p1_case(level) -> None:
    result = determine_handoff_policy(
        HandoffPolicyInput(
            semantic_risk_level=level,
            semantic_risk_categories=("regulatory",),
        )
    )

    assert result.case_type == "business_escalation"
    assert result.priority == "p1"


def test_all_medium_risk_categories_create_one_p2_case() -> None:
    result = determine_handoff_policy(
        HandoffPolicyInput(
            semantic_risk_level="medium",
            semantic_risk_categories=(
                "self_harm",
                "legal",
                "regulatory",
                "reputation",
                "other",
            ),
        )
    )

    assert result.case_type == "safety_review"
    assert result.priority == "p2"
    assert result.reason_codes == (
        "semantic_medium_self_harm",
        "semantic_medium_legal",
        "semantic_medium_regulatory",
        "semantic_medium_reputation",
        "semantic_medium_other",
    )


def test_manual_refund_review_creates_a_p1_case() -> None:
    result = determine_handoff_policy(
        HandoffPolicyInput(refund_requires_manual_review=True)
    )

    assert result.case_type == "refund_review"
    assert result.priority == "p1"
    assert result.reason_codes == ("refund_manual_review",)


def test_confirmed_human_request_creates_a_p2_case() -> None:
    result = determine_handoff_policy(HandoffPolicyInput(human_handoff_confirmed=True))

    assert result.case_type == "general_support"
    assert result.priority == "p2"
    assert result.reason_codes == ("confirmed_human_request",)


def test_unconfirmed_human_request_does_not_create_a_case() -> None:
    result = determine_handoff_policy(HandoffPolicyInput())

    assert result.should_create_case is False


@pytest.mark.parametrize(
    ("severity", "expected_priority"),
    [
        ("critical", "p0"),
        ("high", "p1"),
        ("medium", "p1"),
        ("low", "p2"),
    ],
)
def test_staff_complaint_priority_depends_on_severity(
    severity,
    expected_priority: str,
) -> None:
    result = determine_handoff_policy(
        HandoffPolicyInput(staff_complaint_severity=severity)
    )

    assert result.case_type == "staff_conduct_complaint"
    assert result.priority == expected_priority
    assert result.reason_codes == (f"staff_conduct_{severity}",)


def test_explicit_other_complaint_creates_a_p3_case() -> None:
    result = determine_handoff_policy(HandoffPolicyInput(explicit_other_complaint=True))

    assert result.case_type == "other_complaint"
    assert result.priority == "p3"
    assert result.reason_codes == ("explicit_other_complaint",)


def test_safety_type_and_highest_priority_are_selected_independently() -> None:
    result = determine_handoff_policy(
        HandoffPolicyInput(
            semantic_risk_level="medium",
            semantic_risk_categories=("self_harm",),
            refund_requires_manual_review=True,
        )
    )

    assert result.case_type == "safety_review"
    assert result.priority == "p1"
    assert result.reason_codes == (
        "semantic_medium_self_harm",
        "refund_manual_review",
    )


def test_type_precedence_selects_staff_complaint_before_business_escalation() -> None:
    result = determine_handoff_policy(
        HandoffPolicyInput(
            semantic_risk_level="high",
            semantic_risk_categories=("legal",),
            staff_complaint_severity="high",
        )
    )

    assert result.case_type == "staff_conduct_complaint"
    assert result.priority == "p1"
    assert set(result.reason_codes) == {
        "semantic_high_legal",
        "staff_conduct_high",
    }


def test_duplicate_categories_do_not_duplicate_reason_codes() -> None:
    result = determine_handoff_policy(
        HandoffPolicyInput(
            semantic_risk_level="medium",
            semantic_risk_categories=("violence", "violence"),
        )
    )

    assert result.reason_codes == ("semantic_medium_violence",)


def test_display_reason_is_readable_but_derived_from_reason_codes() -> None:
    display_reason = build_display_reason(
        ("refund_manual_review", "confirmed_human_request")
    )

    assert display_reason == (
        "The refund requires manual review. "
        "The user confirmed a request for human support."
    )


def test_none_semantic_level_rejects_categories() -> None:
    with pytest.raises(
        ValidationError,
        match="semantic_risk_categories must be empty",
    ):
        HandoffPolicyInput(
            semantic_risk_level="none",
            semantic_risk_categories=("legal",),
        )


def test_non_none_semantic_level_requires_categories() -> None:
    with pytest.raises(
        ValidationError,
        match="semantic_risk_categories must not be empty",
    ):
        HandoffPolicyInput(semantic_risk_level="medium")


@pytest.mark.parametrize(
    ("current_status", "target_status"),
    [
        ("open", "in_progress"),
        ("in_progress", "on_hold"),
        ("in_progress", "resolved"),
        ("on_hold", "in_progress"),
        ("on_hold", "resolved"),
        ("resolved", "open"),
    ],
)
def test_allowed_case_status_transitions_do_not_raise(
    current_status,
    target_status,
) -> None:
    validate_case_status_transition(current_status, target_status)


def test_repeating_the_current_status_is_idempotent() -> None:
    validate_case_status_transition("in_progress", "in_progress")


@pytest.mark.parametrize(
    ("current_status", "target_status"),
    [
        ("open", "resolved"),
        ("open", "on_hold"),
        ("resolved", "in_progress"),
    ],
)
def test_forbidden_case_status_transitions_raise(
    current_status,
    target_status,
) -> None:
    with pytest.raises(
        InvalidCaseStatusTransition,
        match="Invalid case status transition",
    ):
        validate_case_status_transition(current_status, target_status)


@pytest.mark.parametrize("status", ["open", "in_progress", "on_hold"])
def test_same_thread_same_type_unresolved_case_accepts_an_event(status) -> None:
    assert (
        should_append_to_existing_case(
            existing_thread_id="thread-1",
            existing_case_type="safety_review",
            existing_status=status,
            incoming_thread_id="thread-1",
            incoming_case_type="safety_review",
        )
        is True
    )


@pytest.mark.parametrize(
    ("incoming_thread_id", "incoming_case_type", "existing_status"),
    [
        ("thread-2", "safety_review", "open"),
        ("thread-1", "refund_review", "open"),
        ("thread-1", "safety_review", "resolved"),
    ],
)
def test_different_or_resolved_case_does_not_accept_an_event(
    incoming_thread_id,
    incoming_case_type,
    existing_status,
) -> None:
    assert (
        should_append_to_existing_case(
            existing_thread_id="thread-1",
            existing_case_type="safety_review",
            existing_status=existing_status,
            incoming_thread_id=incoming_thread_id,
            incoming_case_type=incoming_case_type,
        )
        is False
    )


@pytest.mark.parametrize(
    ("current_priority", "incoming_priority", "expected"),
    [
        ("p1", "p2", "p1"),
        ("p2", "p0", "p0"),
        ("p3", "p3", "p3"),
    ],
)
def test_merged_case_priority_never_downgrades(
    current_priority,
    incoming_priority,
    expected: str,
) -> None:
    assert select_higher_priority(current_priority, incoming_priority) == expected
