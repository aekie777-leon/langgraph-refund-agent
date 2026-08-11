"""Offline integration tests for the compiled refund graph."""

import re

import pytest
from langchain_core.messages import HumanMessage

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


@pytest.fixture
def refund_graph(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        graph_module,
        "_get_order_detector",
        lambda: FakeOrderDetector(),
    )
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
