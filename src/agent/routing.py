"""Pure conditional-edge routing functions for the workflow."""

from agent.state import RefundState


def route_by_intent(state: RefundState) -> str:
    """Route the workflow based on the classified user intent."""
    decision = state.get("decision")

    if decision is None:
        return "END"
    if decision in ("refund_request", "order_inquiry"):
        return "order_query"
    if decision == "complaint":
        return "complaint"

    raise ValueError(f"Unexpected intent decision: {decision}")


def route_after_detection(state: RefundState) -> str:
    """Continue only when a current order number is available."""
    return "search_node" if state.get("order_id") else "END"


def route_after_order_lookup(state: RefundState) -> str:
    """Route a successful order lookup according to the current intent."""
    if not state.get("search_success"):
        return "END"

    if state.get("decision") == "order_inquiry":
        return "order_response"
    if state.get("decision") == "refund_request":
        return "check_refund_eligibility"

    raise ValueError(
        f"Unexpected decision after order lookup: {state.get('decision')}"
    )


def route_after_policy(state: RefundState) -> str:
    """Request approval only for automatically eligible refunds."""
    if not state.get("eligible") or state.get("requires_manual_review"):
        return "END"
    return "approval_node"
