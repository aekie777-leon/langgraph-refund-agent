"""Refund-policy, approval, creation, and cancellation nodes."""

import os
from typing import Any, Literal

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, interrupt

from agent.auth.context import require_scope
from agent.refunds.runtime import get_refund_service
from agent.state import RefundState
from agent.tools.check_refund_policy import check_refund_policy


async def check_refund_eligibility_node(
    state: RefundState,
) -> dict[str, Any]:
    """Apply the deterministic refund policy to the current order."""
    result = await check_refund_policy.ainvoke({"order_info": state["order_info"]})
    eligible = result["eligible"]
    requires_manual_review = result["requires_manual_review"]
    reason = result["reason"]

    if not eligible:
        return {
            "eligible": False,
            "requires_manual_review": requires_manual_review,
            "reason": reason,
            "messages": [AIMessage(content=f"Sorry, {reason}")],
        }

    if requires_manual_review:
        support_contact = os.getenv(
            "CUSTOMER_SERVICE_CONTACT", "your customer service team"
        )
        return {
            "eligible": True,
            "requires_manual_review": True,
            "reason": reason,
            "messages": [
                AIMessage(content=f"{reason} Please contact {support_contact}.")
            ],
        }

    return {
        "eligible": True,
        "reason": reason,
        "requires_manual_review": False,
        "order_id": result["order_id"],
    }


async def create_refund_node(
    state: RefundState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Create the approved refund request in PostgreSQL."""
    order_id = state.get("order_id")
    if not isinstance(order_id, str):
        return {
            "success": False,
            "messages": [
                AIMessage(
                    content=(
                        "The refund request could not be created automatically. "
                        "Please contact customer service."
                    )
                )
            ],
        }
    created = await get_refund_service().create_refund(
        require_scope(config),
        order_id=order_id,
    )
    if not created:
        return {
            "success": False,
            "messages": [
                AIMessage(
                    content=(
                        "The refund request could not be created automatically. "
                        "Please contact customer service."
                    )
                )
            ],
        }

    return {
        "success": True,
        "messages": [
            AIMessage(
                content=(
                    "The refund request has been submitted successfully. "
                    "A result is expected within seven business days."
                )
            )
        ],
    }


def approval_node(_state: RefundState) -> Command[Literal["proceed", "cancel"]]:
    """Interrupt execution until the user confirms or cancels the refund."""
    decision = interrupt(
        {
            "question": (
                "Are you sure you want a refund? Refund requests cannot be "
                "cancelled after they are issued."
            )
        }
    )
    return Command(goto="proceed" if decision is True else "cancel")


def cancelled_node(_state: RefundState) -> dict[str, Any]:
    """Return confirmation that the user cancelled the refund flow."""
    return {
        "messages": [
            AIMessage(
                content="The refund has been cancelled. Thank you for your support."
            )
        ]
    }
