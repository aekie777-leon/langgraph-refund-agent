"""Offline integration tests for the compiled refund graph."""

import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent import graph as graph_module
from agent import models
from agent.auth.provider import UnauthenticatedError
from agent.cases.models import SupportCase, SupportCaseEvent
from agent.cases.service import CaseService
from agent.integrations.models import ProviderAuthentication, ProviderConnection
from agent.integrations.provider import ProviderConnectionNotFoundError
from agent.integrations.provider_failure import ProviderQueueFailureResult
from agent.nodes.cases import build_finalize_case_handoff_node
from agent.operations.demo_provider import DemoOrderProvider
from agent.operations.models import OrderOperationEvent
from agent.operations.runtime import ProviderConfigurationUnavailableError
from agent.operations.service import OperationService
from agent.schemas import (
    FormalComplaintDetection,
    OperationRequestExtraction,
    OrderDetection,
    Route,
    SemanticRiskDetection,
)
from tests.fakes.identity import config_with_identity
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


class FakeProviderConnectionResolver:
    """Resolve one safe connection for offline queued-operation graph tests."""

    async def resolve(self, *, tenant_id: str, capability: str) -> ProviderConnection:
        return ProviderConnection(
            connection_id="provider-demo",
            tenant_id=tenant_id,
            capability=capability,
            base_url="https://provider.example.test",
            endpoint="/v1/commands",
            authentication=ProviderAuthentication(scheme="none"),
        )


class UnavailableProviderConnectionResolver:
    async def resolve(self, **_kwargs):
        raise ProviderConfigurationUnavailableError("unavailable")


class MissingProviderConnectionResolver:
    async def resolve(self, **_kwargs):
        raise ProviderConnectionNotFoundError("missing")


class BrokenProviderConnectionResolver:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def resolve(self, **_kwargs):
        raise self._error


class InMemoryProviderQueueFailureCoordinator:
    """Test-only atomic-shape fake for the provider configuration fallback."""

    def __init__(self, operations, cases) -> None:
        self.operations, self.cases = operations, cases

    async def move_to_manual_review(self, scope, *, operation_id, request_id):
        operation = self.operations.operations[operation_id]
        if operation.status == "manual_review":
            case = self.cases.cases[operation.support_case_id]
            return ProviderQueueFailureResult(operation, case, "duplicate_ignored")
        existing = next((case for case in self.cases.cases.values() if case.tenant_id == scope.tenant_id and case.thread_id == operation.thread_id and case.case_type == "order_operation_review" and case.status != "resolved"), None)
        now = datetime.now(UTC)
        if existing is None:
            case = SupportCase(case_id=uuid4(), thread_id=operation.thread_id, source_message_id=operation.source_message_id, order_id=operation.order_id, case_type="order_operation_review", priority="p1", reason_codes=("provider_delivery_failed",), display_reason="Provider delivery failed and requires human review.", triggering_message_excerpt=operation.request_excerpt, created_at=now, updated_at=now, customer_id=operation.customer_id, tenant_id=operation.tenant_id, created_by="system")
            self.cases.cases[case.case_id] = case
            self.cases.events.append(SupportCaseEvent(event_id=uuid4(), idempotency_key=f"test-provider-failure:{operation_id}:case", case_id=case.case_id, event_type="case_created", source_message_id=case.source_message_id, order_id=case.order_id, reason_codes=case.reason_codes, triggering_message_excerpt=case.triggering_message_excerpt, current_priority="p1", current_status="open", customer_id=case.customer_id, tenant_id=case.tenant_id, created_at=now))
            action = "created"
        else:
            case = existing
            action = "reused"
        updated = operation.model_copy(update={"status": "manual_review", "requires_manual_review": True, "review_case_type": "order_operation_review", "review_priority": "p1", "support_case_id": case.case_id, "updated_at": now, "version": operation.version + 1})
        self.operations.operations[operation_id] = updated
        for event_type in ("confirmation_recorded", "status_changed", "support_case_attached"):
            self.operations.events.append(OrderOperationEvent(event_id=uuid4(), idempotency_key=f"test-provider-failure:{operation_id}:{event_type}", operation_id=operation_id, event_type=event_type, previous_status="pending_confirmation" if event_type == "status_changed" else None, current_status="manual_review" if event_type == "status_changed" else None, support_case_id=case.case_id if event_type == "support_case_attached" else None, actor="system", customer_id=operation.customer_id, tenant_id=operation.tenant_id, created_at=now))
        return ProviderQueueFailureResult(updated, case, action)


def _provider_failure_graph(monkeypatch, case_repository, resolver):
    """Build the real resumable graph with a test-only failure coordinator."""
    monkeypatch.setattr(models, "get_order_detector", lambda: FakeOrderDetector())
    monkeypatch.setattr(models, "get_router", lambda: FakeRouter())
    monkeypatch.setattr(models, "get_formal_complaint_classifier", lambda: FakeFormalComplaintClassifier())
    monkeypatch.setattr(models, "get_risk_classifier", lambda: FakeRiskClassifier())
    monkeypatch.setattr(models, "get_llm", lambda: FakeComplaintModel())
    monkeypatch.setattr(models, "get_operation_request_extractor", lambda: FakeOperationExtractor())
    monkeypatch.setattr("agent.nodes.operations.get_provider_connection_resolver", lambda: resolver)
    monkeypatch.setattr("agent.nodes.cases.get_provider_connection_resolver", lambda: resolver)
    operations = InMemoryOperationRepository()
    service = OperationService(operations, provider_queue_failure_coordinator=InMemoryProviderQueueFailureCoordinator(operations, case_repository))
    return graph_module.build_graph(case_service_provider=lambda: CaseService(case_repository), order_provider=DemoOrderProvider(), operation_service=service, checkpointer=MemorySaver()), operations


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
    resolver = FakeProviderConnectionResolver()
    monkeypatch.setattr("agent.nodes.operations.get_provider_connection_resolver", lambda: resolver)
    monkeypatch.setattr("agent.nodes.cases.get_provider_connection_resolver", lambda: resolver)
    monkeypatch.setattr(models, "get_router", lambda: FakeRouter())
    monkeypatch.setattr(
        models,
        "get_formal_complaint_classifier",
        lambda: FakeFormalComplaintClassifier(),
    )
    resolver = FakeProviderConnectionResolver()
    monkeypatch.setattr("agent.nodes.operations.get_provider_connection_resolver", lambda: resolver)
    monkeypatch.setattr("agent.nodes.cases.get_provider_connection_resolver", lambda: resolver)
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
    resolver = FakeProviderConnectionResolver()
    monkeypatch.setattr("agent.nodes.operations.get_provider_connection_resolver", lambda: resolver)
    monkeypatch.setattr("agent.nodes.cases.get_provider_connection_resolver", lambda: resolver)
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
    return config_with_identity("customer", thread_id=thread_id)


async def test_graph_requests_an_order_number(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="I want a refund.")]}
    )

    assert result["order_id_valid"] is False
    assert result["messages"][-1].content == "Please enter your order number."


async def test_graph_handles_unknown_order(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="Refund ORD-99999 please.")]},
        config=_run_config("unknown-order-thread"),
    )

    assert result["search_success"] is False
    assert "Order not found" in result["messages"][-1].content


async def test_graph_rejects_expired_order(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="Refund ORD-10003 please.")]},
        config=_run_config("expired-order-thread"),
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
        {"messages": [HumanMessage(content="Refund ORD-10001 please.")]},
        config=_run_config("automatic-refund-thread"),
    )

    assert result["eligible"] is True
    assert result["requires_manual_review"] is False
    assert result["__interrupt__"]


async def test_graph_returns_order_information_for_an_inquiry(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {"messages": [HumanMessage(content="What is the status of ORD-10001?")]},
        config=_run_config("order-inquiry-thread"),
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
        },
        config=_run_config("reused-order-thread"),
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
    assert result["operation_status"] == "queued"
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
    assert result["operation_status"] == "queued"
    assert result["support_case_action"] == "not_created"


async def test_graph_provider_configuration_unavailable_creates_manual_review_case(monkeypatch, case_repository) -> None:
    graph, operations = _provider_failure_graph(monkeypatch, case_repository, UnavailableProviderConnectionResolver())
    config = _run_config("provider-unavailable-cancel")
    await graph.ainvoke({"messages": [HumanMessage(id="provider-unavailable-cancel", content="Please cancel ORD-10008.")]}, config=config)
    result = await graph.ainvoke(Command(resume=True), config=config)
    assert result["operation_status"] == "manual_review"
    assert result["operation_service_action"] == "created"
    assert result["support_case_action"] == "created"
    assert result["support_case_type"] == "order_operation_review"
    assert result["support_case_priority"] == "p1"
    assert result["support_case_status"] == "open"
    assert "provider_delivery_failed" in result["support_case_reason_codes"]
    assert result["provider_failure_case_persisted"] is False
    assert operations.outbox_commands == {}


async def test_graph_provider_connection_not_found_creates_manual_review_case(monkeypatch, case_repository) -> None:
    graph, operations = _provider_failure_graph(monkeypatch, case_repository, MissingProviderConnectionResolver())
    config = _run_config("provider-missing-return")
    await graph.ainvoke({"messages": [HumanMessage(id="provider-missing-return", content="Please return ORD-10001.")]}, config=config)
    result = await graph.ainvoke(Command(resume=True), config=config)
    assert result["operation_status"] == "manual_review"
    assert result["support_case_action"] == "created"
    assert operations.outbox_commands == {}


async def test_graph_provider_configuration_failure_replay_is_idempotent(monkeypatch, case_repository) -> None:
    graph, operations = _provider_failure_graph(monkeypatch, case_repository, UnavailableProviderConnectionResolver())
    config = _run_config("provider-unavailable-replay")
    payload = {"messages": [HumanMessage(id="provider-unavailable-replay-message", content="Please cancel ORD-10008.")]}
    await graph.ainvoke(payload, config=config)
    first = await graph.ainvoke(Command(resume=True), config=config)
    operation = next(iter(operations.operations.values()))
    case_count, operation_event_count, case_event_count = len(case_repository.cases), len(operations.events), len(case_repository.events)

    replay = await graph.ainvoke(payload, config=config)

    assert first["support_case_action"] == "created"
    assert replay["operation_status"] == "manual_review"
    # A new graph turn intentionally consumes the one-shot handoff marker.
    # Its no-case result means no second handoff was persisted.
    assert replay["support_case_action"] == "not_created"
    assert len(case_repository.cases) == case_count
    assert len(operations.events) == operation_event_count
    assert len(case_repository.events) == case_event_count
    assert next(iter(operations.operations.values())).support_case_id == operation.support_case_id
    assert operations.outbox_commands == {}


async def test_graph_return_provider_configuration_failure_uses_manual_review(monkeypatch, case_repository) -> None:
    graph, _ = _provider_failure_graph(monkeypatch, case_repository, UnavailableProviderConnectionResolver())
    config = _run_config("provider-return-failure")
    await graph.ainvoke({"messages": [HumanMessage(id="provider-return-failure", content="Please return ORD-10001.")]}, config=config)
    result = await graph.ainvoke(Command(resume=True), config=config)
    assert result["operation_status"] == "manual_review"
    assert result["support_case_type"] == "order_operation_review"


async def test_graph_exchange_provider_configuration_failure_uses_manual_review(monkeypatch, case_repository) -> None:
    graph, _ = _provider_failure_graph(monkeypatch, case_repository, UnavailableProviderConnectionResolver())
    config = _run_config("provider-exchange-failure")
    await graph.ainvoke({"messages": [HumanMessage(id="provider-exchange-failure", content="Please exchange ORD-10001.")]}, config=config)
    result = await graph.ainvoke(Command(resume=True), config=config)
    assert result["operation_status"] == "manual_review"
    assert result["support_case_priority"] == "p1"


async def test_graph_normal_provider_connection_still_queues_one_outbox(monkeypatch, case_repository) -> None:
    graph, operations = _provider_failure_graph(monkeypatch, case_repository, FakeProviderConnectionResolver())
    config = _run_config("provider-normal-return")
    await graph.ainvoke({"messages": [HumanMessage(id="provider-normal-return", content="Please return ORD-10001.")]}, config=config)
    result = await graph.ainvoke(Command(resume=True), config=config)
    assert result["operation_status"] == "queued"
    assert result["support_case_action"] == "not_created"
    assert len(operations.outbox_commands) == 1


async def test_graph_normal_cancellation_connection_still_queues_one_outbox(monkeypatch, case_repository) -> None:
    graph, operations = _provider_failure_graph(monkeypatch, case_repository, FakeProviderConnectionResolver())
    config = _run_config("provider-normal-cancellation")
    await graph.ainvoke({"messages": [HumanMessage(id="provider-normal-cancellation", content="Please cancel ORD-10008.")]}, config=config)
    result = await graph.ainvoke(Command(resume=True), config=config)
    assert result["operation_status"] == "queued"
    assert result["support_case_action"] == "not_created"
    assert len(operations.outbox_commands) == 1


async def test_graph_provider_failure_case_marker_does_not_leak_to_next_turn(monkeypatch, case_repository) -> None:
    graph, _ = _provider_failure_graph(monkeypatch, case_repository, UnavailableProviderConnectionResolver())
    config = _run_config("provider-marker-next-turn")
    await graph.ainvoke({"messages": [HumanMessage(id="provider-marker-one", content="Please return ORD-10001.")]}, config=config)
    await graph.ainvoke(Command(resume=True), config=config)
    result = await graph.ainvoke({"messages": [HumanMessage(id="provider-marker-two", content="employee insulted me.")]}, config=config)
    assert result["support_case_type"] == "staff_conduct_complaint"
    assert result["provider_failure_case_persisted"] is False


async def test_finalize_case_handoff_consumes_provider_failure_case_marker() -> None:
    """A provider fallback case survives finalization exactly once."""
    node = build_finalize_case_handoff_node(service_provider=lambda: None)  # type: ignore[arg-type]
    state = {
        "provider_failure_case_persisted": True,
        "support_case_action": "created",
        "support_case_id": "00000000-0000-0000-0000-000000000001",
        "support_case_type": "order_operation_review",
        "support_case_priority": "p1",
        "support_case_status": "open",
        "support_case_reason_codes": ["provider_delivery_failed"],
    }
    result = await node(state, _run_config("provider-failure-marker-thread"))

    assert result["support_case_action"] == "created"
    assert result["support_case_id"] == state["support_case_id"]
    assert result["provider_failure_case_persisted"] is False


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


@pytest.mark.parametrize(
    "error",
    [ValueError("unexpected resolver value"), RuntimeError("unexpected resolver runtime")],
)
async def test_graph_delivery_resolver_unknown_error_propagates(error: Exception) -> None:
    node = build_finalize_case_handoff_node(
        service_provider=lambda: None,  # type: ignore[arg-type]
        connection_resolver_provider=lambda: BrokenProviderConnectionResolver(error),
    )
    state = {
        "domain_case_reason_codes": ["delivery_tracking_stalled"],
        "operation_extraction": {"delivery_issue_type": "tracking_stalled", "ambiguous": False},
        "messages": [HumanMessage(id=f"delivery-resolver-{type(error).__name__}", content="Tracking has stalled for ORD-10010.")],
        "order_id": "ORD-10010",
    }
    with pytest.raises(type(error), match="unexpected resolver"):
        await node(state, _run_config(f"delivery-resolver-{type(error).__name__}"))


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


async def test_graph_denies_another_customers_order(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(content="What is the status of ORD-10001?")
            ]
        },
        config=config_with_identity("customer", user_id="customer-b"),
    )

    assert result["search_success"] is False
    assert "Order not found" in result["messages"][-1].content


async def test_graph_allows_own_safety_order(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(content="What is the status of ORD-20001?")
            ]
        },
        config=config_with_identity("customer", user_id="customer-b"),
    )

    assert result["search_success"] is True
    assert result["last_order_id"] == "ORD-20001"
    assert "Status: delivered" in result["messages"][-1].content


async def test_graph_denies_other_tenant_order(refund_graph) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(content="What is the status of ORD-30001?")
            ]
        },
        config=config_with_identity("customer", user_id="customer-a"),
    )

    assert result["search_success"] is False
    assert "Order not found" in result["messages"][-1].content


async def test_graph_fails_closed_without_identity(refund_graph) -> None:
    with pytest.raises(UnauthenticatedError):
        await refund_graph.ainvoke(
            {
                "messages": [
                    HumanMessage(content="What is the status of ORD-10001?")
                ]
            },
            config={"configurable": {"thread_id": "no-auth-thread"}},
        )


async def test_graph_state_does_not_contain_identity_or_credentials(
    refund_graph,
) -> None:
    result = await refund_graph.ainvoke(
        {
            "messages": [
                HumanMessage(content="What is the status of ORD-10001?")
            ]
        },
        config=_run_config("no-token-state-thread"),
    )

    assert "langgraph_auth_user" not in result
    assert "Bearer" not in str(result)
    assert "token" not in str(result).lower()
