"""Unit tests for the refund tools."""

import datetime as dt
import importlib
import uuid
from pathlib import Path

import pytest

from agent.tools import check_refund_policy, search_order


def _order(**overrides):
    order = {
        "order_id": "ORD-10001",
        "delivery_date": (dt.date.today() - dt.timedelta(days=2)).isoformat(),
        "status": "delivered",
        "refunded": False,
        "amount": 69.99,
    }
    order.update(overrides)
    return order


def test_search_order_is_independent_of_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = search_order.invoke({"order_id": "ORD-10001"})

    assert result["success"] is True
    assert result["order"]["order_id"] == "ORD-10001"
    assert "delivery_days_ago" not in result["order"]


def test_search_order_returns_not_found() -> None:
    result = search_order.invoke({"order_id": "ORD-99999"})

    assert result == {
        "success": False,
        "error": "Order not found.",
        "order_id": "ORD-99999",
    }


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        (
            {"delivery_date": (dt.date.today() + dt.timedelta(days=1)).isoformat()},
            "The delivery date is invalid.",
        ),
        (
            {"delivery_date": (dt.date.today() - dt.timedelta(days=8)).isoformat()},
            "This order is past the refund deadline.",
        ),
        ({"status": "shipped"}, "This order is not in a refundable status."),
        ({"refunded": True}, "This order has already been refunded."),
    ],
)
def test_policy_rejects_ineligible_orders(overrides, expected_reason) -> None:
    result = check_refund_policy.invoke({"order_info": _order(**overrides)})

    assert result["eligible"] is False
    assert result["reason"] == expected_reason


def test_policy_routes_large_refund_to_manual_review() -> None:
    result = check_refund_policy.invoke({"order_info": _order(amount=100)})

    assert result["eligible"] is True
    assert result["requires_manual_review"] is True


def test_policy_accepts_order_at_seven_day_boundary() -> None:
    result = check_refund_policy.invoke(
        {
            "order_info": _order(
                delivery_date=(dt.date.today() - dt.timedelta(days=7)).isoformat()
            )
        }
    )

    assert result["eligible"] is True
    assert result["order_id"] == "ORD-10001"


def test_policy_handles_invalid_data() -> None:
    result = check_refund_policy.invoke(
        {"order_info": _order(delivery_date="not-a-date")}
    )

    assert result["eligible"] is False
    assert result["reason"] == "The order data is incomplete or invalid."


def test_create_refund_request_success(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("agent.tools.create_refund_request")
    monkeypatch.setattr(module, "_save_refund_request", lambda _data: True)

    result = module.create_refund_request.invoke({"order_id": "ORD-10001"})

    assert result["success"] is True
    assert result["status"] == "pending"
    uuid.UUID(result["refund_id"])
    assert result["created_at"].tzinfo is not None


def test_create_refund_request_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("agent.tools.create_refund_request")
    existing_id = uuid.uuid4()
    monkeypatch.setattr(module, "_save_refund_request", lambda _data: False)
    monkeypatch.setattr(
        module,
        "_existing_request",
        lambda _order_id: (existing_id, "pending"),
    )

    result = module.create_refund_request.invoke({"order_id": "ORD-10001"})

    assert result["success"] is False
    assert result["status"] == "already_exists"
    assert result["refund_id"] == str(existing_id)


def test_database_configuration_reports_missing_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("agent.tools.create_refund_request")
    for name in (
        "POSTGRES_URI",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_DB",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="POSTGRES_DB"):
        module._database_uri()
