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

from agent.tools import check_refund_policy, create_refund_request, search_order

load_dotenv()

ORDER_ID_PATTERN = re.compile(r"ORD-\d{5}")
PREVIOUS_ORDER_REFERENCE_PATTERNS = (
    re.compile(r"\b(?:this|that|the same|previous|last)\s+order\b", re.IGNORECASE),
    re.compile(r"\b(?:refund|return)\s+(?:it|this|that)\b", re.IGNORECASE),
    re.compile(r"(?:这个|那个|这笔|那笔|刚才的|上一个|上一笔|同一个|同一笔)订单"),
)

Intent = Literal["refund_request", "order_inquiry", "complaint"]

COMPLAINT_SYSTEM_PROMPT = """
You are a professional customer service assistant.

The user is expressing dissatisfaction with a product, delivery, or service.

Your task is to:
- Acknowledge the user's specific concern.
- Respond with brief and natural empathy.
- Maintain a calm and professional tone.
- If appropriate, guide the user toward a reasonable next step.

Rules:
- Do not invent order information, delivery status, refund status, or other facts.
- Do not promise refunds, compensation, discounts, or outcomes that have not been confirmed.
- Do not claim that an action has been completed unless it actually has been completed.
- Do not repeatedly apologize or use overly scripted customer-service language.
- Keep the response concise.
"""


class Route(BaseModel):
    """Represent the intent selected by the routing model."""

    step: Intent = Field(
        description="The next step in the routing process"
    )


class RefundState(MessagesState, total=False):
    """State shared by the refund workflow nodes."""

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


class OrderDetection(BaseModel):
    """Represent an order-number detection result."""

    has_order_id: bool = Field(
        description="Whether the user input contains a complete order number"
    )
    order_id: str | None = Field(
        default=None,
        description="The complete order number when one is present",
    )


def _get_llm() -> ChatOpenAI:
    """Create the configured OpenAI-compatible chat model."""
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

    return ChatOpenAI(**client_options)


def _get_order_detector() -> Any:
    """Create the structured-output model used to detect order numbers."""
    return _get_llm().with_structured_output(OrderDetection)


def _router() -> Any:
    """Create the structured-output model used for intent routing."""
    return _get_llm().with_structured_output(Route)


def _references_previous_order(text: str) -> bool:
    """Return whether the user explicitly refers to the previous valid order."""
    return any(pattern.search(text) for pattern in PREVIOUS_ORDER_REFERENCE_PATTERNS)


def _new_turn_state(decision: Intent | None) -> dict[str, Any]:
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


def create_graph():
    """Build and compile the refund workflow."""
    order_detector = _get_order_detector()

    router = _router()

    async def llm_call_router(state: RefundState) -> dict[str, Any]:
        """Route the input to the appropriate node."""
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
                **_new_turn_state(None),
                "messages": [AIMessage(content="Please enter your question.")],
            }
        decision = await router.ainvoke(
            [
                SystemMessage(
                    content="Route the input to refund_request, order_inquiry, or complaint based on the user's request."
                ),
                latest_user_message,
            ]
        )
        if not isinstance(decision, Route):
            raise TypeError("Router returned an unexpected result")

        return _new_turn_state(decision.step)

    def route_by_intent(state: RefundState) -> str:
        """Route the workflow based on the classified user intent."""
        decision = state.get("decision")

        if decision is None:
            return "END"

        if decision == "refund_request":
            return "order_query"
        elif decision == "order_inquiry":
            return "order_query"
        elif decision == "complaint":
            return "complaint"

        raise ValueError(f"Unexpected intent decision: {decision}")

    async def handle_complaint(state: RefundState) -> dict[str, Any]:
        """Handle general customer complaints and dissatisfaction.

        This node generates a concise customer-service response that:
        1. Acknowledges the user's specific concern.
        2. Responds with appropriate empathy and professionalism.
        3. Guides the user toward a reasonable next step when needed.

        This node does not query orders, issue refunds, or perform other
        business operations.
        """
        llm = _get_llm()

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
                "messages": [AIMessage(content="Please enter your complaint as text.")]
            }

        response = await llm.ainvoke(
            [
                SystemMessage(content=COMPLAINT_SYSTEM_PROMPT),
                latest_user_message,
            ]
        )

        return {"messages": [response]}

    def route_after_order_lookup(state: RefundState) -> str:
        if not state.get("search_success"):
            return "END"

        if state["decision"] == "order_inquiry":
            return "order_response"

        if state["decision"] == "refund_request":
            return "check_refund_eligibility"

        raise ValueError(
            f"Unexpected decision after order lookup: {state.get('decision')}"
        )

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

        previous_order_id = state.get("last_order_id")

        if not response.has_order_id or order_id is None:
            if previous_order_id is not None and _references_previous_order(
                latest_user_message.content
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

        return {
            "search_success": True,
            "order_info": response["order"],
            "last_order_id": state["order_id"],
        }

    async def order_response(state: RefundState) -> dict[str, Any]:
        order = state["order_info"]
        message = (
            f"Order ID: {order['order_id']}\n"
            f"Status: {order['status']}\n"
            f"Product name: {order['product_name']}"
        )

        return {"messages": [AIMessage(content=message)]}

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
    workflow.add_node("llm_call_router", llm_call_router)
    workflow.add_node("detect_order", detect_order)
    workflow.add_node("search_node", search_node)
    workflow.add_node("check_refund_eligibility", check_node)
    workflow.add_node("order_response", order_response)
    workflow.add_node("approval_node", approval_node)
    workflow.add_node("proceed", create_node)
    workflow.add_node("cancel", cancelled_node)
    workflow.add_node("handle_complaint", handle_complaint)
    workflow.add_edge(START, "llm_call_router")
    workflow.add_conditional_edges(
        "llm_call_router",
        route_by_intent,
        {"order_query": "detect_order", "complaint": "handle_complaint", "END": END},
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
    workflow.add_edge("order_response", END)
    workflow.add_edge("proceed", END)
    workflow.add_edge("cancel", END)

    return workflow.compile()
