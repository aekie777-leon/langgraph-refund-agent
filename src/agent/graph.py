"""LangGraph workflow for the refund assistant."""

import os
import re
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from .tools import check_refund_policy, create_refund_request, search_order

load_dotenv()

ORDER_ID_PATTERN = re.compile(r"ORD-\d{5}")


class RefundState(MessagesState, total=False):
    """State shared by the refund workflow nodes."""

    order_id: str | None
    order_info: dict[str, Any]
    success: bool
    search_success: bool
    eligible: bool
    requires_manual_review: bool
    order_id_valid: bool
    reason: str


class OrderDetection(BaseModel):
    """Represent an order-number detection result."""

    has_order_id: bool = Field(
        description="Whether the user input contains a complete order number"
    )
    order_id: str | None = Field(
        default=None,
        description="The complete order number when one is present",
    )


def _get_order_detector():
    """Create the structured-output model used to detect order numbers."""
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", api_key),
            ("OPENAI_MODEL", model),
        )
        if not value
    ]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Missing required environment variables: {names}")

    client_options: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
    }
    if base_url := os.getenv("OPENAI_BASE_URL"):
        client_options["base_url"] = base_url

    return ChatOpenAI(**client_options).with_structured_output(OrderDetection)


def create_graph():
    """Build and compile the refund workflow."""
    order_detector = _get_order_detector()

    async def detect_order(state: RefundState) -> dict[str, Any]:
        """Extract a complete order number without guessing missing digits."""
        latest_user_message = next(
            (
                message
                for message in reversed(state.get("messages", []))
                if isinstance(message, HumanMessage)
            ),
            None,
        )
        if latest_user_message is None or not isinstance(
            latest_user_message.content, str
        ):
            return {
                "order_id": None,
                "order_id_valid": False,
                "messages": [AIMessage(content="Please enter your order number.")],
            }

        response = await order_detector.ainvoke(
            [
                SystemMessage(
                    content=(
                        "Detect whether the user supplied a complete order number. "
                        "Valid order numbers use the format ORD-12345. Never guess or "
                        "complete a partial number."
                    )
                ),
                latest_user_message,
            ]
        )
        order_id = response.order_id.strip().upper() if response.order_id else None

        if not response.has_order_id or order_id is None:
            return {
                "order_id": None,
                "order_id_valid": False,
                "messages": [AIMessage(content="Please enter your order number.")],
            }
        if ORDER_ID_PATTERN.fullmatch(order_id) is None:
            return {
                "order_id": None,
                "order_id_valid": False,
                "messages": [AIMessage(content="Please enter a valid order number.")],
            }

        return {"order_id": order_id, "order_id_valid": True}

    def route_after_detection(state: RefundState) -> str:
        return "search_node" if state.get("order_id") else "END"

    async def search_node(state: RefundState) -> dict[str, Any]:
        response = await search_order.ainvoke({"order_id": state["order_id"]})
        if not response["success"]:
            return {
                "search_success": False,
                "messages": [
                    AIMessage(
                        content=(
                            "Order not found. Please enter the correct order number."
                        )
                    )
                ],
            }

        return {"search_success": True, "order_info": response["order"]}

    def route_after_search(state: RefundState) -> str:
        return "check_node" if state.get("search_success") else "END"

    async def check_node(state: RefundState) -> dict[str, Any]:
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

    def route_after_policy(state: RefundState) -> str:
        if not state.get("eligible") or state.get("requires_manual_review"):
            return "END"
        return "approval_node"

    async def create_node(state: RefundState) -> dict[str, Any]:
        result = await create_refund_request.ainvoke({"order_id": state["order_id"]})
        if not result["success"]:
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
        return {
            "messages": [
                AIMessage(
                    content="The refund has been cancelled. Thank you for your support."
                )
            ]
        }

    workflow = StateGraph(RefundState)
    workflow.add_node("detect_order", detect_order)
    workflow.add_node("search_node", search_node)
    workflow.add_node("check_node", check_node)
    workflow.add_node("approval_node", approval_node)
    workflow.add_node("proceed", create_node)
    workflow.add_node("cancel", cancelled_node)

    workflow.add_edge(START, "detect_order")
    workflow.add_conditional_edges(
        "detect_order",
        route_after_detection,
        {"END": END, "search_node": "search_node"},
    )
    workflow.add_conditional_edges(
        "search_node",
        route_after_search,
        {"END": END, "check_node": "check_node"},
    )
    workflow.add_conditional_edges(
        "check_node",
        route_after_policy,
        {"END": END, "approval_node": "approval_node"},
    )
    workflow.add_edge("proceed", END)
    workflow.add_edge("cancel", END)

    return workflow.compile()
