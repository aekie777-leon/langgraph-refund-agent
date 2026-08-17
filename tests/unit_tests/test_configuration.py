"""Tests for graph construction and configuration validation."""

import json
from pathlib import Path

import pytest
from langgraph.pregel import Pregel

from agent import graph as graph_module
from agent import models
from agent.schemas import (
    FormalComplaintDetection,
    OperationRequestExtraction,
    OrderDetection,
    Route,
    SemanticRiskDetection,
)


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
        return Route(
            step="refund_request",
            human_handoff_requested=False,
        )


class FakeFormalComplaintClassifier:
    """Return an ordinary complaint classification without an API call."""

    async def ainvoke(self, _messages):
        return FormalComplaintDetection(
            complaint_kind="ordinary",
            reason="No formal complaint is present.",
        )


class FakeRiskClassifier:
    """Return a fixed semantic-risk result without calling an API."""

    async def ainvoke(self, _messages):
        return SemanticRiskDetection(
            risk_level="none",
            categories=[],
            reason="No semantic risk is present.",
        )


class FakeOperationRequestExtractor:
    """Return a fixed operation extraction without calling an API."""

    async def ainvoke(self, _messages):
        return OperationRequestExtraction(
            operation_type="return",
            reason="changed_mind",
            ambiguous=False,
        )


def test_build_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        models,
        "get_order_detector",
        lambda: FakeOrderDetector(),
    )
    monkeypatch.setattr(models, "get_router", lambda: FakeRouter())
    monkeypatch.setattr(
        models,
        "get_formal_complaint_classifier",
        lambda: FakeFormalComplaintClassifier(),
    )
    monkeypatch.setattr(
        models,
        "get_risk_classifier",
        lambda: FakeRiskClassifier(),
    )
    monkeypatch.setattr(
        models,
        "get_operation_request_extractor",
        lambda: FakeOperationRequestExtractor(),
    )

    graph = graph_module.build_graph()

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
        "classify_formal_complaint",
        "confirm_human_handoff",
        "acknowledge_human_handoff",
        "check_risk_rules",
        "classify_semantic_risk",
        "confirm_order_priority",
        "handle_noncritical_risk",
        "handle_critical_risk",
        "finalize_case_handoff",
        "operation_subflow",
    }.issubset(graph.get_graph().nodes)


def test_agent_server_factory_accepts_runnable_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        models,
        "get_order_detector",
        lambda: FakeOrderDetector(),
    )
    monkeypatch.setattr(models, "get_router", lambda: FakeRouter())
    monkeypatch.setattr(
        models,
        "get_formal_complaint_classifier",
        lambda: FakeFormalComplaintClassifier(),
    )
    monkeypatch.setattr(
        models,
        "get_risk_classifier",
        lambda: FakeRiskClassifier(),
    )
    monkeypatch.setattr(
        models,
        "get_operation_request_extractor",
        lambda: FakeOperationRequestExtractor(),
    )

    graph = graph_module.create_graph(
        {"configurable": {"thread_id": "factory-test-thread"}}
    )

    assert isinstance(graph, Pregel)
    assert "finalize_case_handoff" in graph.get_graph().nodes


def test_langgraph_uses_the_custom_lifespan_app() -> None:
    config = json.loads(Path("langgraph.json").read_text(encoding="utf-8"))

    assert config["http"]["app"] == "./src/agent/webapp.py:app"


def test_docker_image_registers_the_custom_lifespan_app() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "ENV LANGGRAPH_HTTP=" in dockerfile
    assert "/deps/project/src/agent/webapp.py:app" in dockerfile


def test_model_configuration_is_checked_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY, OPENAI_MODEL"):
        models.get_order_detector()
