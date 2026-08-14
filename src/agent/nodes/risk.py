"""Risk detection, classification, and handling nodes."""

import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any, Literal

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.types import Command, interrupt

from agent import models
from agent.prompts import (
    NONCRITICAL_RISK_RESPONSE_SYSTEM_PROMPT,
    SEMANTIC_RISK_CLASSIFIER_SYSTEM_PROMPT,
)
from agent.risk_matcher import RiskRuleMatcher
from agent.schemas import SemanticRiskDetection
from agent.state import RefundState, latest_text_user_message, new_turn_state

AsyncNode = Callable[[RefundState], Awaitable[dict[str, Any]]]

_RISK_MATCHER = RiskRuleMatcher.from_json()


def check_risk_rules(state: RefundState) -> dict[str, Any]:
    """Check deterministic risk rules against the latest user message."""
    latest_user_message = latest_text_user_message(state)
    if latest_user_message is None:
        text = ""
    else:
        if not isinstance(latest_user_message.content, str):
            raise TypeError("Risk rule matching requires text content")
        text = latest_user_message.content

    result = _RISK_MATCHER.match(text)

    return {
        **new_turn_state(None),
        "risk_hard_critical": result.hard_critical,
        "risk_has_signals": result.has_risk_signals,
        "risk_rule_matches": [asdict(match) for match in result.matches],
        "semantic_risk_level": None,
        "semantic_risk_categories": [],
        "semantic_risk_reason": "",
        "risk_order_choice": None,
        "risk_requires_human_review": False,
    }


def build_semantic_risk_classifier_node(classifier: Any) -> AsyncNode:
    """Build the semantic risk-classification node."""

    async def classify_semantic_risk(state: RefundState) -> dict[str, Any]:
        latest_user_message = latest_text_user_message(state)
        if latest_user_message is None:
            return {
                "semantic_risk_level": "none",
                "semantic_risk_categories": [],
                "semantic_risk_reason": "No text user message was available.",
            }

        rule_context = json.dumps(
            state.get("risk_rule_matches", []),
            ensure_ascii=False,
        )
        result = await classifier.ainvoke(
            [
                SystemMessage(content=SEMANTIC_RISK_CLASSIFIER_SYSTEM_PROMPT),
                SystemMessage(
                    content=(
                        "Deterministic rule matches for the latest message: "
                        f"{rule_context}"
                    )
                ),
                *state.get("messages", []),
            ]
        )
        if not isinstance(result, SemanticRiskDetection):
            raise TypeError("Risk classifier returned an unexpected result")

        return {
            "semantic_risk_level": result.risk_level,
            "semantic_risk_categories": result.categories,
            "semantic_risk_reason": result.reason,
        }

    return classify_semantic_risk


def confirm_order_priority(
    _state: RefundState,
) -> Command[Literal["detect_order", "handle_noncritical_risk"]]:
    """Ask whether the user wants to handle the order request now."""
    decision = interrupt(
        {
            "type": "order_priority_confirmation",
            "message": (
                "I understand that this situation is frustrating, "
                "and I want to help resolve it."
            ),
            "question": "Would you like me to handle your order request now?",
            "options": [
                {
                    "value": "handle_order",
                    "label": "Yes, handle the order",
                },
                {
                    "value": "continue_risk",
                    "label": "No, continue with my concern",
                },
            ],
        }
    )

    if decision == "handle_order":
        return Command(
            goto="detect_order",
            update={"risk_order_choice": "handle_order"},
        )
    if decision == "continue_risk":
        return Command(
            goto="handle_noncritical_risk",
            update={"risk_order_choice": "continue_risk"},
        )

    raise ValueError(f"Unexpected order-priority decision: {decision!r}")


async def handle_noncritical_risk(state: RefundState) -> dict[str, Any]:
    """Respond to a non-critical contextual risk."""
    latest_user_message = latest_text_user_message(state)
    if latest_user_message is None:
        return {
            "messages": [AIMessage(content="Please describe your concern as text.")]
        }

    risk_context = {
        "level": state.get("semantic_risk_level"),
        "categories": state.get("semantic_risk_categories", []),
        "reason": state.get("semantic_risk_reason", ""),
    }
    response = await models.get_llm().ainvoke(
        [
            SystemMessage(content=NONCRITICAL_RISK_RESPONSE_SYSTEM_PROMPT),
            SystemMessage(
                content=(
                    "Current semantic risk assessment: "
                    f"{json.dumps(risk_context, ensure_ascii=False)}"
                )
            ),
            latest_user_message,
        ]
    )
    return {"messages": [response]}


def handle_critical_risk(state: RefundState) -> dict[str, Any]:
    """Return a conservative response for a critical risk."""
    categories = set(state.get("semantic_risk_categories", []))
    if state.get("risk_hard_critical"):
        categories.update(
            match["category"]
            for match in state.get("risk_rule_matches", [])
            if match.get("category")
        )

    if categories.intersection({"self_harm", "violence"}):
        content = (
            "Your message may indicate an urgent safety concern. "
            "If you or someone else may be in immediate danger, please contact "
            "local emergency services or a trusted person who can help now."
        )
    else:
        support_contact = os.getenv(
            "CUSTOMER_SERVICE_CONTACT",
            "your customer service team",
        )
        content = (
            "This concern requires human review. "
            f"Please contact {support_contact} for further assistance."
        )

    return {
        "risk_requires_human_review": True,
        "messages": [AIMessage(content=content)],
    }
