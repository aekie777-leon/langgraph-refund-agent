"""Tests for graph construction and configuration validation."""

import pytest
from langgraph.pregel import Pregel

from agent import graph as graph_module
from agent import models
from agent.schemas import OrderDetection, Route


class FakeOrderDetector:
    """Return a fixed structured-output result without calling an API."""

    async def ainvoke(self, _messages):
        return OrderDetection(
            has_order_id=True,
            order_id="ORD-10001",
        )


class FakeRouter:
    """Return a fixed routing result without calling an API."""

    async def ainvoke(self, _messages):
        return Route(step="refund_request")


def test_create_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        models,
        "get_order_detector",
        lambda: FakeOrderDetector(),
    )
    monkeypatch.setattr(models, "get_router", lambda: FakeRouter())

    graph = graph_module.create_graph()

    assert isinstance(graph, Pregel)
    assert {
        "llm_call_router",
        "detect_order",
        "search_node",
        "check_refund_eligibility",
        "order_response",
        "approval_node",
        "proceed",
        "cancel",
        "handle_complaint",
    }.issubset(graph.get_graph().nodes)


def test_model_configuration_is_checked_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY, OPENAI_MODEL"):
        models.get_order_detector()
