"""Build the LangGraph customer-service workflow."""

from langgraph.graph import END, START, StateGraph

from agent import models
from agent.nodes.complaints import handle_complaint
from agent.nodes.intent import build_intent_router_node
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
from agent.routing import (
    route_after_detection,
    route_after_order_lookup,
    route_after_policy,
    route_after_risk_rules,
    route_after_semantic_risk,
    route_by_intent_and_risk,
)
from agent.state import RefundState


def create_graph():
    """Build and compile the customer-service workflow."""
    workflow = StateGraph(RefundState)

    workflow.add_node("check_risk_rules", check_risk_rules)
    workflow.add_node(
        "classify_semantic_risk",
        build_semantic_risk_classifier_node(models.get_risk_classifier()),
    )
    workflow.add_node(
        "llm_call_router",
        build_intent_router_node(models.get_router()),
    )
    workflow.add_node(
        "detect_order",
        build_order_detection_node(models.get_order_detector()),
    )
    workflow.add_node("search_node", search_order_node)
    workflow.add_node(
        "check_refund_eligibility",
        check_refund_eligibility_node,
    )
    workflow.add_node("order_response", order_response_node)
    workflow.add_node("approval_node", approval_node)
    workflow.add_node("proceed", create_refund_node)
    workflow.add_node("cancel", cancelled_node)
    workflow.add_node("handle_complaint", handle_complaint)
    workflow.add_node("confirm_order_priority", confirm_order_priority)
    workflow.add_node("handle_noncritical_risk", handle_noncritical_risk)
    workflow.add_node("handle_critical_risk", handle_critical_risk)

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
            "confirm_order_priority": "confirm_order_priority",
            "order_query": "detect_order",
            "noncritical_risk": "handle_noncritical_risk",
            "complaint": "handle_complaint",
            "END": END,
        },
    )
    workflow.add_conditional_edges(
        "detect_order",
        route_after_detection,
        {"END": END, "search_node": "search_node"},
    )
    workflow.add_conditional_edges(
        "search_node",
        route_after_order_lookup,
        {
            "order_response": "order_response",
            "check_refund_eligibility": "check_refund_eligibility",
            "END": END,
        },
    )
    workflow.add_conditional_edges(
        "check_refund_eligibility",
        route_after_policy,
        {"END": END, "approval_node": "approval_node"},
    )
    workflow.add_edge("handle_complaint", END)
    workflow.add_edge("handle_noncritical_risk", END)
    workflow.add_edge("handle_critical_risk", END)
    workflow.add_edge("order_response", END)
    workflow.add_edge("proceed", END)
    workflow.add_edge("cancel", END)

    return workflow.compile()
