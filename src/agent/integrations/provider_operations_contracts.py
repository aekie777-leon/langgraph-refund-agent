"""Strict field-whitelisted contracts for the Provider operations control plane."""

from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent.integrations.models import (
    InboxProcessingStatus,
    OutboxDeliveryStatus,
    ProviderAggregateType,
    ProviderFailureKind,
)
from agent.integrations.persistence_models import (
    DeliveryAttemptOutcome,
    InboxAttemptOutcome,
)

ProviderOperationsRequestId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]
SafeProviderErrorCode = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]{0,127}$"),
]


class ProviderRedriveReasonCode(StrEnum):
    """Enumerate the only operator-supplied reasons accepted for redrive."""

    DEPENDENCY_OR_CONFIGURATION_RESTORED = "dependency_or_configuration_restored"
    TRANSIENT_INCIDENT_RESOLVED = "transient_incident_resolved"
    MANUAL_RETRY_APPROVED = "manual_retry_approved"


class _StrictProviderOperationsModel(BaseModel):
    """Apply the shared deny-by-default Provider operations model policy."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class ProviderRedriveRequest(_StrictProviderOperationsModel):
    """Request one idempotent redrive using a fixed, non-free-form reason."""

    request_id: ProviderOperationsRequestId
    reason_code: ProviderRedriveReasonCode


class ProviderOutboxQueueSummary(_StrictProviderOperationsModel):
    """Expose one safe aggregate count for an Outbox status."""

    status: OutboxDeliveryStatus
    count: int = Field(ge=0)
    oldest_available_at: AwareDatetime | None = None


class ProviderInboxQueueSummary(_StrictProviderOperationsModel):
    """Expose one safe aggregate count for an Inbox status."""

    status: InboxProcessingStatus
    count: int = Field(ge=0)
    oldest_available_at: AwareDatetime | None = None


class ProviderQueueOverview(_StrictProviderOperationsModel):
    """Return tenant-scoped Outbox and Inbox queue aggregates."""

    outbox: tuple[ProviderOutboxQueueSummary, ...] = Field(default_factory=tuple)
    inbox: tuple[ProviderInboxQueueSummary, ...] = Field(default_factory=tuple)
    generated_at: AwareDatetime


class ProviderOutboxAttemptView(_StrictProviderOperationsModel):
    """Expose only operational metadata from one Outbox attempt."""

    delivery_cycle: int = Field(ge=1)
    attempt_number: int = Field(ge=1)
    outcome: DeliveryAttemptOutcome | None = None
    failure_kind: ProviderFailureKind | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    safe_error_code: SafeProviderErrorCode | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    next_available_at: AwareDatetime | None = None


class ProviderInboxAttemptView(_StrictProviderOperationsModel):
    """Expose only operational metadata from one Inbox attempt."""

    processing_cycle: int = Field(ge=1)
    attempt_number: int = Field(ge=1)
    outcome: InboxAttemptOutcome | None = None
    safe_error_code: SafeProviderErrorCode | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None


class ProviderRedriveView(_StrictProviderOperationsModel):
    """Expose immutable, payload-free redrive audit metadata."""

    # v0.7 accepted arbitrary non-blank request ids.  Invalid legacy values
    # are suppressed rather than disclosed or used to relax new requests.
    request_id: ProviderOperationsRequestId | None = None
    # Legacy v0.7 outbox audit rows intentionally remain unclassified. The old
    # free-form reason is never projected into this control-plane contract.
    reason_code: ProviderRedriveReasonCode | None = None
    actor: str = Field(min_length=1)
    previous_cycle: int = Field(ge=1)
    new_cycle: int = Field(ge=2)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_cycle(self) -> Self:
        """Require each audit record to describe exactly one new cycle."""
        if self.new_cycle != self.previous_cycle + 1:
            raise ValueError("new_cycle must equal previous_cycle + 1")
        return self


class ProviderOutboxDetail(_StrictProviderOperationsModel):
    """Expose a payload-free Outbox failure and its safe history."""

    command_id: UUID
    aggregate_type: ProviderAggregateType
    aggregate_id: UUID
    status: OutboxDeliveryStatus
    delivery_cycle: int = Field(ge=1)
    attempts_in_cycle: int = Field(ge=0)
    available_at: AwareDatetime
    last_failure_kind: ProviderFailureKind | None = None
    last_error_code: SafeProviderErrorCode | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    published_at: AwareDatetime | None = None
    dead_at: AwareDatetime | None = None
    attempts: tuple[ProviderOutboxAttemptView, ...] = Field(default_factory=tuple)
    redrives: tuple[ProviderRedriveView, ...] = Field(default_factory=tuple)


class ProviderInboxDetail(_StrictProviderOperationsModel):
    """Expose a payload-free Inbox failure and its safe history."""

    inbox_id: UUID
    command_id: UUID
    aggregate_type: ProviderAggregateType
    aggregate_id: UUID
    status: InboxProcessingStatus
    processing_cycle: int = Field(ge=1)
    attempts_in_cycle: int = Field(ge=0)
    total_attempts: int = Field(ge=0)
    available_at: AwareDatetime
    last_error_code: SafeProviderErrorCode | None = None
    received_at: AwareDatetime
    updated_at: AwareDatetime
    processed_at: AwareDatetime | None = None
    failed_at: AwareDatetime | None = None
    attempts: tuple[ProviderInboxAttemptView, ...] = Field(default_factory=tuple)
    redrives: tuple[ProviderRedriveView, ...] = Field(default_factory=tuple)
