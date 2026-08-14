"""Unit tests for the workflow's pure conditional-edge routing."""

import pytest

from agent.routing import (
    route_after_detection,
    route_after_order_lookup,
    route_after_policy,
    route_after_risk_rules,
    route_after_semantic_risk,
    route_by_intent_and_risk,
)


@pytest.mark.parametrize(
    ("decision", "risk_level", "expected"),
    [
        ("refund_request", "none", "order_query"),
        ("order_inquiry", "none", "order_query"),
        ("complaint", "none", "complaint"),
        ("refund_request", "low", "confirm_order_priority"),
        ("order_inquiry", "medium", "confirm_order_priority"),
        ("complaint", "low", "noncritical_risk"),
        ("complaint", "medium", "noncritical_risk"),
        (None, "none", "END"),
    ],
)
def test_route_by_intent_and_risk(
    decision,
    risk_level,
    expected: str,
) -> None:
    assert (
        route_by_intent_and_risk(
            {
                "decision": decision,
                "semantic_risk_level": risk_level,
            }
        )
        == expected
    )


def test_route_by_intent_rejects_an_unknown_value() -> None:
    with pytest.raises(ValueError, match="Unexpected intent/risk combination"):
        route_by_intent_and_risk(
            {
                "decision": "unknown",  # type: ignore[typeddict-item]
                "semantic_risk_level": "none",
            }
        )


@pytest.mark.parametrize(
    ("hard_critical", "expected"),
    [(True, "critical_risk"), (False, "semantic_risk")],
)
def test_route_after_risk_rules(
    hard_critical: bool,
    expected: str,
) -> None:
    assert route_after_risk_rules({"risk_hard_critical": hard_critical}) == expected


@pytest.mark.parametrize(
    ("risk_level", "expected"),
    [
        ("none", "intent"),
        ("low", "intent"),
        ("medium", "intent"),
        ("high", "critical_risk"),
        ("critical", "critical_risk"),
    ],
)
def test_route_after_semantic_risk(risk_level, expected: str) -> None:
    assert (
        route_after_semantic_risk({"semantic_risk_level": risk_level}) == expected
    )


def test_route_after_semantic_risk_rejects_a_missing_level() -> None:
    with pytest.raises(ValueError, match="Unexpected semantic risk level"):
        route_after_semantic_risk({})


def test_medium_self_harm_order_request_still_offers_order_help() -> None:
    assert (
        route_by_intent_and_risk(
            {
                "decision": "order_inquiry",
                "semantic_risk_level": "medium",
                "semantic_risk_categories": ["self_harm"],
            }
        )
        == "confirm_order_priority"
    )


@pytest.mark.parametrize(
    ("order_id", "expected"),
    [("ORD-10001", "search_node"), (None, "END")],
)
def test_route_after_detection(order_id: str | None, expected: str) -> None:
    assert route_after_detection({"order_id": order_id}) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"search_success": False}, "END"),
        (
            {"search_success": True, "decision": "order_inquiry"},
            "order_response",
        ),
        (
            {"search_success": True, "decision": "refund_request"},
            "check_refund_eligibility",
        ),
    ],
)
def test_route_after_order_lookup(state, expected: str) -> None:
    assert route_after_order_lookup(state) == expected


def test_route_after_order_lookup_rejects_an_unexpected_intent() -> None:
    with pytest.raises(ValueError, match="Unexpected decision after order lookup"):
        route_after_order_lookup(
            {"search_success": True, "decision": "complaint"}
        )


@pytest.mark.parametrize(
    ("eligible", "manual_review", "expected"),
    [
        (False, False, "END"),
        (True, True, "END"),
        (True, False, "approval_node"),
    ],
)
def test_route_after_policy(
    eligible: bool,
    manual_review: bool,
    expected: str,
) -> None:
    assert (
        route_after_policy(
            {
                "eligible": eligible,
                "requires_manual_review": manual_review,
            }
        )
        == expected
    )
