"""Human-support confirmation nodes."""

from typing import Any, Literal, cast

from langchain_core.messages import AIMessage
from langgraph.types import Command, interrupt

from agent.routing import route_after_human_handoff_declined
from agent.state import RefundState

HandoffDestination = Literal[
    "acknowledge_human_handoff",
    "confirm_order_priority",
    "detect_order",
    "handle_noncritical_risk",
    "handle_complaint",
]


def confirm_human_handoff(
    state: RefundState,
) -> Command[HandoffDestination]:
    """Confirm an explicit request for human customer support."""
    choice = interrupt(
        {
            "type": "human_handoff_confirmation",
            "message": (
                "You asked to speak with a human customer-service representative."
            ),
            "question": (
                "Would you like me to create a support request for human assistance?"
            ),
            "options": [
                {
                    "value": "confirm_handoff",
                    "label": "Yes, request human support",
                },
                {
                    "value": "continue_self_service",
                    "label": "No, continue here",
                },
            ],
        }
    )

    if choice == "confirm_handoff":
        return Command(
            goto="acknowledge_human_handoff",
            update={"human_handoff_confirmed": True},
        )
    if choice == "continue_self_service":
        destination = cast(
            HandoffDestination,
            route_after_human_handoff_declined(state),
        )
        return Command(
            goto=destination,
            update={
                "human_handoff_requested": False,
                "human_handoff_confirmed": False,
            },
        )

    raise ValueError(f"Unexpected human-handoff confirmation choice: {choice!r}")


def acknowledge_human_handoff(
    _state: RefundState,
) -> dict[str, Any]:
    """Acknowledge confirmation without claiming persistence succeeded."""
    return {
        "messages": [
            AIMessage(
                content=("Understood. I’ll route this conversation for human support.")
            )
        ]
    }
