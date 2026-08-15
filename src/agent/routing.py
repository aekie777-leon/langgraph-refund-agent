"""Pure conditional-edge routing functions for the workflow."""

from agent.state import RefundState


def route_after_risk_rules(state: RefundState) -> str:
    """Route hard-critical rule matches before semantic classification."""
    if state.get("risk_hard_critical"):
        return "critical_risk"
    return "semantic_risk"


def route_after_semantic_risk(state: RefundState) -> str:
    """Route the workflow according to semantic risk severity."""
    risk_level = state.get("semantic_risk_level")

    if risk_level in ("high", "critical"):
        return "critical_risk"
    if risk_level in ("none", "low", "medium"):
        return "intent"

    raise ValueError(f"Unexpected semantic risk level: {risk_level!r}")


def route_by_intent_and_risk(state: RefundState) -> str:
    """Route by intent, risk, and explicit human-support requests."""
    decision = state.get("decision")
    risk_level = state.get("semantic_risk_level")

    if decision is None:
        return "finalize"
    if decision == "complaint":
        return "formal_complaint"
    if state.get("human_handoff_requested"):
        return "confirm_human_handoff"
    if decision in ("refund_request", "order_inquiry"):
        if risk_level in ("low", "medium"):
            return "confirm_order_priority"
        if risk_level == "none":
            return "order_query"

    raise ValueError(
        "Unexpected intent/risk combination: "
        f"decision={decision!r}, risk_level={risk_level!r}"
    )


def route_after_formal_complaint(state: RefundState) -> str:
    """Route a classified complaint to confirmation or a response node."""
    if state.get("human_handoff_requested"):
        return "confirm_human_handoff"
    if state.get("semantic_risk_level") in ("low", "medium"):
        return "noncritical_risk"
    return "complaint"


def route_after_human_handoff_declined(state: RefundState) -> str:
    """Resume the previously classified self-service flow."""
    decision = state.get("decision")
    risk_level = state.get("semantic_risk_level")

    if decision == "complaint":
        if risk_level in ("low", "medium"):
            return "handle_noncritical_risk"
        if risk_level == "none":
            return "handle_complaint"
    if decision in ("refund_request", "order_inquiry"):
        if risk_level in ("low", "medium"):
            return "confirm_order_priority"
        if risk_level == "none":
            return "detect_order"

    raise ValueError(
        "Cannot resume self-service flow: "
        f"decision={decision!r}, risk_level={risk_level!r}"
    )


def route_after_detection(state: RefundState) -> str:
    """Continue only when a current order number is available."""
    return "search_node" if state.get("order_id") else "finalize"


def route_after_order_lookup(state: RefundState) -> str:
    """Route a successful order lookup according to the current intent."""
    if not state.get("search_success"):
        return "finalize"

    if state.get("decision") == "order_inquiry":
        return "order_response"
    if state.get("decision") == "refund_request":
        return "check_refund_eligibility"

    raise ValueError(f"Unexpected decision after order lookup: {state.get('decision')}")


def route_after_policy(state: RefundState) -> str:
    """Request approval only for automatically eligible refunds."""
    if not state.get("eligible") or state.get("requires_manual_review"):
        return "finalize"
    return "approval_node"
