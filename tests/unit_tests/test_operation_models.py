"""Unit tests for order-operation model validation."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent.operations.models import (
    CaseRecommendation,
    OperationDecision,
    OrderOperationRequest,
    OrderSnapshot,
)


def _snapshot() -> OrderSnapshot:
    return OrderSnapshot(
        order_id="ORD-10001",
        version=1,
        amount=Decimal("69.99"),
        currency="USD",
        order_status="confirmed",
        payment_status="paid",
        fulfillment_status="unfulfilled",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def test_exchange_requires_a_replacement_variant() -> None:
    with pytest.raises(ValidationError, match="replacement_variant_id"):
        OrderOperationRequest(
            thread_id="thread-1",
            source_message_id="message-1",
            order_id="ORD-10001",
            operation_type="exchange",
            reason="size_or_variant_issue",
            customer_id="customer-a",
            tenant_id="tenant-demo",
        )


def test_cancellation_rejects_a_return_only_reason() -> None:
    with pytest.raises(ValidationError, match="cancellation reason"):
        OrderOperationRequest(
            thread_id="thread-1",
            source_message_id="message-1",
            order_id="ORD-10001",
            operation_type="cancellation",
            reason="damaged_item",
            customer_id="customer-a",
            tenant_id="tenant-demo",
        )


def test_return_rejects_a_replacement_variant() -> None:
    with pytest.raises(ValidationError, match="only an exchange"):
        OrderOperationRequest(
            thread_id="thread-1",
            source_message_id="message-1",
            order_id="ORD-10001",
            operation_type="return",
            reason="changed_mind",
            replacement_variant_id="variant-blue",
            customer_id="customer-a",
            tenant_id="tenant-demo",
        )


def test_snapshot_preserves_upstream_lifecycle_contradictions_for_policy() -> None:
    snapshot = _snapshot().model_copy(
        update={
            "order_status": "cancelled",
            "fulfillment_status": "shipped",
            "shipped_at": datetime(2026, 8, 2, tzinfo=UTC),
        }
    )

    assert snapshot.order_status == "cancelled"
    assert snapshot.fulfillment_status == "shipped"


def test_manual_review_requires_case_recommendation() -> None:
    with pytest.raises(ValidationError, match="case recommendation"):
        OperationDecision(
            outcome="manual_review",
            operation_type="return",
            requires_confirmation=True,
            reason_codes=("return_manual_amount_review",),
            display_reason="Manual review is required.",
        )


def test_eligible_operation_requires_confirmation() -> None:
    with pytest.raises(ValidationError, match="requires confirmation"):
        OperationDecision(
            outcome="eligible",
            operation_type="cancellation",
            reason_codes=("cancellation_eligible",),
            display_reason="The order can be cancelled.",
        )


def test_manual_review_with_case_recommendation_is_valid() -> None:
    decision = OperationDecision(
        outcome="manual_review",
        operation_type="return",
        requires_confirmation=True,
        reason_codes=("return_manual_amount_review",),
        display_reason="Manual review is required.",
        case_recommendation=CaseRecommendation(
            case_type="order_operation_review",
            priority="p1",
            reason_codes=("return_manual_amount_review",),
        ),
    )

    assert decision.case_recommendation is not None
