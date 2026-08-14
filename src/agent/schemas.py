"""Structured model outputs used by the customer-service workflow."""

from typing import Literal

from pydantic import BaseModel, Field

Intent = Literal["refund_request", "order_inquiry", "complaint"]
SemanticRiskLevel = Literal["none", "low", "medium", "high", "critical"]
SemanticRiskCategory = Literal[
    "self_harm",
    "violence",
    "legal",
    "regulatory",
    "reputation",
    "other",
]


class Route(BaseModel):
    """Represent the intent selected by the routing model."""

    step: Intent = Field(description="The next step in the routing process")


class OrderDetection(BaseModel):
    """Represent an order-number detection result."""

    has_order_id: bool = Field(
        description="Whether the user input contains a complete order number"
    )
    order_id: str | None = Field(
        default=None,
        description="The complete order number when one is present",
    )


class SemanticRiskDetection(BaseModel):
    """Represent the language model's contextual risk assessment."""

    risk_level: SemanticRiskLevel = Field(
        description=(
            "The highest semantic risk level supported by the user's message "
            "and conversation context. Use 'none' only when no genuine risk "
            "is present."
        )
    )
    categories: list[SemanticRiskCategory] = Field(
        description=(
            "The applicable risk categories. Use 'other' when a genuine risk "
            "does not fit any named category. Return an empty list only when "
            "risk_level is 'none'."
        )
    )
    reason: str = Field(
        description=(
            "A concise explanation grounded in the user's message, conversation "
            "context, and any rule-based signals."
        )
    )
