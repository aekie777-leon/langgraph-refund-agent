"""Order-detection, lookup, and response node implementations."""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from langchain_core.messages import AIMessage, SystemMessage

from agent.order_context import ORDER_ID_PATTERN, references_previous_order
from agent.prompts import ORDER_DETECTION_SYSTEM_PROMPT
from agent.state import RefundState, latest_text_user_message
from agent.tools.search_order import search_order

AsyncNode = Callable[[RefundState], Awaitable[dict[str, Any]]]


def build_order_detection_node(order_detector: Any) -> AsyncNode:
    """Build an order-number detection node using the supplied model."""

    async def detect_order(state: RefundState) -> dict[str, Any]:
        latest_user_message = latest_text_user_message(state)
        if latest_user_message is None:
            return {
                "order_id": None,
                "order_id_valid": False,
                "messages": [AIMessage(content="Please enter your order number.")],
            }

        message_text = cast(str, latest_user_message.content)
        response = await order_detector.ainvoke(
            [
                SystemMessage(content=ORDER_DETECTION_SYSTEM_PROMPT),
                latest_user_message,
            ]
        )
        order_id = response.order_id.strip().upper() if response.order_id else None
        previous_order_id = state.get("last_order_id")

        if not response.has_order_id or order_id is None:
            if previous_order_id is not None and references_previous_order(
                message_text
            ):
                return {
                    "order_id": previous_order_id,
                    "order_id_valid": True,
                }

            return {
                "order_id": None,
                "last_order_id": None,
                "order_id_valid": False,
                "messages": [AIMessage(content="Please enter your order number.")],
            }

        if ORDER_ID_PATTERN.fullmatch(order_id) is None:
            return {
                "order_id": None,
                "last_order_id": None,
                "order_id_valid": False,
                "messages": [AIMessage(content="Please enter a valid order number.")],
            }

        return {
            "order_id": order_id,
            "last_order_id": None,
            "order_id_valid": True,
        }

    return detect_order


async def search_order_node(state: RefundState) -> dict[str, Any]:
    """Look up the current order and retain it for explicit later reference."""
    response = await search_order.ainvoke({"order_id": state["order_id"]})
    if not response["success"]:
        return {
            "search_success": False,
            "messages": [
                AIMessage(
                    content="Order not found. Please enter the correct order number."
                )
            ],
        }

    return {
        "search_success": True,
        "order_info": response["order"],
        "last_order_id": state["order_id"],
    }


async def order_response_node(state: RefundState) -> dict[str, Any]:
    """Return the public order fields used by the inquiry flow."""
    order = state["order_info"]
    message = (
        f"Order ID: {order['order_id']}\n"
        f"Status: {order['status']}\n"
        f"Product name: {order['product_name']}"
    )
    return {"messages": [AIMessage(content=message)]}
