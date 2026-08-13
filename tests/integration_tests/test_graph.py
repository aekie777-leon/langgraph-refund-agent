"""Offline integration tests for the compiled refund graph."""

import re

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent import graph as graph_module

pytestmark = pytest.mark.anyio


class FakeOrderDetector:
    """Extract demonstration order numbers without making a network call."""

    async def ainvoke(self, messages):
        text = messages[-1].content
        match = re.search(r"ORD-\d{5}", text, flags=re.IGNORECASE)
        return graph_module.OrderDetection(
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
        return graph_module.Route(step=step)


class FakeComplaintModel:
    """Return a fixed complaint response without making a network call."""

    async def ainvoke(self, _messages):
        return AIMessage(
            content="I understand your concern. Please contact customer service."
        )


@pytest.fixture
def refund_graph(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        graph_module,
        "_get_order_detector",
        lambda: FakeOrderDetector(),
    )
    monkeypatch.setattr(graph_module, "_router", lambda: FakeRouter())
    monkeypatch.setattr(graph_module, "_get_llm", lambda: FakeComplaintModel())
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
