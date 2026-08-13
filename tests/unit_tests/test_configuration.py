"""Tests for graph construction and configuration validation."""

import pytest
from langgraph.pregel import Pregel

from agent import graph as graph_module


class FakeOrderDetector:
    """Return a fixed structured-output result without calling an API."""

    async def ainvoke(self, _messages):
        return graph_module.OrderDetection(
            has_order_id=True,
            order_id="ORD-10001",
        )


class FakeRouter:
    """Return a fixed routing result without calling an API."""

    async def ainvoke(self, _messages):
        return graph_module.Route(step="refund_request")


def test_create_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        graph_module,
        "_get_order_detector",
        lambda: FakeOrderDetector(),
    )
    monkeypatch.setattr(graph_module, "_router", lambda: FakeRouter())

    assert isinstance(graph_module.create_graph(), Pregel)


def test_model_configuration_is_checked_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY, OPENAI_MODEL"):
        graph_module._get_order_detector()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Please refund this order.", True),
        ("Return it, please.", True),
        ("请退刚才的订单。", True),
        ("I want to refund another order.", False),
        ("Refund ORD-1234.", False),
    ],
)
def test_previous_order_reference_detection(text: str, expected: bool) -> None:
    assert graph_module._references_previous_order(text) is expected
