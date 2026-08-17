"""Order lookup tool backed by bundled demonstration data."""

import datetime as dt
import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

ORDERS_FILE = Path(__file__).resolve().parent.parent / "data" / "orders.json"


class SearchArgs(BaseModel):
    """Validate order lookup input including the caller's ownership scope."""

    order_id: str = Field(pattern=r"^ORD-\d{5}$", description="Order number")
    customer_id: str = Field(min_length=1, description="Authenticated customer identifier")
    tenant_id: str = Field(min_length=1, description="Authenticated tenant identifier")


@tool(args_schema=SearchArgs)
def search_order(order_id: str, customer_id: str, tenant_id: str) -> dict[str, Any]:
    """Retrieve demonstration order information owned by the caller."""
    with ORDERS_FILE.open(encoding="utf-8") as file:
        database = json.load(file)

    order_info = database.get(order_id)
    if order_info is None:
        return {
            "success": False,
            "error": "Order not found.",
            "order_id": order_id,
        }

    if (
        order_info.get("owner_customer_id") != customer_id
        or order_info.get("tenant_id") != tenant_id
    ):
        return {
            "success": False,
            "error": "Order not found.",
            "order_id": order_id,
        }

    order_info = dict(order_info)
    order_info.pop("owner_customer_id", None)
    order_info.pop("tenant_id", None)
    if "delivery_days_ago" in order_info:
        days_ago = int(order_info.pop("delivery_days_ago"))
        order_info["delivery_date"] = (
            dt.date.today() - dt.timedelta(days=days_ago)
        ).isoformat()

    return {"success": True, "order": order_info}
