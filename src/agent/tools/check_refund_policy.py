"""Refund eligibility policy tool."""

import datetime as dt
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class RefundArgs(BaseModel):
    """Validate the refund-policy tool input."""

    order_info: dict[str, Any] = Field(description="Full order details")


class OrderInfo(BaseModel):
    """Represent the order fields required by the refund policy."""

    model_config = ConfigDict(extra="ignore")

    order_id: str = Field(pattern=r"^ORD-\d{5}$")
    delivery_date: dt.date
    status: str
    refunded: bool
    amount: float = Field(ge=0, allow_inf_nan=False)


def _result(
    *,
    eligible: bool,
    reason: str,
    requires_manual_review: bool = False,
    order_id: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "eligible": eligible,
        "reason": reason,
        "requires_manual_review": requires_manual_review,
    }
    if order_id is not None:
        result["order_id"] = order_id
    return result


@tool(args_schema=RefundArgs)
def check_refund_policy(order_info: dict[str, Any]) -> dict[str, Any]:
    """Determine refund eligibility from validated order information."""
    try:
        order = OrderInfo.model_validate(order_info)
    except ValidationError:
        return _result(
            eligible=False,
            reason="The order data is incomplete or invalid.",
        )

    duration = dt.date.today() - order.delivery_date
    if duration < dt.timedelta(0):
        return _result(
            eligible=False,
            reason="The delivery date is invalid.",
        )
    if duration > dt.timedelta(days=7):
        return _result(
            eligible=False,
            reason="This order is past the refund deadline.",
        )
    if order.status != "delivered":
        return _result(
            eligible=False,
            reason="This order is not in a refundable status.",
        )
    if order.refunded:
        return _result(
            eligible=False,
            reason="This order has already been refunded.",
        )
    if order.amount >= 100:
        return _result(
            eligible=True,
            reason=(
                "This order is eligible for a refund, but the amount exceeds "
                "the automatic processing limit."
            ),
            requires_manual_review=True,
        )

    return _result(
        eligible=True,
        reason="The order is eligible for a refund.",
        order_id=order.order_id,
    )
