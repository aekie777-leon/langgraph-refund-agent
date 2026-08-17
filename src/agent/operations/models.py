"""Define typed order-operation domain contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

OrderStatus = Literal["pending_payment", "confirmed", "cancelled", "completed"]
PaymentStatus = Literal[
    "unpaid",
    "paid",
    "refund_pending",
    "partially_refunded",
    "refunded",
]
FulfillmentStatus = Literal["unfulfilled", "processing", "shipped", "delivered"]
OperationType = Literal["cancellation", "return", "exchange"]
OperationStatus = Literal[
    "submitted",
    "processing",
    "manual_review",
    "completed",
    "rejected",
]
OperationRecordStatus = Literal[
    "pending_confirmation",
    "submitted",
    "processing",
    "manual_review",
    "completed",
    "rejected",
    "cancelled_by_customer",
]
OperationEventType = Literal[
    "operation_created",
    "confirmation_recorded",
    "status_changed",
    "support_case_attached",
]
OperationServiceAction = Literal[
    "created",
    "duplicate_ignored",
    "confirmed",
    "submitted",
    "cancelled",
    "status_changed",
    "status_unchanged",
    "support_case_attached",
]
ExistingOperationType = Literal["refund", "cancellation", "return", "exchange"]
DeliveryIssueType = Literal[
    "delayed",
    "tracking_stalled",
    "delivery_failed",
    "marked_delivered_not_received",
    "package_damaged",
    "wrong_item_or_missing_parts",
    "other_delivery_issue",
]
CancellationReason = Literal[
    "ordered_by_mistake",
    "no_longer_needed",
    "incorrect_item_or_quantity",
    "delivery_too_slow",
    "payment_issue",
    "other",
]
ReturnExchangeReason = Literal[
    "changed_mind",
    "wrong_item_received",
    "damaged_item",
    "defective_item",
    "missing_parts",
    "not_as_described",
    "size_or_variant_issue",
    "other",
]
OperationReason = CancellationReason | ReturnExchangeReason
OperationOutcome = Literal[
    "eligible",
    "manual_review",
    "rejected",
    "existing_operation",
    "already_completed",
]
DeliveryOutcome = Literal["self_service", "manual_review", "offer_return_exchange"]
RecommendedCaseType = Literal[
    "order_operation_review",
    "delivery_investigation",
]
CasePriority = Literal["p0", "p1", "p2", "p3"]

_CANCELLATION_REASONS = frozenset(
    {
        "ordered_by_mistake",
        "no_longer_needed",
        "incorrect_item_or_quantity",
        "delivery_too_slow",
        "payment_issue",
        "other",
    }
)


class ExistingOperation(BaseModel):
    """Summarize an operation already recorded by an order system."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    operation_id: str = Field(min_length=1)
    operation_type: ExistingOperationType
    status: OperationStatus
    provider_reference: str | None = Field(default=None, min_length=1)


class OrderSnapshot(BaseModel):
    """Represent the read-only facts used by deterministic operation policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    order_id: str = Field(pattern=r"^ORD-\d{5}$")
    version: int = Field(ge=1)
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    order_status: OrderStatus
    payment_status: PaymentStatus
    fulfillment_status: FulfillmentStatus
    created_at: AwareDatetime
    shipped_at: AwareDatetime | None = None
    delivered_at: AwareDatetime | None = None
    promised_delivery_at: AwareDatetime | None = None
    last_tracking_event_at: AwareDatetime | None = None
    return_eligible: bool | None = None
    exchange_eligible: bool | None = None
    existing_operations: tuple[ExistingOperation, ...] = Field(default_factory=tuple)
    customer_id: str | None = None
    tenant_id: str | None = None


class OrderOperationRequest(BaseModel):
    """Represent one normalized cancellation, return, or exchange request."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    thread_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    order_id: str = Field(pattern=r"^ORD-\d{5}$")
    operation_type: OperationType
    reason: OperationReason
    replacement_variant_id: str | None = Field(default=None, min_length=1)
    customer_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_operation_specific_fields(self) -> Self:
        """Keep reason codes and exchange data valid for the requested operation."""
        if self.operation_type == "cancellation":
            if self.reason not in _CANCELLATION_REASONS:
                raise ValueError("a cancellation must use a cancellation reason")
            if self.replacement_variant_id is not None:
                raise ValueError("only an exchange may include replacement_variant_id")
            return self

        if self.reason in _CANCELLATION_REASONS - {"other"}:
            raise ValueError("a return or exchange must use a return/exchange reason")
        if self.operation_type == "exchange" and self.replacement_variant_id is None:
            raise ValueError("an exchange requires replacement_variant_id")
        if self.operation_type == "return" and self.replacement_variant_id is not None:
            raise ValueError("only an exchange may include replacement_variant_id")
        return self


class DeliveryIssueRequest(BaseModel):
    """Represent a normalized delivery issue; claims are not proof of fault."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    thread_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    order_id: str = Field(pattern=r"^ORD-\d{5}$")
    issue_type: DeliveryIssueType
    investigation_requested: bool = False


class CaseRecommendation(BaseModel):
    """Describe a future support-case request without persisting a case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_type: RecommendedCaseType
    priority: CasePriority
    reason_codes: tuple[str, ...] = Field(min_length=1)


class OperationDecision(BaseModel):
    """Return a deterministic decision for one state-changing operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: OperationOutcome
    operation_type: OperationType
    requires_confirmation: bool = False
    reason_codes: tuple[str, ...] = Field(min_length=1)
    display_reason: str = Field(min_length=1)
    existing_operation: ExistingOperation | None = None
    case_recommendation: CaseRecommendation | None = None

    @model_validator(mode="after")
    def validate_outcome_fields(self) -> Self:
        """Keep the result shape consistent with its deterministic outcome."""
        if self.outcome == "existing_operation":
            if self.existing_operation is None:
                raise ValueError("existing_operation outcome requires an operation")
            if self.requires_confirmation or self.case_recommendation is not None:
                raise ValueError("an existing operation cannot request confirmation or a case")
            return self

        if self.existing_operation is not None:
            raise ValueError("only existing_operation may contain an existing operation")
        if self.outcome in ("eligible", "manual_review") and not self.requires_confirmation:
            raise ValueError(f"{self.outcome} requires confirmation")
        if self.outcome in ("rejected", "already_completed") and self.requires_confirmation:
            raise ValueError(f"{self.outcome} cannot require confirmation")
        if self.outcome == "manual_review" and self.case_recommendation is None:
            raise ValueError("manual_review requires a case recommendation")
        if self.outcome != "manual_review" and self.case_recommendation is not None:
            raise ValueError("only manual_review may contain a case recommendation")
        return self


class DeliveryDecision(BaseModel):
    """Return a deterministic assessment for a delivery issue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: DeliveryOutcome
    issue_type: DeliveryIssueType
    reason_codes: tuple[str, ...] = Field(min_length=1)
    display_reason: str = Field(min_length=1)
    case_recommendation: CaseRecommendation | None = None

    @model_validator(mode="after")
    def validate_outcome_fields(self) -> Self:
        """Require a case recommendation only for a manual review."""
        if self.outcome == "manual_review" and self.case_recommendation is None:
            raise ValueError("manual_review requires a case recommendation")
        if self.outcome != "manual_review" and self.case_recommendation is not None:
            raise ValueError("only manual_review may contain a case recommendation")
        return self


class OrderOperation(BaseModel):
    """Represent one persisted whole-order customer operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    operation_id: UUID
    idempotency_key: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    order_id: str = Field(pattern=r"^ORD-\d{5}$")
    operation_type: OperationType
    request_reason_code: OperationReason
    policy_reason_codes: tuple[str, ...] = Field(min_length=1)
    display_reason: str = Field(min_length=1)
    replacement_variant_id: str | None = Field(default=None, min_length=1)
    request_excerpt: str = Field(min_length=1, max_length=500)
    order_version: int = Field(ge=1)
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    requires_manual_review: bool
    review_case_type: RecommendedCaseType | None = None
    review_priority: CasePriority | None = None
    support_case_id: UUID | None = None
    provider_reference: str | None = Field(default=None, min_length=1)
    status: OperationRecordStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: int = Field(default=1, ge=1)
    customer_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    created_by: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_persisted_operation(self) -> Self:
        """Keep persisted operation fields aligned with the database contract."""
        OrderOperationRequest(
            thread_id=self.thread_id,
            source_message_id=self.source_message_id,
            order_id=self.order_id,
            operation_type=self.operation_type,
            reason=self.request_reason_code,
            replacement_variant_id=self.replacement_variant_id,
            customer_id=self.customer_id,
            tenant_id=self.tenant_id,
        )
        if self.requires_manual_review:
            if (
                self.review_case_type != "order_operation_review"
                or self.review_priority not in ("p1", "p2")
            ):
                raise ValueError("manual review requires its approved case metadata")
        elif self.review_case_type is not None or self.review_priority is not None:
            raise ValueError("only manual review may contain review case metadata")
        if self.support_case_id is not None and not self.requires_manual_review:
            raise ValueError("only manual review may link a support case")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        return self


class OrderOperationEvent(BaseModel):
    """Represent one immutable operation event."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    event_id: UUID
    idempotency_key: str = Field(min_length=1)
    operation_id: UUID
    event_type: OperationEventType
    previous_status: OperationRecordStatus | None = None
    current_status: OperationRecordStatus | None = None
    provider_reference: str | None = Field(default=None, min_length=1)
    support_case_id: UUID | None = None
    actor: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_event_shape(self) -> Self:
        """Require status fields only for a status-change event."""
        has_statuses = (
            self.previous_status is not None and self.current_status is not None
        )
        if self.event_type == "status_changed" and not has_statuses:
            raise ValueError("status_changed requires previous and current status")
        if self.event_type != "status_changed" and (
            self.previous_status is not None or self.current_status is not None
        ):
            raise ValueError("only status_changed may contain status fields")
        if self.event_type == "support_case_attached" and self.support_case_id is None:
            raise ValueError("support_case_attached requires support_case_id")
        return self


class OperationServiceResult(BaseModel):
    """Represent the result of one operation-service write."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: OperationServiceAction
    operation: OrderOperation
    events: tuple[OrderOperationEvent, ...] = Field(default_factory=tuple)


def is_aware(value: datetime) -> bool:
    """Return whether a datetime contains a usable UTC offset."""
    return value.tzinfo is not None and value.utcoffset() is not None
