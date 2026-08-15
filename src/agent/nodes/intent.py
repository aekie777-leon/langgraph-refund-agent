"""Intent-classification node implementation."""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage

from agent.prompts import INTENT_ROUTER_SYSTEM_PROMPT
from agent.schemas import Route
from agent.state import RefundState, latest_text_user_message, new_turn_state

AsyncNode = Callable[[RefundState], Awaitable[dict[str, Any]]]


def build_intent_router_node(router: Any) -> AsyncNode:
    """Build an intent-routing node using the supplied structured model."""

    async def llm_call_router(state: RefundState) -> dict[str, Any]:
        latest_user_message = latest_text_user_message(state)
        if latest_user_message is None:
            return {
                **new_turn_state(None),
                "messages": [AIMessage(content="Please enter your question.")],
            }

        decision = await router.ainvoke(
            [
                SystemMessage(content=INTENT_ROUTER_SYSTEM_PROMPT),
                latest_user_message,
            ]
        )
        if not isinstance(decision, Route):
            raise TypeError("Router returned an unexpected result")

        return {
            **new_turn_state(decision.step),
            "human_handoff_requested": decision.human_handoff_requested,
        }

    return llm_call_router
