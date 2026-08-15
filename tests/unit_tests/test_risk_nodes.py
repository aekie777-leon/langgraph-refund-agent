"""Unit tests for rule-based and semantic-risk workflow nodes."""

from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from agent import models
from agent.nodes import risk as risk_nodes
from agent.schemas import SemanticRiskDetection


class FakeSemanticRiskClassifier:
    """Return a fixed structured risk result and retain invocation messages."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def ainvoke(self, messages):
        self.messages = messages
        return SemanticRiskDetection(
            risk_level="medium",
            categories=["regulatory"],
            reason="The user is considering regulatory escalation.",
        )


class FakeResponseModel:
    """Return a fixed response for non-critical risk handling."""

    async def ainvoke(self, _messages):
        from langchain_core.messages import AIMessage

        return AIMessage(content="I understand your concern.")


def test_check_risk_rules_preserves_hard_critical_and_resets_turn_state() -> None:
    result = risk_nodes.check_risk_rules(
        {
            "messages": [HumanMessage(content="I will kill you.")],
            "decision": "refund_request",
            "order_id": "ORD-10001",
            "order_info": {"order_id": "ORD-10001"},
            "success": True,
            "search_success": True,
            "eligible": True,
            "requires_manual_review": True,
            "order_id_valid": True,
            "reason": "Old result",
            "semantic_risk_level": "medium",
            "semantic_risk_categories": ["legal"],
            "risk_order_choice": "handle_order",
            "human_handoff_requested": True,
            "human_handoff_confirmed": True,
            "staff_complaint_severity": "high",
            "explicit_other_complaint": True,
            "formal_complaint_reason": "Old complaint result",
        }
    )

    assert result["risk_hard_critical"] is True
    assert result["risk_has_signals"] is False
    assert result["risk_rule_matches"][0]["category"] == "violence"
    assert result["decision"] is None
    assert result["order_id"] is None
    assert result["order_info"] == {}
    assert result["semantic_risk_level"] is None
    assert result["semantic_risk_categories"] == []
    assert result["risk_order_choice"] is None
    assert result["human_handoff_requested"] is False
    assert result["human_handoff_confirmed"] is False
    assert result["staff_complaint_severity"] is None
    assert result["explicit_other_complaint"] is False
    assert result["formal_complaint_reason"] == ""


@pytest.mark.anyio
async def test_semantic_classifier_receives_rule_signal_context() -> None:
    classifier = FakeSemanticRiskClassifier()
    node = risk_nodes.build_semantic_risk_classifier_node(classifier)

    result = await node(
        {
            "messages": [HumanMessage(content="I will contact consumer protection.")],
            "risk_rule_matches": [
                {
                    "rule_id": "signal-regulatory-en-001",
                    "category": "regulatory",
                }
            ],
        }
    )

    assert result == {
        "semantic_risk_level": "medium",
        "semantic_risk_categories": ["regulatory"],
        "semantic_risk_reason": ("The user is considering regulatory escalation."),
    }
    assert "signal-regulatory-en-001" in classifier.messages[1].content


@pytest.mark.anyio
async def test_semantic_classifier_skips_model_without_a_text_message() -> None:
    classifier = FakeSemanticRiskClassifier()
    node = risk_nodes.build_semantic_risk_classifier_node(classifier)

    result = await node({"messages": []})

    assert result["semantic_risk_level"] == "none"
    assert result["semantic_risk_categories"] == []
    assert classifier.messages == []


@pytest.mark.parametrize(
    ("decision", "expected_goto"),
    [
        ("handle_order", "detect_order"),
        ("continue_risk", "handle_noncritical_risk"),
    ],
)
def test_confirm_order_priority_routes_the_resume_choice(
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    expected_goto: str,
) -> None:
    payload: dict[str, Any] = {}

    def fake_interrupt(value):
        payload.update(value)
        return decision

    monkeypatch.setattr(risk_nodes, "interrupt", fake_interrupt)

    command = risk_nodes.confirm_order_priority({})

    assert command.goto == expected_goto
    assert command.update == {"risk_order_choice": decision}
    assert payload["type"] == "order_priority_confirmation"
    assert len(payload["options"]) == 2


def test_confirm_order_priority_rejects_an_unknown_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(risk_nodes, "interrupt", lambda _value: "unknown")

    with pytest.raises(ValueError, match="Unexpected order-priority decision"):
        risk_nodes.confirm_order_priority({})


def test_critical_safety_risk_uses_the_safety_response() -> None:
    result = risk_nodes.handle_critical_risk(
        {
            "semantic_risk_categories": ["self_harm"],
            "risk_hard_critical": False,
        }
    )

    assert result["risk_requires_human_review"] is True
    assert "urgent safety concern" in result["messages"][0].content


@pytest.mark.anyio
async def test_noncritical_risk_uses_the_response_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models, "get_llm", lambda: FakeResponseModel())

    result = await risk_nodes.handle_noncritical_risk(
        {
            "messages": [HumanMessage(content="I may file a complaint.")],
            "semantic_risk_level": "medium",
            "semantic_risk_categories": ["regulatory"],
            "semantic_risk_reason": "Possible regulatory escalation.",
        }
    )

    assert result["messages"][0].content == "I understand your concern."
