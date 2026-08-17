"""Define request and error models for the internal support-case API."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.cases.models import RESERVED_AGENT_IDS, CaseStatus, OnHoldReason


class ChangeCaseStatusRequest(BaseModel):
    """Represent one idempotent support-case status operation."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    target_status: CaseStatus
    request_id: str = Field(min_length=1, max_length=128)
    on_hold_reason: OnHoldReason | None = None

    @model_validator(mode="after")
    def validate_on_hold_reason(self) -> Self:
        """Keep the requested status and hold metadata consistent."""
        if self.target_status == "on_hold":
            if self.on_hold_reason is None:
                raise ValueError(
                    "on_hold_reason is required when target_status is on_hold"
                )
        elif self.on_hold_reason is not None:
            raise ValueError(
                "on_hold_reason is only valid when target_status is on_hold"
            )
        return self


class AssignCaseRequest(BaseModel):
    """Represent one idempotent support-case assignment operation."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^[^:]+$")
    request_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_reserved_agent(self) -> Self:
        """Reject identifiers reserved by the system."""
        if self.agent_id in RESERVED_AGENT_IDS:
            raise ValueError(f"agent_id is reserved: {self.agent_id}")
        return self


class ApiErrorDetail(BaseModel):
    """Describe one stable machine-readable API error."""

    code: str
    message: str


class ApiErrorResponse(BaseModel):
    """Wrap the stable internal API error shape."""

    error: ApiErrorDetail
