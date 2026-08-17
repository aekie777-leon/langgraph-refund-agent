"""Unit tests for deterministic order-operation and delivery policy."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agent.operations.models import (
    DeliveryIssueRequest,
    ExistingOperation,
    OrderOperationRequest,
    OrderSnapshot,
)
from agent.operations.policy import (
    RETURN_WINDOW_DAYS,
    TRACKING_STALLED_HOURS,
    CurrencyThresholds,
    evaluate_delivery_issue,
    evaluate_operation,
)

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
THRESHOLDS = CurrencyThresholds({"USD": Decimal("100.00")})


def _snapshot(**overrides: object) -> OrderSnapshot:
    values: dict[str, object] = {
        "order_id": "ORD-10001",
        "version": 1,
        "amount": Decimal("69.99"),
        "currency": "USD",
        "order_status": "confirmed",
        "payment_status": "paid",
        "fulfillment_status": "unfulfilled",
        "created_at": NOW - timedelta(days=10),
        "return_eligible": True,
        "exchange_eligible": True,
    }
    values.update(overrides)
    return OrderSnapshot.model_validate(values)


def _operation(
    operation_type: str,
    *,
    reason: str | None = None,
) -> OrderOperationRequest:
    reasons = {
        "cancellation": "no_longer_needed",
        "return": "changed_mind",
        "exchange": "size_or_variant_issue",
    }
    values: dict[str, object] = {
        "thread_id": "thread-1",
        "source_message_id": f"message-{operation_type}",
        "order_id": "ORD-10001",
        "operation_type": operation_type,
        "reason": reason or reasons[operation_type],
        "customer_id": "customer-a",
        "tenant_id": "tenant-demo",
    }
    if operation_type == "exchange":
        values["replacement_variant_id"] = "variant-blue"
    return OrderOperationRequest.model_validate(values)


def _delivery(
    issue_type: str,
    *,
    investigation_requested: bool = False,
) -> DeliveryIssueRequest:
    return DeliveryIssueRequest.model_validate(
        {
            "thread_id": "thread-1",
            "source_message_id": f"message-{issue_type}",
            "order_id": "ORD-10001",
            "issue_type": issue_type,
            "investigation_requested": investigation_requested,
        }
    )


def test_unfulfilled_confirmed_order_can_be_cancelled_after_confirmation() -> None:
    result = evaluate_operation(
        _operation("cancellation"),
        _snapshot(),
        now=NOW,
        thresholds=THRESHOLDS,
    )

    assert result.outcome == "eligible"
    assert result.requires_confirmation is True


def test_processing_cancellation_requires_p1_manual_review_and_confirmation() -> None:
    result = evaluate_operation(
        _operation("cancellation"),
        _snapshot(fulfillment_status="processing"),
        now=NOW,
        thresholds=THRESHOLDS,
    )

    assert result.outcome == "manual_review"
    assert result.requires_confirmation is True
    assert result.case_recommendation is not None
    assert result.case_recommendation.priority == "p1"
    assert result.reason_codes == ("cancellation_fulfillment_processing",)


@pytest.mark.parametrize("fulfillment_status", ["shipped", "delivered"])
def test_fulfilled_order_cancellation_is_rejected(fulfillment_status: str) -> None:
    values: dict[str, object] = {"fulfillment_status": fulfillment_status}
    if fulfillment_status == "shipped":
        values["shipped_at"] = NOW - timedelta(days=1)
    else:
        values.update(
            {
                "shipped_at": NOW - timedelta(days=2),
                "delivered_at": NOW - timedelta(days=1),
            }
        )
    result = evaluate_operation(
        _operation("cancellation"),
        _snapshot(**values),
        now=NOW,
        thresholds=THRESHOLDS,
    )

    assert result.outcome == "rejected"
    assert result.reason_codes == ("cancellation_fulfillment_started",)


def test_cancelled_order_is_an_idempotent_completed_result() -> None:
    result = evaluate_operation(
        _operation("cancellation"),
        _snapshot(order_status="cancelled"),
        now=NOW,
        thresholds=THRESHOLDS,
    )

    assert result.outcome == "already_completed"
    assert result.requires_confirmation is False


def test_contradictory_lifecycle_routes_to_manual_review() -> None:
    result = evaluate_operation(
        _operation("cancellation"),
        _snapshot(
            order_status="cancelled",
            fulfillment_status="shipped",
            shipped_at=NOW - timedelta(days=1),
        ),
        now=NOW,
        thresholds=THRESHOLDS,
    )

    assert result.outcome == "manual_review"
    assert result.reason_codes == ("order_state_invalid",)


def test_return_is_eligible_at_the_exact_seven_day_deadline() -> None:
    delivered_at = NOW - timedelta(days=RETURN_WINDOW_DAYS)
    result = evaluate_operation(
        _operation("return"),
        _snapshot(
            fulfillment_status="delivered",
            shipped_at=delivered_at - timedelta(days=1),
            delivered_at=delivered_at,
        ),
        now=NOW,
        thresholds=THRESHOLDS,
    )

    assert result.outcome == "eligible"
    assert result.requires_confirmation is True


def test_return_is_rejected_after_the_seven_day_deadline() -> None:
    delivered_at = NOW - timedelta(days=RETURN_WINDOW_DAYS, microseconds=1)
    result = evaluate_operation(
        _operation("return"),
        _snapshot(
            fulfillment_status="delivered",
            shipped_at=delivered_at - timedelta(days=1),
            delivered_at=delivered_at,
        ),
        now=NOW,
        thresholds=THRESHOLDS,
    )

    assert result.outcome == "rejected"
    assert result.reason_codes == ("operation_return_window_expired",)


def test_usd_manual_review_threshold_is_inclusive() -> None:
    result = evaluate_operation(
        _operation("return"),
        _snapshot(
            amount=Decimal("100.00"),
            fulfillment_status="delivered",
            shipped_at=NOW - timedelta(days=2),
            delivered_at=NOW - timedelta(days=1),
        ),
        now=NOW,
        thresholds=THRESHOLDS,
    )

    assert result.outcome == "manual_review"
    assert result.case_recommendation is not None
    assert result.case_recommendation.priority == "p1"


def test_unconfigured_currency_requires_manual_review() -> None:
    result = evaluate_operation(
        _operation("return"),
        _snapshot(
            currency="EUR",
            fulfillment_status="delivered",
            shipped_at=NOW - timedelta(days=2),
            delivered_at=NOW - timedelta(days=1),
        ),
        now=NOW,
        thresholds=THRESHOLDS,
    )

    assert result.outcome == "manual_review"
    assert result.reason_codes == ("currency_threshold_unconfigured",)


def test_existing_refund_blocks_a_return_operation() -> None:
    existing = ExistingOperation(
        operation_id="refund-1",
        operation_type="refund",
        status="processing",
    )
    result = evaluate_operation(
        _operation("return"),
        _snapshot(
            fulfillment_status="delivered",
            shipped_at=NOW - timedelta(days=2),
            delivered_at=NOW - timedelta(days=1),
            existing_operations=(existing,),
        ),
        now=NOW,
        thresholds=THRESHOLDS,
    )

    assert result.outcome == "existing_operation"
    assert result.existing_operation == existing


@pytest.mark.parametrize(
    ("availability", "outcome", "priority"),
    [
        (True, "eligible", None),
        (None, "manual_review", "p2"),
        (False, "rejected", None),
    ],
)
def test_exchange_uses_replacement_availability(
    availability: bool | None,
    outcome: str,
    priority: str | None,
) -> None:
    result = evaluate_operation(
        _operation("exchange"),
        _snapshot(
            fulfillment_status="delivered",
            shipped_at=NOW - timedelta(days=2),
            delivered_at=NOW - timedelta(days=1),
        ),
        now=NOW,
        thresholds=THRESHOLDS,
        replacement_available=availability,
    )

    assert result.outcome == outcome
    assert (
        result.case_recommendation.priority if result.case_recommendation else None
    ) == priority


def test_tracking_stalled_at_the_72_hour_boundary_creates_p1_case() -> None:
    result = evaluate_delivery_issue(
        _delivery("tracking_stalled"),
        _snapshot(
            fulfillment_status="shipped",
            shipped_at=NOW - timedelta(days=4),
            last_tracking_event_at=NOW - timedelta(hours=TRACKING_STALLED_HOURS),
        ),
        now=NOW,
    )

    assert result.outcome == "manual_review"
    assert result.case_recommendation is not None
    assert result.case_recommendation.priority == "p1"


def test_tracking_is_not_stalled_before_the_72_hour_boundary() -> None:
    result = evaluate_delivery_issue(
        _delivery("tracking_stalled"),
        _snapshot(
            fulfillment_status="shipped",
            shipped_at=NOW - timedelta(days=4),
            last_tracking_event_at=NOW - timedelta(
                hours=TRACKING_STALLED_HOURS,
                minutes=-1,
            ),
        ),
        now=NOW,
    )

    assert result.outcome == "self_service"
    assert result.reason_codes == ("delivery_tracking_recent",)


def test_delivered_not_received_is_a_p1_investigation_not_verified_fault() -> None:
    result = evaluate_delivery_issue(
        _delivery("marked_delivered_not_received"),
        _snapshot(
            fulfillment_status="delivered",
            shipped_at=NOW - timedelta(days=2),
            delivered_at=NOW - timedelta(days=1),
        ),
        now=NOW,
    )

    assert result.outcome == "manual_review"
    assert result.case_recommendation is not None
    assert result.case_recommendation.priority == "p1"
    assert "fault" not in result.display_reason.lower()


def test_delivery_policy_rejects_a_naive_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_delivery_issue(
            _delivery("delivery_failed"),
            _snapshot(),
            now=datetime(2026, 8, 17, 12, 0),
        )
