"""Structured model outputs used by the customer-service workflow."""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

Intent = Literal["refund_request", "order_inquiry", "complaint"]
SemanticRiskLevel = Literal["none", "low", "medium", "high", "critical"]
SemanticRiskCategory = Literal[
    "self_harm",
    "violence",
    "legal",
    "regulatory",
    "reputation",
    "other",
]
StaffComplaintSeverity = Literal["critical", "high", "medium", "low"]
FormalComplaintKind = Literal["ordinary", "staff_conduct", "other_formal"]


class Route(BaseModel):
    """Represent the intent selected by the routing model."""

    step: Intent = Field(description="The primary business intent of the message")
    human_handoff_requested: bool = Field(
        description=(
            "Whether the user explicitly asks to speak with, contact, or be "
            "transferred to a human customer-service representative"
        )
    )


class FormalComplaintDetection(BaseModel):
    """Represent a conservative formal-complaint classification."""

    complaint_kind: FormalComplaintKind = Field(
        description=(
            "Use 'staff_conduct' only for an explicit complaint about staff "
            "conduct, 'other_formal' only for an explicitly filed non-staff "
            "complaint, and 'ordinary' for general dissatisfaction or feedback"
        )
    )
    staff_complaint_severity: StaffComplaintSeverity | None = Field(
        default=None,
        description=(
            "Severity of explicitly reported staff conduct; required only "
            "when complaint_kind is 'staff_conduct'"
        ),
    )
    reason: str = Field(
        description="A concise explanation grounded in the user's complaint"
    )

    @model_validator(mode="after")
    def validate_staff_severity(self) -> Self:
        """Keep complaint kind and staff severity internally consistent."""
        if (
            self.complaint_kind == "staff_conduct"
            and self.staff_complaint_severity is None
        ):
            raise ValueError("staff_complaint_severity is required for staff_conduct")
        if (
            self.complaint_kind != "staff_conduct"
            and self.staff_complaint_severity is not None
        ):
            raise ValueError("staff_complaint_severity is only valid for staff_conduct")
        return self


class OrderDetection(BaseModel):
    """Represent an order-number detection result."""

    has_order_id: bool = Field(
        description="Whether the user input contains a complete order number"
    )
    order_id: str | None = Field(
        default=None,
        description="The complete order number when one is present",
    )


class SemanticRiskDetection(BaseModel):
    """Represent the language model's contextual risk assessment."""

    risk_level: SemanticRiskLevel = Field(
        description=(
            "The highest semantic risk level supported by the user's message "
            "and conversation context. Use 'none' only when no genuine risk "
            "is present."
        )
    )
    categories: list[SemanticRiskCategory] = Field(
        description=(
            "The applicable risk categories. Use 'other' when a genuine risk "
            "does not fit any named category. Return an empty list only when "
            "risk_level is 'none'."
        )
    )
    reason: str = Field(
        description=(
            "A concise explanation grounded in the user's message, conversation "
            "context, and any rule-based signals."
        )
    )
