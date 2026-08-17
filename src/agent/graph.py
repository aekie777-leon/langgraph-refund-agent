"""Build the LangGraph customer-service workflow."""

from collections.abc import Callable
from typing import Any, cast

from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.graph import END, START, StateGraph

from agent import models
from agent.cases.runtime import get_case_service
from agent.cases.service import CaseService
from agent.nodes.cases import build_finalize_case_handoff_node
from agent.nodes.complaints import (
    build_formal_complaint_classifier_node,
    handle_complaint,
)
from agent.nodes.handoff import (
    acknowledge_human_handoff,
    confirm_human_handoff,
)
from agent.nodes.intent import build_intent_router_node
from agent.nodes.operations import (
    build_attach_operation_case_node,
    build_operation_subgraph,
    build_support_case_status_node,
)
from agent.nodes.orders import (
    build_order_detection_node,
    order_response_node,
    search_order_node,
)
from agent.nodes.refunds import (
    approval_node,
    cancelled_node,
    check_refund_eligibility_node,
    create_refund_node,
)
from agent.nodes.risk import (
    build_semantic_risk_classifier_node,
    check_risk_rules,
    confirm_order_priority,
    handle_critical_risk,
    handle_noncritical_risk,
)
from agent.operations.provider import OrderProvider
from agent.operations.runtime import RuntimeOperationService, RuntimeOrderProvider
from agent.operations.service import OperationService
from agent.routing import (
    route_after_detection,
    route_after_formal_complaint,
    route_after_order_lookup,
    route_after_order_priority,
    route_after_policy,
    route_after_risk_rules,
    route_after_semantic_risk,
    route_by_intent_and_risk,
)
from agent.state import RefundState


def build_graph(
    *,
    case_service_provider: Callable[[], CaseService] = get_case_service,
    order_provider: OrderProvider | None = None,
    operation_service: OperationService | None = None,
    checkpointer: Any | None = None,
):
    """Build the workflow with an injectable support-case service."""
    workflow = StateGraph[RefundState, None, RefundState, RefundState](RefundState)

    workflow.add_node("check_risk_rules", check_risk_rules)
    workflow.add_node(
        "classify_semantic_risk",
        RunnableLambda(
            build_semantic_risk_classifier_node(models.get_risk_classifier())
        ),
    )
    workflow.add_node(
        "operation_subflow",
        build_operation_subgraph(
            order_detector=models.get_order_detector(),
            extractor=models.get_operation_request_extractor(),
            provider=order_provider or RuntimeOrderProvider(),
            service=cast(Any, operation_service or RuntimeOperationService()),
        ),
    )
    workflow.add_node("resume_order_flow", lambda _state: {})
    workflow.add_node(
        "support_case_status",
        build_support_case_status_node(case_service_provider),
    )
    workflow.add_node(
        "llm_call_router",
        RunnableLambda(build_intent_router_node(models.get_router())),
    )
    workflow.add_node(
        "detect_order",
        RunnableLambda(build_order_detection_node(models.get_order_detector())),
    )
    workflow.add_node("search_node", search_order_node)
    workflow.add_node(
        "check_refund_eligibility",
        check_refund_eligibility_node,
    )
    workflow.add_node("order_response", order_response_node)
    workflow.add_node("approval_node", RunnableLambda(approval_node))
    workflow.add_node("proceed", create_refund_node)
    workflow.add_node("cancel", RunnableLambda(cancelled_node))
    workflow.add_node("handle_complaint", handle_complaint)
    workflow.add_node(
        "classify_formal_complaint",
        RunnableLambda(
            build_formal_complaint_classifier_node(
                models.get_formal_complaint_classifier()
            )
        ),
    )
    workflow.add_node(
        "confirm_human_handoff",
        RunnableLambda(confirm_human_handoff),
    )
    workflow.add_node(
        "acknowledge_human_handoff",
        RunnableLambda(acknowledge_human_handoff),
    )
    workflow.add_node(
        "confirm_order_priority",
        RunnableLambda(confirm_order_priority),
    )
    workflow.add_node("handle_noncritical_risk", handle_noncritical_risk)
    workflow.add_node("handle_critical_risk", handle_critical_risk)
    workflow.add_node(
        "finalize_case_handoff",
        RunnableLambda(build_finalize_case_handoff_node(case_service_provider)),
    )
    workflow.add_node(
        "attach_operation_case",
        build_attach_operation_case_node(
            cast(Any, operation_service or RuntimeOperationService())
        ),
    )

    workflow.add_edge(START, "check_risk_rules")
    workflow.add_conditional_edges(
        "check_risk_rules",
        route_after_risk_rules,
        {
            "critical_risk": "handle_critical_risk",
            "semantic_risk": "classify_semantic_risk",
        },
    )
    workflow.add_conditional_edges(
        "classify_semantic_risk",
        route_after_semantic_risk,
        {
            "critical_risk": "handle_critical_risk",
            "intent": "llm_call_router",
        },
    )
    workflow.add_conditional_edges(
        "llm_call_router",
        route_by_intent_and_risk,
        {
            "formal_complaint": "classify_formal_complaint",
            "confirm_human_handoff": "confirm_human_handoff",
            "confirm_order_priority": "confirm_order_priority",
            "order_query": "detect_order",
            "operation_flow": "operation_subflow",
            "support_case_status": "support_case_status",
            "finalize": "finalize_case_handoff",
        },
    )
    workflow.add_conditional_edges(
        "classify_formal_complaint",
        route_after_formal_complaint,
        {
            "confirm_human_handoff": "confirm_human_handoff",
            "noncritical_risk": "handle_noncritical_risk",
            "complaint": "handle_complaint",
        },
    )
    workflow.add_conditional_edges(
        "resume_order_flow",
        route_after_order_priority,
        {
            "order_query": "detect_order",
            "operation_flow": "operation_subflow",
        },
    )
    workflow.add_conditional_edges(
        "detect_order",
        route_after_detection,
        {
            "finalize": "finalize_case_handoff",
            "search_node": "search_node",
        },
    )
    workflow.add_conditional_edges(
        "search_node",
        route_after_order_lookup,
        {
            "order_response": "order_response",
            "check_refund_eligibility": "check_refund_eligibility",
            "finalize": "finalize_case_handoff",
        },
    )
    workflow.add_conditional_edges(
        "check_refund_eligibility",
        route_after_policy,
        {
            "finalize": "finalize_case_handoff",
            "approval_node": "approval_node",
        },
    )
    workflow.add_edge("handle_complaint", "finalize_case_handoff")
    workflow.add_edge("handle_noncritical_risk", "finalize_case_handoff")
    workflow.add_edge("handle_critical_risk", "finalize_case_handoff")
    workflow.add_edge("order_response", "finalize_case_handoff")
    workflow.add_edge("proceed", "finalize_case_handoff")
    workflow.add_edge("cancel", "finalize_case_handoff")
    workflow.add_edge("operation_subflow", "finalize_case_handoff")
    workflow.add_edge("support_case_status", "finalize_case_handoff")
    workflow.add_edge(
        "acknowledge_human_handoff",
        "finalize_case_handoff",
    )
    workflow.add_edge("finalize_case_handoff", "attach_operation_case")
    workflow.add_edge("attach_operation_case", END)

    return workflow.compile(checkpointer=checkpointer)


def create_graph(_config: RunnableConfig):
    """Build the production graph for the LangGraph Agent Server."""
    return build_graph()
