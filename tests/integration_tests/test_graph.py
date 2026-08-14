"""Offline integration tests for the compiled refund graph."""

import re

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent import graph as graph_module
from agent import models
from agent.schemas import OrderDetection, Route, SemanticRiskDetection

pytestmark = pytest.mark.anyio


class FakeOrderDetector:
    """Extract demonstration order numbers without making a network call."""

    async def ainvoke(self, messages):
        text = messages[-1].content
        match = re.search(r"ORD-\d{5}", text, flags=re.IGNORECASE)
        return OrderDetection(
            has_order_id=match is not None,
            order_id=match.group(0) if match else None,
        )


class FakeRouter:
    """Route test messages deterministically without making a network call."""

    async def ainvoke(self, messages):
        text = messages[-1].content.lower()
        if any(word in text for word in ("complaint", "terrible", "unhappy")):
            step = "complaint"
        elif any(word in text for word in ("status", "where", "track")):
            step = "order_inquiry"
        else:
            step = "refund_request"
        return Route(step=step)


class FakeRiskClassifier:
    """Classify test risk phrases without making a network call."""

    async def ainvoke(self, messages):
        text = messages[-1].content.lower()
        if "immediate semantic danger" in text:
            return SemanticRiskDetection(
                risk_level="high",
                categories=["violence"],
                reason="The test message represents a serious semantic risk.",
            )
        if "consumer protection" in text or "formal complaint" in text:
            return SemanticRiskDetection(
                risk_level="medium",
                categories=["regulatory"],
                reason="The user is considering a regulatory complaint.",
            )
        if "head against the wall" in text:
            return SemanticRiskDetection(
                risk_level="medium",
                categories=["self_harm"],
                reason="The user uses ambiguous self-harm language.",
            )
        return SemanticRiskDetection(
            risk_level="none",
            categories=[],
            reason="No semantic risk is present.",
        )


class FakeComplaintModel:
    """Return a fixed complaint response without making a network call."""

    async def ainvoke(self, _messages):
        return AIMessage(
            content="I understand your concern. Please contact customer service."
        )


@pytest.fixture
def refund_graph(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        models,
        "get_order_detector",
        lambda: FakeOrderDetector(),
    )
    monkeypatch.setattr(models, "get_router", lambda: FakeRouter())
    monkeypatch.setattr(
        models,
        "get_risk_classifier",
        lambda: FakeRiskClassifier(),
    )
    monkeypatch.setattr(models, "get_llm", lambda: FakeComplaintModel())
    return graph_module.create_graph()


async def test_graph_requests_an_order_number(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="I want a refund.")]}
    )

    assert result["order_id_valid"] is False
    assert result["messages"][-1].content == "Please enter your order number."


async def test_graph_handles_unknown_order(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="Refund ORD-99999 please.")]}
    )

    assert result["search_success"] is False
    assert "Order not found" in result["messages"][-1].content


async def test_graph_rejects_expired_order(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="Refund ORD-10003 please.")]}
    )

    assert result["eligible"] is False
    assert result["reason"] == "This order is past the refund deadline."


async def test_graph_routes_large_refund_to_manual_review(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="Refund ORD-10002 please.")]}
    )

    assert result["eligible"] is True
    assert result["requires_manual_review"] is True
    assert "customer service" in result["messages"][-1].content


async def test_graph_interrupts_before_automatic_refund(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="Refund ORD-10001 please.")]}
    )

    assert result["eligible"] is True
    assert result["requires_manual_review"] is False
    assert result["__interrupt__"]


async def test_graph_returns_order_information_for_an_inquiry(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="What is the status of ORD-10001?")]}
    )

    assert result["decision"] == "order_inquiry"
    assert result["last_order_id"] == "ORD-10001"
    assert "Status: delivered" in result["messages"][-1].content
    assert "Product name:" in result["messages"][-1].content
    assert "__interrupt__" not in result


async def test_graph_handles_a_complaint_without_order_operations(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="The delivery service was terrible.")]}
    )

    assert result["decision"] == "complaint"
    assert result["order_id"] is None
    assert result["order_info"] == {}
    assert result["eligible"] is False
    assert "understand your concern" in result["messages"][-1].content


async def test_graph_does_not_reuse_an_order_for_another_order(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [HumanMessage(content="I want to refund another order.")],
            "order_id": "ORD-10001",
            "last_order_id": "ORD-10001",
            "order_info": {"order_id": "ORD-10001"},
            "search_success": True,
            "eligible": True,
            "requires_manual_review": False,
            "reason": "The order is eligible for a refund.",
        }
    )

    assert result["order_id"] is None
    assert result["last_order_id"] is None
    assert result["order_info"] == {}
    assert result["eligible"] is False
    assert result["messages"][-1].content == "Please enter your order number."


async def test_graph_reuses_an_explicitly_referenced_order(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [HumanMessage(content="Please refund this order.")],
            "last_order_id": "ORD-10001",
        }
    )

    assert result["order_id"] == "ORD-10001"
    assert result["last_order_id"] == "ORD-10001"
    assert result["eligible"] is True
    assert result["__interrupt__"]


async def test_graph_clears_previous_refund_state_for_a_complaint(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [HumanMessage(content="I have a complaint about delivery.")],
            "order_id": "ORD-10001",
            "last_order_id": "ORD-10001",
            "order_info": {"order_id": "ORD-10001"},
            "success": True,
            "search_success": True,
            "eligible": True,
            "requires_manual_review": True,
            "reason": "Old result",
        }
    )

    assert result["decision"] == "complaint"
    assert result["order_id"] is None
    assert result["last_order_id"] == "ORD-10001"
    assert result["order_info"] == {}
    assert result["success"] is False
    assert result["search_success"] is False
    assert result["eligible"] is False
    assert result["requires_manual_review"] is False
    assert result["reason"] == ""


async def test_graph_routes_hard_critical_rules_before_semantic_risk(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="I will kill you.")]}
    )

    assert result["risk_hard_critical"] is True
    assert result["risk_requires_human_review"] is True
    assert result["semantic_risk_level"] is None
    assert result["decision"] is None
    assert "urgent safety concern" in result["messages"][-1].content


async def test_graph_routes_semantic_high_risk_to_critical_handling(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="This is immediate semantic danger.")]}
    )

    assert result["risk_hard_critical"] is False
    assert result["semantic_risk_level"] == "high"
    assert result["semantic_risk_categories"] == ["violence"]
    assert result["risk_requires_human_review"] is True
    assert result["decision"] is None


async def test_graph_asks_before_processing_a_risky_refund_request(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "Please refund ORD-10001 or I will contact consumer "
                        "protection."
                    )
                )
            ]
        }
    )

    assert result["semantic_risk_level"] == "medium"
    assert result["semantic_risk_categories"] == ["regulatory"]
    assert result["decision"] == "refund_request"
    assert result["__interrupt__"]
    assert (
        result["__interrupt__"][0].value["type"]
        == "order_priority_confirmation"
    )


async def test_medium_self_harm_language_does_not_hide_an_order_inquiry(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "I want to bang my head against the wall. "
                        "Where is ORD-10001?"
                    )
                )
            ]
        }
    )

    assert result["risk_hard_critical"] is False
    assert result["semantic_risk_level"] == "medium"
    assert result["semantic_risk_categories"] == ["self_harm"]
    assert result["decision"] == "order_inquiry"
    assert result["__interrupt__"]


async def test_medium_risk_complaint_uses_noncritical_risk_response(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    content="I am considering a formal complaint about delivery."
                )
            ]
        }
    )

    assert result["semantic_risk_level"] == "medium"
    assert result["decision"] == "complaint"
    assert "understand your concern" in result["messages"][-1].content
    assert "__interrupt__" not in result
