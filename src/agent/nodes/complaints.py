"""Complaint classification and response nodes."""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage

from agent import models
from agent.prompts import (
    COMPLAINT_SYSTEM_PROMPT,
    FORMAL_COMPLAINT_CLASSIFIER_SYSTEM_PROMPT,
)
from agent.schemas import FormalComplaintDetection
from agent.state import RefundState, latest_text_user_message

AsyncComplaintNode = Callable[[RefundState], Awaitable[dict[str, Any]]]


def build_formal_complaint_classifier_node(
    classifier: Any,
) -> AsyncComplaintNode:
    """Build the structured formal-complaint classification node."""

    async def classify_formal_complaint(
        state: RefundState,
    ) -> dict[str, Any]:
        latest_user_message = latest_text_user_message(state)
        if latest_user_message is None:
            return {
                "staff_complaint_severity": None,
                "explicit_other_complaint": False,
                "formal_complaint_reason": ("No text user message was available."),
            }

        result = await classifier.ainvoke(
            [
                SystemMessage(content=FORMAL_COMPLAINT_CLASSIFIER_SYSTEM_PROMPT),
                *state.get("messages", []),
            ]
        )
        if not isinstance(result, FormalComplaintDetection):
            raise TypeError("Formal complaint classifier returned an unexpected result")

        return {
            "staff_complaint_severity": (
                result.staff_complaint_severity
                if result.complaint_kind == "staff_conduct"
                else None
            ),
            "explicit_other_complaint": (result.complaint_kind == "other_formal"),
            "formal_complaint_reason": result.reason,
        }

    return classify_formal_complaint


async def handle_complaint(state: RefundState) -> dict[str, Any]:
    """Generate a concise complaint response without business operations."""
    latest_user_message = latest_text_user_message(state)
    if latest_user_message is None:
        return {"messages": [AIMessage(content="Please enter your complaint as text.")]}

    response = await models.get_llm().ainvoke(
        [
            SystemMessage(content=COMPLAINT_SYSTEM_PROMPT),
            latest_user_message,
        ]
    )
    return {"messages": [response]}
