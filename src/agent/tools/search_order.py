"""Order lookup tool backed by bundled demonstration data."""

import datetime as dt
import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

ORDERS_FILE = Path(__file__).resolve().parent.parent / "data" / "orders.json"


class SearchArgs(BaseModel):
    """Validate order lookup input."""

    order_id: str = Field(pattern=r"^ORD-\d{5}$", description="Order number")


@tool(args_schema=SearchArgs)
def search_order(order_id: str) -> dict[str, Any]:
    """Retrieve demonstration order information by order number."""
    with ORDERS_FILE.open(encoding="utf-8") as file:
        database = json.load(file)

    order_info = database.get(order_id)
    if order_info is None:
        return {
            "success": False,
            "error": "Order not found.",
            "order_id": order_id,
        }

    if "delivery_days_ago" in order_info:
        days_ago = int(order_info.pop("delivery_days_ago"))
        order_info["delivery_date"] = (
            dt.date.today() - dt.timedelta(days=days_ago)
        ).isoformat()

    return {"success": True, "order": order_info}
