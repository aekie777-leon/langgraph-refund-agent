"""Consistency checks between the two demonstration order data sources."""

import json
from pathlib import Path

from agent.operations.demo_provider import DemoOrderProvider

ORDERS_FILE = (
    Path(__file__).resolve().parents[2] / "src" / "agent" / "data" / "orders.json"
)


def _json_orders() -> dict[str, dict[str, object]]:
    with ORDERS_FILE.open(encoding="utf-8") as file:
        return json.load(file)


def _demo_orders() -> dict[str, tuple[str | None, str | None]]:
    provider = DemoOrderProvider()
    return {
        order_id: (snapshot.customer_id, snapshot.tenant_id)
        for order_id, snapshot in provider._orders.items()
    }


def test_all_json_orders_have_ownership() -> None:
    for order_id, entry in _json_orders().items():
        assert entry.get("owner_customer_id"), f"{order_id} lacks owner_customer_id"
        assert entry.get("tenant_id"), f"{order_id} lacks tenant_id"


def test_all_demo_orders_have_ownership() -> None:
    for order_id, (customer_id, tenant_id) in _demo_orders().items():
        assert customer_id, f"{order_id} lacks customer_id"
        assert tenant_id, f"{order_id} lacks tenant_id"


def test_shared_orders_have_identical_ownership() -> None:
    json_orders = _json_orders()
    demo_orders = _demo_orders()
    shared = set(json_orders) & set(demo_orders)

    assert shared, "expected at least one shared order between the data sources"
    for order_id in shared:
        json_ownership = (
            json_orders[order_id]["owner_customer_id"],
            json_orders[order_id]["tenant_id"],
        )
        assert json_ownership == demo_orders[order_id], order_id


def test_spot_check_first_order_ownership() -> None:
    json_orders = _json_orders()
    demo_orders = _demo_orders()

    assert (
        json_orders["ORD-10001"]["owner_customer_id"],
        json_orders["ORD-10001"]["tenant_id"],
    ) == ("customer-a", "tenant-demo")
    assert demo_orders["ORD-10001"] == ("customer-a", "tenant-demo")
    assert json_orders["ORD-20001"]["owner_customer_id"] == "customer-b"
    assert json_orders["ORD-30001"]["owner_customer_id"] == "customer-c"
    assert json_orders["ORD-30001"]["tenant_id"] == "tenant-other"
