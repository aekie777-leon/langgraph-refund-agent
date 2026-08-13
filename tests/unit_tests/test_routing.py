"""Unit tests for the workflow's pure conditional-edge routing."""

import pytest

from agent.routing import (
    route_after_detection,
    route_after_order_lookup,
    route_after_policy,
    route_by_intent,
)


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ("refund_request", "order_query"),
        ("order_inquiry", "order_query"),
        ("complaint", "complaint"),
        (None, "END"),
    ],
)
def test_route_by_intent(decision, expected: str) -> None:
    assert route_by_intent({"decision": decision}) == expected


def test_route_by_intent_rejects_an_unknown_value() -> None:
    with pytest.raises(ValueError, match="Unexpected intent decision"):
        route_by_intent({"decision": "unknown"})  # type: ignore[typeddict-item]


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
