"""Apply deterministic policies for order operations and delivery issues."""

from datetime import datetime, timedelta
from decimal import Decimal

from agent.operations.models import (
    CasePriority,
    CaseRecommendation,
    DeliveryDecision,
    DeliveryIssueRequest,
    DeliveryIssueType,
    ExistingOperation,
    OperationDecision,
    OperationType,
    OrderOperationRequest,
    OrderSnapshot,
    is_aware,
)

RETURN_WINDOW_DAYS = 7
TRACKING_STALLED_HOURS = 72
_ACTIVE_OPERATION_STATUSES = frozenset(
    {"queued", "submitted", "processing", "manual_review", "completed"}
)


class CurrencyThresholds:
    """Supply manual-review amounts as Decimal values keyed by ISO currency."""

    def __init__(self, thresholds: dict[str, Decimal]) -> None:
        """Store a copy of configured per-currency thresholds."""
        self._thresholds = dict(thresholds)

    def threshold_for(self, currency: str) -> Decimal | None:
        """Return the configured threshold, if any, for a currency."""
        return self._thresholds.get(currency)


def evaluate_operation(
    request: OrderOperationRequest,
    snapshot: OrderSnapshot,
    *,
    now: datetime,
    thresholds: CurrencyThresholds,
    replacement_available: bool | None = None,
) -> OperationDecision:
    """Return the policy result for one matching order-operation request."""
    _validate_request_context(request, snapshot, now)

    if _has_contradictory_lifecycle(snapshot):
        return _manual(
            request.operation_type,
            "order_state_invalid",
            "Order state needs manual review.",
            "p1",
        )

    existing = _conflicting_operation(snapshot, request.operation_type)
    if existing is not None:
        return _existing(request.operation_type, existing)

    if request.operation_type == "cancellation":
        return _evaluate_cancellation(snapshot)
    if request.operation_type == "return":
        return _evaluate_return_or_exchange(
            operation_type="return",
            snapshot=snapshot,
            now=now,
            thresholds=thresholds,
        )
    return _evaluate_exchange(
        snapshot=snapshot,
        now=now,
        thresholds=thresholds,
        replacement_available=replacement_available,
    )


def evaluate_delivery_issue(
    request: DeliveryIssueRequest,
    snapshot: OrderSnapshot,
    *,
    now: datetime,
) -> DeliveryDecision:
    """Assess a delivery issue without treating an unverified claim as fact."""
    if request.order_id != snapshot.order_id:
        raise ValueError("request and snapshot must have the same order_id")
    _require_aware_datetime(now)

    if _has_contradictory_lifecycle(snapshot):
        return _delivery_manual(
            request.issue_type,
            "delivery_data_invalid",
            "Delivery data needs investigation.",
            "p1",
        )

    if request.issue_type == "tracking_stalled":
        return _evaluate_tracking_stalled(snapshot, now)
    if request.issue_type == "delayed":
        return _evaluate_delayed(snapshot, now, request.investigation_requested)
    if request.issue_type == "delivery_failed":
        return _delivery_manual(
            request.issue_type,
            "delivery_failed",
            "Delivery needs a redelivery or address review.",
            "p2",
        )
    if request.issue_type == "marked_delivered_not_received":
        return _delivery_manual(
            request.issue_type,
            "delivery_marked_received_dispute",
            "Your delivery report needs investigation.",
            "p1",
        )
    if request.issue_type == "package_damaged":
        return _delivery_manual(
            request.issue_type,
            "delivery_damage_claim",
            "Your damage report needs review.",
            "p1",
        )
    if request.issue_type == "wrong_item_or_missing_parts":
        return DeliveryDecision(
            outcome="offer_return_exchange",
            issue_type=request.issue_type,
            reason_codes=("delivery_wrong_item_or_missing_parts_claim",),
            display_reason="We can evaluate a return or exchange request.",
        )
    return _delivery_manual(
        request.issue_type,
        "delivery_other_issue",
        "Your delivery report needs review.",
        "p2",
    )


def _evaluate_cancellation(snapshot: OrderSnapshot) -> OperationDecision:
    if snapshot.order_status == "cancelled":
        return OperationDecision(
            outcome="already_completed",
            operation_type="cancellation",
            reason_codes=("order_already_cancelled",),
            display_reason="This order has already been cancelled.",
        )
    if snapshot.payment_status in ("refund_pending", "refunded"):
        return _rejected(
            "cancellation",
            "cancellation_payment_already_refunded",
            "This order already has a refund in progress or completed.",
        )
    if snapshot.fulfillment_status in ("shipped", "delivered"):
        return _rejected(
            "cancellation",
            "cancellation_fulfillment_started",
            "This order can no longer be cancelled after fulfillment has started.",
        )
    if snapshot.fulfillment_status == "processing":
        return _manual(
            "cancellation",
            "cancellation_fulfillment_processing",
            "This cancellation needs warehouse review.",
            "p1",
        )
    if snapshot.order_status in ("pending_payment", "confirmed"):
        return _eligible("cancellation", "cancellation_eligible", "This order can be cancelled.")
    return _manual(
        "cancellation",
        "cancellation_state_invalid",
        "This cancellation needs manual review.",
        "p1",
    )


def _evaluate_return_or_exchange(
    *,
    operation_type: OperationType,
    snapshot: OrderSnapshot,
    now: datetime,
    thresholds: CurrencyThresholds,
) -> OperationDecision:
    if snapshot.payment_status in ("refund_pending", "refunded"):
        return _rejected(
            operation_type,
            "operation_payment_already_refunded",
            "This order already has a refund in progress or completed.",
        )
    if snapshot.fulfillment_status != "delivered":
        return _rejected(
            operation_type,
            "operation_not_delivered",
            "This order must be delivered before a return or exchange can be requested.",
        )
    if snapshot.delivered_at is None or snapshot.delivered_at > now:
        return _manual(
            operation_type,
            "operation_delivery_date_invalid",
            "Delivery timing needs manual review.",
            "p1",
        )
    if now > snapshot.delivered_at + timedelta(days=RETURN_WINDOW_DAYS):
        return _rejected(
            operation_type,
            "operation_return_window_expired",
            "This order is past the return and exchange deadline.",
        )

    eligibility = (
        snapshot.return_eligible
        if operation_type == "return"
        else snapshot.exchange_eligible
    )
    if eligibility is False:
        return _rejected(
            operation_type,
            f"{operation_type}_not_eligible",
            f"This order is not eligible for {operation_type}.",
        )
    if eligibility is None:
        return _manual(
            operation_type,
            f"{operation_type}_eligibility_unknown",
            f"{operation_type.title()} eligibility needs manual review.",
            "p1",
        )

    threshold = thresholds.threshold_for(snapshot.currency)
    if threshold is None:
        return _manual(
            operation_type,
            "currency_threshold_unconfigured",
            "This currency needs manual review.",
            "p1",
        )
    if operation_type == "return" and snapshot.amount >= threshold:
        return _manual(
            "return",
            "return_manual_amount_review",
            "This return amount needs manual review.",
            "p1",
        )
    return _eligible(
        operation_type,
        f"{operation_type}_eligible",
        f"This order is eligible for {operation_type}.",
    )


def _evaluate_exchange(
    *,
    snapshot: OrderSnapshot,
    now: datetime,
    thresholds: CurrencyThresholds,
    replacement_available: bool | None,
) -> OperationDecision:
    base_decision = _evaluate_return_or_exchange(
        operation_type="exchange",
        snapshot=snapshot,
        now=now,
        thresholds=thresholds,
    )
    if base_decision.outcome != "eligible":
        return base_decision
    if replacement_available is False:
        return _rejected(
            "exchange",
            "exchange_replacement_unavailable",
            "The requested replacement is unavailable.",
        )
    if replacement_available is None:
        return _manual(
            "exchange",
            "exchange_inventory_unknown",
            "Replacement availability needs manual review.",
            "p2",
        )
    return _eligible(
        "exchange",
        "exchange_eligible",
        "This order is eligible for exchange.",
    )


def _evaluate_tracking_stalled(
    snapshot: OrderSnapshot,
    now: datetime,
) -> DeliveryDecision:
    if (
        snapshot.fulfillment_status != "shipped"
        or snapshot.last_tracking_event_at is None
        or snapshot.last_tracking_event_at > now
    ):
        return _delivery_manual(
            "tracking_stalled",
            "delivery_data_invalid",
            "Tracking data needs investigation.",
            "p1",
        )
    if now - snapshot.last_tracking_event_at >= timedelta(hours=TRACKING_STALLED_HOURS):
        return _delivery_manual(
            "tracking_stalled",
            "delivery_tracking_stalled",
            "Tracking has not updated for 72 hours.",
            "p1",
        )
    return _delivery_self_service(
        "tracking_stalled",
        "delivery_tracking_recent",
        "Tracking is still updating.",
    )


def _evaluate_delayed(
    snapshot: OrderSnapshot,
    now: datetime,
    investigation_requested: bool,
) -> DeliveryDecision:
    if snapshot.promised_delivery_at is None:
        return _delivery_manual(
            "delayed",
            "delivery_data_invalid",
            "Delivery timing needs investigation.",
            "p1",
        )
    if snapshot.fulfillment_status == "delivered" or now <= snapshot.promised_delivery_at:
        return _delivery_self_service(
            "delayed",
            "delivery_not_overdue",
            "The order is not confirmed as overdue.",
        )
    if investigation_requested or snapshot.last_tracking_event_at is None:
        return _delivery_manual(
            "delayed",
            "delivery_overdue_investigation",
            "The delayed delivery needs investigation.",
            "p2",
        )
    return _delivery_self_service(
        "delayed",
        "delivery_overdue",
        "The order is delayed; current tracking is available.",
    )


def _validate_request_context(
    request: OrderOperationRequest,
    snapshot: OrderSnapshot,
    now: datetime,
) -> None:
    if request.order_id != snapshot.order_id:
        raise ValueError("request and snapshot must have the same order_id")
    _require_aware_datetime(now)


def _require_aware_datetime(value: datetime) -> None:
    if not is_aware(value):
        raise ValueError("now must be timezone-aware")


def _has_contradictory_lifecycle(snapshot: OrderSnapshot) -> bool:
    if snapshot.fulfillment_status == "delivered" and snapshot.delivered_at is None:
        return True
    if snapshot.fulfillment_status != "delivered" and snapshot.delivered_at is not None:
        return True
    if snapshot.fulfillment_status == "unfulfilled" and snapshot.shipped_at is not None:
        return True
    if snapshot.fulfillment_status in ("shipped", "delivered") and snapshot.shipped_at is None:
        return True
    if (
        snapshot.order_status == "pending_payment"
        and snapshot.fulfillment_status != "unfulfilled"
    ):
        return True
    if (
        snapshot.order_status == "cancelled"
        and snapshot.fulfillment_status != "unfulfilled"
    ):
        return True
    return snapshot.delivered_at is not None and snapshot.delivered_at < snapshot.created_at


def _conflicting_operation(
    snapshot: OrderSnapshot,
    operation_type: OperationType,
) -> ExistingOperation | None:
    for operation in snapshot.existing_operations:
        if operation.status not in _ACTIVE_OPERATION_STATUSES:
            continue
        return operation
    return None


def _eligible(
    operation_type: OperationType,
    reason_code: str,
    display_reason: str,
) -> OperationDecision:
    return OperationDecision(
        outcome="eligible",
        operation_type=operation_type,
        requires_confirmation=True,
        reason_codes=(reason_code,),
        display_reason=display_reason,
    )


def _manual(
    operation_type: OperationType,
    reason_code: str,
    display_reason: str,
    priority: CasePriority,
) -> OperationDecision:
    return OperationDecision(
        outcome="manual_review",
        operation_type=operation_type,
        requires_confirmation=True,
        reason_codes=(reason_code,),
        display_reason=display_reason,
        case_recommendation=CaseRecommendation(
            case_type="order_operation_review",
            priority=priority,
            reason_codes=(reason_code,),
        ),
    )


def _rejected(
    operation_type: OperationType,
    reason_code: str,
    display_reason: str,
) -> OperationDecision:
    return OperationDecision(
        outcome="rejected",
        operation_type=operation_type,
        reason_codes=(reason_code,),
        display_reason=display_reason,
    )


def _existing(
    operation_type: OperationType,
    existing_operation: ExistingOperation,
) -> OperationDecision:
    return OperationDecision(
        outcome="existing_operation",
        operation_type=operation_type,
        reason_codes=("operation_already_exists",),
        display_reason="An existing order operation is already being handled.",
        existing_operation=existing_operation,
    )


def _delivery_manual(
    issue_type: DeliveryIssueType,
    reason_code: str,
    display_reason: str,
    priority: CasePriority,
) -> DeliveryDecision:
    return DeliveryDecision(
        outcome="manual_review",
        issue_type=issue_type,
        reason_codes=(reason_code,),
        display_reason=display_reason,
        case_recommendation=CaseRecommendation(
            case_type="delivery_investigation",
            priority=priority,
            reason_codes=(reason_code,),
        ),
    )


def _delivery_self_service(
    issue_type: DeliveryIssueType,
    reason_code: str,
    display_reason: str,
) -> DeliveryDecision:
    return DeliveryDecision(
        outcome="self_service",
        issue_type=issue_type,
        reason_codes=(reason_code,),
        display_reason=display_reason,
    )
