"""Structured model outputs used by the customer-service workflow."""

from typing import Literal

from pydantic import BaseModel, Field

Intent = Literal["refund_request", "order_inquiry", "complaint"]


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
