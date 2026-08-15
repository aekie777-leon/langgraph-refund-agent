"""Unit tests for human-support confirmation nodes."""

from typing import Any

import pytest

from agent.nodes import handoff as handoff_nodes


def test_confirmed_handoff_routes_to_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {}

    def fake_interrupt(value):
        payload.update(value)
        return "confirm_handoff"

    monkeypatch.setattr(handoff_nodes, "interrupt", fake_interrupt)

    command = handoff_nodes.confirm_human_handoff(
        {
            "decision": "complaint",
            "semantic_risk_level": "none",
        }
    )

    assert command.goto == "acknowledge_human_handoff"
    assert command.update == {"human_handoff_confirmed": True}
    assert payload["type"] == "human_handoff_confirmation"
    assert len(payload["options"]) == 2


@pytest.mark.parametrize(
    ("state", "expected_goto"),
    [
        (
            {"decision": "refund_request", "semantic_risk_level": "none"},
            "detect_order",
        ),
        (
            {"decision": "order_inquiry", "semantic_risk_level": "medium"},
            "confirm_order_priority",
        ),
        (
            {"decision": "complaint", "semantic_risk_level": "none"},
            "handle_complaint",
        ),
        (
            {"decision": "complaint", "semantic_risk_level": "medium"},
            "handle_noncritical_risk",
        ),
    ],
)
def test_declined_handoff_resumes_self_service(
    monkeypatch: pytest.MonkeyPatch,
    state,
    expected_goto: str,
) -> None:
    monkeypatch.setattr(
        handoff_nodes,
        "interrupt",
        lambda _value: "continue_self_service",
    )

    command = handoff_nodes.confirm_human_handoff(state)

    assert command.goto == expected_goto
    assert command.update == {
        "human_handoff_requested": False,
        "human_handoff_confirmed": False,
    }


def test_unknown_handoff_choice_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handoff_nodes,
        "interrupt",
        lambda _value: "unknown",
    )

    with pytest.raises(ValueError, match="Unexpected human-handoff"):
        handoff_nodes.confirm_human_handoff(
            {
                "decision": "complaint",
                "semantic_risk_level": "none",
            }
        )


def test_acknowledgement_does_not_claim_case_creation() -> None:
    result = handoff_nodes.acknowledge_human_handoff({})
    content = result["messages"][0].content

    assert "human support" in content
    assert "created" not in content.lower()
