"""Define domain models for deterministic support-case handoff decisions."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent.integrations.models import ProviderCommandExecutionStatus
from agent.schemas import (
    SemanticRiskCategory,
    SemanticRiskLevel,
    StaffComplaintSeverity,
)

CaseType = Literal[
    "safety_review",
    "business_escalation",
    "refund_review",
    "general_support",
    "staff_conduct_complaint",
    "other_complaint",
    "order_operation_review",
    "delivery_investigation",
]
CasePriority = Literal["p0", "p1", "p2", "p3"]
CaseStatus = Literal["open", "in_progress", "on_hold", "resolved"]
CaseEventType = Literal[
    "case_created",
    "trigger_appended",
    "status_changed",
    "assigned",
    "provider_update",
]
CaseServiceAction = Literal[
    "not_created",
    "created",
    "event_appended",
    "duplicate_ignored",
    "status_changed",
    "status_unchanged",
    "assigned",
]
RESERVED_AGENT_IDS = frozenset({"system", "legacy"})
OnHoldReason = Literal[
    "waiting_customer",
    "waiting_external_system",
    "waiting_internal_team",
    "system_unavailable",
    "force_majeure",
    "other",
]
HandoffReason = Literal[
    "hard_critical_self_harm",
    "hard_critical_violence",
    "hard_critical_legal",
    "hard_critical_regulatory",
    "hard_critical_reputation",
    "hard_critical_other",
    "semantic_critical_self_harm",
    "semantic_critical_violence",
    "semantic_critical_legal",
    "semantic_critical_regulatory",
    "semantic_critical_reputation",
    "semantic_critical_other",
    "semantic_high_self_harm",
    "semantic_high_violence",
    "semantic_high_legal",
    "semantic_high_regulatory",
    "semantic_high_reputation",
    "semantic_high_other",
    "semantic_medium_self_harm",
    "semantic_medium_violence",
    "semantic_medium_legal",
    "semantic_medium_regulatory",
    "semantic_medium_reputation",
    "semantic_medium_other",
    "refund_manual_review",
    "confirmed_human_request",
    "staff_conduct_critical",
    "staff_conduct_high",
    "staff_conduct_medium",
    "staff_conduct_low",
    "explicit_other_complaint",
    "order_state_invalid",
    "cancellation_fulfillment_processing",
    "cancellation_state_invalid",
    "operation_delivery_date_invalid",
    "return_eligibility_unknown",
    "exchange_eligibility_unknown",
    "currency_threshold_unconfigured",
    "return_manual_amount_review",
    "exchange_inventory_unknown",
    "delivery_data_invalid",
    "delivery_tracking_stalled",
    "delivery_overdue_investigation",
    "delivery_failed",
    "delivery_marked_received_dispute",
    "delivery_damage_claim",
    "delivery_other_issue",
    "provider_delivery_failed",
]


class HandoffPolicyInput(BaseModel):
    """Contain structured facts consumed by the deterministic handoff policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hard_critical_categories: tuple[SemanticRiskCategory, ...] = Field(
        default_factory=tuple,
        description="Categories produced by hard-critical deterministic rules.",
    )
    semantic_risk_level: SemanticRiskLevel | None = Field(
        default=None,
        description=(
            "Structured semantic risk level, or None when semantic classification "
            "was skipped."
        ),
    )
    semantic_risk_categories: tuple[SemanticRiskCategory, ...] = Field(
        default_factory=tuple,
        description="Categories returned by semantic risk classification.",
    )
    refund_requires_manual_review: bool = False
    human_handoff_confirmed: bool = False
    staff_complaint_severity: StaffComplaintSeverity | None = None
    explicit_other_complaint: bool = False
    domain_case_reason_codes: tuple[HandoffReason, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def validate_semantic_risk(self) -> Self:
        """Reject inconsistent semantic risk facts before policy evaluation."""
        level = self.semantic_risk_level
        categories = self.semantic_risk_categories

        if level in (None, "none") and categories:
            raise ValueError(
                "semantic_risk_categories must be empty when semantic_risk_level "
                "is None or 'none'"
            )
        if level not in (None, "none") and not categories:
            raise ValueError(
                "semantic_risk_categories must not be empty for a non-none "
                "semantic_risk_level"
            )
        return self


class CaseListQuery(BaseModel):
    """Represent validated support-case list filters."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    status: CaseStatus | None = None
    priority: CasePriority | None = None
    case_type: CaseType | None = None
    thread_id: str | None = Field(default=None, min_length=1)
    order_id: str | None = Field(default=None, min_length=1)
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class HandoffDecision(BaseModel):
    """Represent the complete deterministic result of handoff policy evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    should_create_case: bool
    case_type: CaseType | None = None
    priority: CasePriority | None = None
    reason_codes: tuple[HandoffReason, ...] = Field(default_factory=tuple)
    display_reason: str = ""

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        """Keep positive and negative decisions internally consistent."""
        if self.should_create_case:
            if self.case_type is None or self.priority is None:
                raise ValueError(
                    "case_type and priority are required when creating a case"
                )
            if not self.reason_codes or not self.display_reason:
                raise ValueError(
                    "reason_codes and display_reason are required when creating a case"
                )
            return self

        if (
            self.case_type is not None
            or self.priority is not None
            or self.reason_codes
            or self.display_reason
        ):
            raise ValueError(
                "a no-case decision must not contain case details or reasons"
            )
        return self


class CaseTrigger(BaseModel):
    """Represent one message that may create or update a support case."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    thread_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    order_id: str | None = None
    risk_level: SemanticRiskLevel | None = None
    risk_categories: tuple[SemanticRiskCategory, ...] = Field(default_factory=tuple)
    triggering_message_excerpt: str = Field(min_length=1, max_length=500)


class SupportCase(BaseModel):
    """Represent the current aggregate state of one support case."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    case_id: UUID
    thread_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    order_id: str | None = None
    case_type: CaseType
    priority: CasePriority
    status: CaseStatus = "open"
    risk_level: SemanticRiskLevel | None = None
    risk_categories: tuple[SemanticRiskCategory, ...] = Field(default_factory=tuple)
    reason_codes: tuple[HandoffReason, ...] = Field(min_length=1)
    display_reason: str = Field(min_length=1)
    triggering_message_excerpt: str = Field(min_length=1, max_length=500)
    on_hold_reason: OnHoldReason | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: int = Field(default=1, ge=1)
    customer_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    assigned_agent_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_lifecycle_fields(self) -> Self:
        """Keep timestamps, on-hold metadata, and case type consistent."""
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        if self.status == "on_hold" and self.on_hold_reason is None:
            raise ValueError("on_hold_reason is required when status is 'on_hold'")
        if self.status != "on_hold" and self.on_hold_reason is not None:
            raise ValueError("on_hold_reason is only allowed when status is 'on_hold'")
        if self.case_type == "delivery_investigation" and self.order_id is None:
            raise ValueError(
                "delivery_investigation cases require an order_id"
            )
        return self


class SupportCaseEvent(BaseModel):
    """Represent one immutable event in a support case history."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    event_id: UUID
    idempotency_key: str = Field(min_length=1)
    case_id: UUID
    event_type: CaseEventType
    source_message_id: str | None = None
    order_id: str | None = None
    risk_level: SemanticRiskLevel | None = None
    risk_categories: tuple[SemanticRiskCategory, ...] = Field(default_factory=tuple)
    reason_codes: tuple[HandoffReason, ...] = Field(default_factory=tuple)
    triggering_message_excerpt: str = Field(default="", max_length=500)
    previous_priority: CasePriority | None = None
    current_priority: CasePriority | None = None
    previous_status: CaseStatus | None = None
    current_status: CaseStatus | None = None
    on_hold_reason: OnHoldReason | None = None
    previous_assigned_agent_id: str | None = Field(default=None, min_length=1)
    current_assigned_agent_id: str | None = Field(default=None, min_length=1)
    provider_command_id: UUID | None = None
    provider_command_status: ProviderCommandExecutionStatus | None = None
    provider_reference: str | None = Field(default=None, min_length=1)
    actor: str | None = None
    customer_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_event_fields(self) -> Self:
        """Require the fields needed by each event type."""
        if self.event_type in ("case_created", "trigger_appended"):
            if self.source_message_id is None:
                raise ValueError("source_message_id is required for trigger events")
            if not self.reason_codes or not self.triggering_message_excerpt:
                raise ValueError(
                    "reason_codes and triggering_message_excerpt are required "
                    "for trigger events"
                )
            if self.current_priority is None or self.current_status is None:
                raise ValueError(
                    "current_priority and current_status are required for "
                    "trigger events"
                )
            return self

        if self.event_type == "assigned":
            if self.current_assigned_agent_id is None:
                raise ValueError(
                    "current_assigned_agent_id is required for assigned events"
                )
            if not self.actor:
                raise ValueError("actor is required for assigned events")
            return self

        if self.event_type == "provider_update":
            if self.provider_command_id is None:
                raise ValueError(
                    "provider_command_id is required for provider_update events"
                )
            if self.provider_command_status is None:
                raise ValueError(
                    "provider_command_status is required for provider_update events"
                )
            if not self.actor:
                raise ValueError("actor is required for provider_update events")
            if self.previous_status is not None or self.current_status is not None:
                raise ValueError(
                    "provider_update events must not carry case status fields"
                )
            return self

        if self.previous_status is None or self.current_status is None:
            raise ValueError(
                "previous_status and current_status are required for status events"
            )
        if not self.actor:
            raise ValueError("actor is required for status events")
        if self.current_status == "on_hold" and self.on_hold_reason is None:
            raise ValueError("on_hold_reason is required for an on-hold status event")
        if self.current_status != "on_hold" and self.on_hold_reason is not None:
            raise ValueError(
                "on_hold_reason is only allowed for an on-hold status event"
            )
        return self


class CaseServiceResult(BaseModel):
    """Represent the result of one case-service operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: CaseServiceAction
    case: SupportCase | None = None
    event: SupportCaseEvent | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        """Keep the action consistent with the returned case and event."""
        if self.action == "not_created":
            if self.case is not None or self.event is not None:
                raise ValueError("not_created must not contain a case or event")
            return self

        if self.case is None:
            raise ValueError(f"{self.action} must contain a case")

        writes_event = self.action in (
            "created",
            "event_appended",
            "status_changed",
            "assigned",
        )
        if writes_event and self.event is None:
            raise ValueError(f"{self.action} must contain an event")
        if not writes_event and self.event is not None:
            raise ValueError(f"{self.action} must not contain an event")
        return self


class SupportCasePage(BaseModel):
    """Represent one stable page of support cases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[SupportCase, ...] = Field(default_factory=tuple)
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class SupportCaseEventPage(BaseModel):
    """Represent one stable page of immutable case events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[SupportCaseEvent, ...] = Field(default_factory=tuple)
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
