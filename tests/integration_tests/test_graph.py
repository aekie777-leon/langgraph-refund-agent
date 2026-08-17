"""Offline integration tests for the compiled refund graph."""

import re

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent import graph as graph_module
from agent import models
from agent.cases.service import CaseService
from agent.operations.demo_provider import DemoOrderProvider
from agent.operations.service import OperationService
from agent.schemas import (
    FormalComplaintDetection,
    OperationRequestExtraction,
    OrderDetection,
    Route,
    SemanticRiskDetection,
)
from tests.operation_support import InMemoryOperationRepository
from tests.support_cases import InMemoryCaseRepository

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
        human_handoff_requested = any(
            phrase in text for phrase in ("human agent", "real person", "真人客服")
        )
        if "case status" in text or "support request status" in text:
            step = "support_case_status"
        elif "cancel" in text:
            step = "cancellation_request"
        elif "exchange" in text:
            step = "exchange_request"
        elif any(word in text for word in ("tracking", "not received", "delivery failed")):
            step = "delivery_issue"
        elif "return" in text:
            step = "return_request"
        elif (
            any(
                word in text
                for word in (
                    "complaint",
                    "terrible",
                    "unhappy",
                    "grievance",
                    "employee",
                )
            )
            or human_handoff_requested
        ):
            step = "complaint"
        elif any(word in text for word in ("status", "where", "track")):
            step = "order_inquiry"
        else:
            step = "refund_request"
        return Route(
            step=step,
            human_handoff_requested=human_handoff_requested,
        )


class FakeFormalComplaintClassifier:
    """Classify formal complaint examples without making a network call."""

    async def ainvoke(self, messages):
        text = messages[-1].content.lower()
        if "employee insulted" in text:
            return FormalComplaintDetection(
                complaint_kind="staff_conduct",
                staff_complaint_severity="medium",
                reason="The user explicitly reported abusive staff conduct.",
            )
        if "official grievance" in text:
            return FormalComplaintDetection(
                complaint_kind="other_formal",
                reason="The user explicitly requested a formal complaint.",
            )
        return FormalComplaintDetection(
            complaint_kind="ordinary",
            reason="The user expressed ordinary dissatisfaction.",
        )


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


class FakeOperationExtractor:
    """Return a valid fallback extraction when operation nodes are constructed."""

    async def ainvoke(self, messages):
        text = messages[-1].content.lower()
        if "cancel" in text and "exchange" in text:
            return OperationRequestExtraction(ambiguous=True)
        if "cancel" in text:
            return OperationRequestExtraction(
                operation_type="cancellation",
                reason="no_longer_needed",
                ambiguous=False,
            )
        if "tracking" in text:
            return OperationRequestExtraction(
                delivery_issue_type="tracking_stalled",
                ambiguous=False,
            )
        if "exchange" in text:
            return OperationRequestExtraction(
                operation_type="exchange",
                reason="changed_mind",
                replacement_variant_id=(
                    "variant-unavailable" if "unavailable" in text else "variant-blue"
                ),
                ambiguous=False,
            )
        return OperationRequestExtraction(
            operation_type="return",
            reason="changed_mind",
            ambiguous=False,
        )


@pytest.fixture
def case_repository() -> InMemoryCaseRepository:
    return InMemoryCaseRepository()


@pytest.fixture
def refund_graph(
    monkeypatch: pytest.MonkeyPatch,
    case_repository: InMemoryCaseRepository,
):
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
    monkeypatch.setattr(models, "get_llm", lambda: FakeComplaintModel())
    monkeypatch.setattr(
        models,
        "get_operation_request_extractor",
        lambda: FakeOperationExtractor(),
    )
    case_service = CaseService(case_repository)
    operation_service = OperationService(InMemoryOperationRepository())
    return graph_module.build_graph(
        case_service_provider=lambda: case_service,
        order_provider=DemoOrderProvider(),
        operation_service=operation_service,
    )


@pytest.fixture
def resumable_operation_graph(
    monkeypatch: pytest.MonkeyPatch,
    case_repository: InMemoryCaseRepository,
):
    monkeypatch.setattr(models, "get_order_detector", lambda: FakeOrderDetector())
    monkeypatch.setattr(models, "get_router", lambda: FakeRouter())
    monkeypatch.setattr(
        models,
        "get_formal_complaint_classifier",
        lambda: FakeFormalComplaintClassifier(),
    )
    monkeypatch.setattr(models, "get_risk_classifier", lambda: FakeRiskClassifier())
    monkeypatch.setattr(models, "get_llm", lambda: FakeComplaintModel())
    monkeypatch.setattr(
        models,
        "get_operation_request_extractor",
        lambda: FakeOperationExtractor(),
    )
    case_service = CaseService(case_repository)
    operation_service = OperationService(InMemoryOperationRepository())
    return graph_module.build_graph(
        case_service_provider=lambda: case_service,
        order_provider=DemoOrderProvider(),
        operation_service=operation_service,
        checkpointer=MemorySaver(),
    )


def _run_config(thread_id: str):
    return {"configurable": {"thread_id": thread_id}}


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
        {
            "messages": [
                HumanMessage(
                    id="large-refund-message",
                    content="Refund ORD-10002 please.",
                )
            ]
        },
        config=_run_config("large-refund-thread"),
    )

    assert result["eligible"] is True
    assert result["requires_manual_review"] is True
    assert "customer service" in result["messages"][-1].content
    assert result["support_case_action"] == "created"
    assert result["support_case_type"] == "refund_review"
    assert result["support_case_priority"] == "p1"


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
    assert result["support_case_action"] == "not_created"


async def test_graph_handles_a_complaint_without_order_operations(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="The delivery service was terrible.")]}
    )

    assert result["decision"] == "complaint"
    assert result["order_id"] is None
    assert result["order_info"] == {}
    assert result["eligible"] is False
    assert "understand your concern" in result["messages"][-1].content
    assert result["support_case_action"] == "not_created"


async def test_graph_creates_a_staff_conduct_case(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="staff-complaint-message",
                    content="Your employee insulted me during the call.",
                )
            ]
        },
        config=_run_config("staff-complaint-thread"),
    )

    assert result["staff_complaint_severity"] == "medium"
    assert result["explicit_other_complaint"] is False
    assert result["support_case_action"] == "created"
    assert result["support_case_type"] == "staff_conduct_complaint"
    assert result["support_case_priority"] == "p1"
    assert result["support_case_reason_codes"] == ["staff_conduct_medium"]


async def test_graph_creates_an_explicit_other_complaint_case(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="formal-complaint-message",
                    content="I want to lodge an official grievance about delivery.",
                )
            ]
        },
        config=_run_config("formal-complaint-thread"),
    )

    assert result["staff_complaint_severity"] is None
    assert result["explicit_other_complaint"] is True
    assert result["support_case_action"] == "created"
    assert result["support_case_type"] == "other_complaint"
    assert result["support_case_priority"] == "p3"
    assert result["support_case_reason_codes"] == ["explicit_other_complaint"]


async def test_graph_asks_to_confirm_an_explicit_human_request(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="human-request-message",
                    content="I want to speak with a human agent.",
                )
            ]
        },
        config=_run_config("human-request-thread"),
    )

    assert result["human_handoff_requested"] is True
    assert result["human_handoff_confirmed"] is False
    assert result["__interrupt__"]
    assert result["__interrupt__"][0].value["type"] == "human_handoff_confirmation"


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
        {
            "messages": [
                HumanMessage(
                    id="hard-critical-message",
                    content="I will kill you.",
                )
            ]
        },
        config=_run_config("hard-critical-thread"),
    )

    assert result["risk_hard_critical"] is True
    assert result["risk_requires_human_review"] is True
    assert result["semantic_risk_level"] is None
    assert result["decision"] is None
    assert "urgent safety concern" in result["messages"][-1].content
    assert result["support_case_action"] == "created"
    assert result["support_case_type"] == "safety_review"
    assert result["support_case_priority"] == "p0"


async def test_graph_routes_semantic_high_risk_to_critical_handling(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="semantic-high-message",
                    content="This is immediate semantic danger.",
                )
            ]
        },
        config=_run_config("semantic-high-thread"),
    )

    assert result["risk_hard_critical"] is False
    assert result["semantic_risk_level"] == "high"
    assert result["semantic_risk_categories"] == ["violence"]
    assert result["risk_requires_human_review"] is True
    assert result["decision"] is None
    assert result["support_case_action"] == "created"
    assert result["support_case_type"] == "safety_review"
    assert result["support_case_priority"] == "p0"


async def test_graph_asks_before_processing_a_risky_refund_request(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "Please refund ORD-10001 or I will contact consumer protection."
                    )
                )
            ]
        }
    )

    assert result["semantic_risk_level"] == "medium"
    assert result["semantic_risk_categories"] == ["regulatory"]
    assert result["decision"] == "refund_request"
    assert result["__interrupt__"]
    assert result["__interrupt__"][0].value["type"] == "order_priority_confirmation"


async def test_medium_self_harm_language_does_not_hide_an_order_inquiry(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "I want to bang my head against the wall. Where is ORD-10001?"
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
                    id="medium-regulatory-message",
                    content="I am considering a formal complaint about delivery.",
                )
            ]
        },
        config=_run_config("medium-regulatory-thread"),
    )

    assert result["semantic_risk_level"] == "medium"
    assert result["decision"] == "complaint"
    assert "understand your concern" in result["messages"][-1].content
    assert "__interrupt__" not in result
    assert result["support_case_action"] == "created"
    assert result["support_case_type"] == "business_escalation"
    assert result["support_case_priority"] == "p2"


async def test_graph_appends_a_second_trigger_to_the_same_case(
    refund_graph,
    case_repository: InMemoryCaseRepository,
) -> None:
    config = _run_config("repeated-risk-thread")
    await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="risk-message-1",
                    content="This is immediate semantic danger.",
                )
            ]
        },
        config=config,
    )
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="risk-message-2",
                    content="This is immediate semantic danger again.",
                )
            ]
        },
        config=config,
    )

    assert result["support_case_action"] == "event_appended"
    assert len(case_repository.cases) == 1
    assert len(case_repository.events) == 2


async def test_graph_ignores_a_retried_source_message(
    refund_graph,
    case_repository: InMemoryCaseRepository,
) -> None:
    config = _run_config("retried-risk-thread")
    payload = {
        "messages": [
            HumanMessage(
                id="stable-risk-message",
                content="This is immediate semantic danger.",
            )
        ]
    }
    first = await refund_graph.ainvoke(payload, config=config)
    duplicate = await refund_graph.ainvoke(payload, config=config)

    assert first["support_case_action"] == "created"
    assert duplicate["support_case_action"] == "duplicate_ignored"
    assert duplicate["support_case_id"] == first["support_case_id"]
    assert len(case_repository.events) == 1


async def test_graph_interrupts_before_an_automatic_cancellation(
    resumable_operation_graph,
) -> None:
    result = await resumable_operation_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="cancellation-message",
                    content="Please cancel ORD-10008.",
                )
            ]
        },
        config=_run_config("cancellation-thread"),
    )

    assert result["__interrupt__"][0].value["type"] == "order_operation_confirmation"


async def test_graph_confirms_manual_cancellation_and_creates_case(
    resumable_operation_graph,
) -> None:
    config = _run_config("manual-cancellation-thread")
    initial = await resumable_operation_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="manual-cancellation-message",
                    content="Please cancel ORD-10009.",
                )
            ]
        },
        config=config,
    )
    result = await resumable_operation_graph.ainvoke(Command(resume=True), config=config)

    assert initial["__interrupt__"]
    assert result["operation_status"] == "manual_review"
    assert result["support_case_type"] == "order_operation_review"
    assert result["support_case_priority"] == "p1"
    assert result["support_case_id"] is not None


async def test_graph_confirms_eligible_return_without_a_support_case(
    resumable_operation_graph,
) -> None:
    config = _run_config("eligible-return-thread")
    initial = await resumable_operation_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="eligible-return-message",
                    content="Please return ORD-10001.",
                )
            ]
        },
        config=config,
    )
    result = await resumable_operation_graph.ainvoke(Command(resume=True), config=config)

    assert initial["__interrupt__"]
    assert result["operation_outcome"] == "eligible"
    assert result["operation_status"] == "submitted"
    assert result["support_case_action"] == "not_created"


async def test_graph_confirms_manual_return_and_creates_p1_case(
    resumable_operation_graph,
) -> None:
    config = _run_config("manual-return-thread")
    initial = await resumable_operation_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="manual-return-message",
                    content="Please return ORD-10002.",
                )
            ]
        },
        config=config,
    )
    result = await resumable_operation_graph.ainvoke(Command(resume=True), config=config)

    assert initial["__interrupt__"]
    assert result["operation_status"] == "manual_review"
    assert result["support_case_type"] == "order_operation_review"
    assert result["support_case_priority"] == "p1"


async def test_graph_confirms_available_exchange_without_a_support_case(
    resumable_operation_graph,
) -> None:
    config = _run_config("available-exchange-thread")
    initial = await resumable_operation_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="available-exchange-message",
                    content="Please exchange ORD-10001.",
                )
            ]
        },
        config=config,
    )
    result = await resumable_operation_graph.ainvoke(Command(resume=True), config=config)

    assert initial["__interrupt__"]
    assert result["operation_outcome"] == "eligible"
    assert result["operation_status"] == "submitted"
    assert result["support_case_action"] == "not_created"


async def test_graph_rejects_exchange_when_replacement_is_unavailable(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="unavailable-exchange-message",
                    content="Please exchange ORD-10001 for an unavailable variant.",
                )
            ]
        },
        config=_run_config("unavailable-exchange-thread"),
    )

    assert result["operation_outcome"] == "rejected"
    assert result["operation_id"] is None
    assert result["support_case_action"] == "not_created"
    assert "replacement is unavailable" in result["messages"][-1].content


async def test_graph_asks_to_choose_one_ambiguous_operation(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="ambiguous-operation-message",
                    content="Please cancel and exchange ORD-10001.",
                )
            ]
        },
        config=_run_config("ambiguous-operation-thread"),
    )

    assert result["operation_outcome"] == "ambiguous"
    assert result["operation_id"] is None
    assert result["support_case_action"] == "not_created"
    assert "choose one operation" in result["messages"][-1].content


async def test_graph_declines_pending_operation_without_submitting_or_creating_case(
    resumable_operation_graph,
) -> None:
    config = _run_config("declined-operation-thread")
    initial = await resumable_operation_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="declined-operation-message",
                    content="Please cancel ORD-10008.",
                )
            ]
        },
        config=config,
    )
    result = await resumable_operation_graph.ainvoke(Command(resume=False), config=config)

    assert initial["__interrupt__"]
    assert result["operation_status"] == "cancelled_by_customer"
    assert result["provider_reference"] is None
    assert result["support_case_action"] == "not_created"


async def test_graph_confirms_delivery_case_without_order_operation(
    resumable_operation_graph,
) -> None:
    config = _run_config("tracking-thread")
    initial = await resumable_operation_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="tracking-message",
                    content="Tracking for ORD-10010 has not updated.",
                )
            ]
        },
        config=config,
    )
    result = await resumable_operation_graph.ainvoke(Command(resume=True), config=config)

    assert initial["__interrupt__"][0].value["type"] == "order_operation_confirmation"
    assert result["operation_outcome"] == "manual_review"
    assert result["operation_id"] is None
    assert result["support_case_type"] == "delivery_investigation"
    assert result["support_case_priority"] == "p1"


async def test_graph_declines_delivery_investigation_without_creating_case(
    resumable_operation_graph,
) -> None:
    config = _run_config("declined-delivery-thread")
    initial = await resumable_operation_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="declined-delivery-message",
                    content="Tracking for ORD-10010 has not updated.",
                )
            ]
        },
        config=config,
    )
    result = await resumable_operation_graph.ainvoke(Command(resume=False), config=config)

    assert initial["__interrupt__"]
    assert result["operation_id"] is None
    assert result["operation_status"] == "cancelled"
    assert result["support_case_action"] == "not_created"


async def test_graph_lists_support_cases_only_for_the_current_thread(
    resumable_operation_graph,
) -> None:
    config = _run_config("case-status-thread")
    initial = await resumable_operation_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="status-tracking-message",
                    content="Tracking for ORD-10010 has not updated.",
                )
            ]
        },
        config=config,
    )
    assert initial["__interrupt__"]
    await resumable_operation_graph.ainvoke(Command(resume=True), config=config)
    result = await resumable_operation_graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    id="case-status-message",
                    content="What is my support request status?",
                )
            ]
        },
        config=config,
    )

    assert "delivery_investigation" in result["messages"][-1].content
    assert "Support cases for this conversation" in result["messages"][-1].content
