"""LangGraph state and state-access helpers."""

from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.graph import MessagesState

from agent.schemas import Intent, SemanticRiskCategory, SemanticRiskLevel


class RefundState(MessagesState, total=False):
    """State shared by the customer-service workflow nodes."""

    order_id: str | None
    last_order_id: str | None
    order_info: dict[str, Any]
    success: bool
    search_success: bool
    eligible: bool
    requires_manual_review: bool
    order_id_valid: bool
    reason: str
    decision: Intent | None
    risk_hard_critical: bool
    risk_has_signals: bool
    risk_rule_matches: list[dict[str, Any]]
    semantic_risk_level: SemanticRiskLevel | None
    semantic_risk_categories: list[SemanticRiskCategory]
    semantic_risk_reason: str
    risk_order_choice: str | None
    risk_requires_human_review: bool


def latest_text_user_message(state: RefundState) -> HumanMessage | None:
    """Return the most recent human message when its content is text."""
    message = next(
        (
            message
            for message in reversed(state.get("messages", []))
            if isinstance(message, HumanMessage)
        ),
        None,
    )
    if message is None or not isinstance(message.content, str):
        return None
    return message


def new_turn_state(decision: Intent | None) -> dict[str, Any]:
    """Reset request-scoped fields while preserving conversation context."""
    return {
        "decision": decision,
        "order_id": None,
        "order_info": {},
        "success": False,
        "search_success": False,
        "eligible": False,
        "requires_manual_review": False,
        "order_id_valid": False,
        "reason": "",
    }
