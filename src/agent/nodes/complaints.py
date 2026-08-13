"""Complaint-response node implementation."""

from typing import Any

from langchain_core.messages import AIMessage, SystemMessage

from agent import models
from agent.prompts import COMPLAINT_SYSTEM_PROMPT
from agent.state import RefundState, latest_text_user_message


async def handle_complaint(state: RefundState) -> dict[str, Any]:
    """Generate a concise complaint response without business operations."""
    latest_user_message = latest_text_user_message(state)
    if latest_user_message is None:
        return {
            "messages": [AIMessage(content="Please enter your complaint as text.")]
        }

    response = await models.get_llm().ainvoke(
        [
            SystemMessage(content=COMPLAINT_SYSTEM_PROMPT),
            latest_user_message,
        ]
    )
    return {"messages": [response]}
