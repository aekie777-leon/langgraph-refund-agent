"""Persistence models for provider messaging (outbox / inbox / attempts).

These models mirror the ``integration`` schema rows and enforce the same state
invariants on the application side: status/lease consistency, timestamp
invariants, aggregate/command consistency, and Pydantic validation of the
JSON payloads. They never contain secrets.
"""

from datetime import UTC
from math import isfinite
from typing import Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from agent.integrations.models import (
    SCHEMA_VERSION,
    InboxProcessingStatus,
    OutboxDeliveryStatus,
    ProviderAggregateType,
    ProviderCapability,
    ProviderCommandEnvelope,
    ProviderCommandPayload,
    ProviderCommandType,
    ProviderFailureKind,
    ProviderWebhookEventData,
    ProviderWebhookEventType,
    SchemaVersion,
)

DeliveryAttemptOutcome = Literal[
    "accepted",
    "provider_rejected",
    "retry_scheduled",
    "terminal_failure",
    "lease_expired",
]
InboxAttemptOutcome = Literal[
    "processed",
    "retry_scheduled",
    "terminal_failure",
    "lease_expired",
]

_ERROR_MESSAGE_MAX = 500


def _to_utc(value: AwareDatetime) -> AwareDatetime:
    """Normalize an aware datetime to UTC."""
    return value.astimezone(UTC)


class OutboxMessage(BaseModel):
    """One outbox command row awaiting or undergoing provider delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    command_id: UUID
    schema_version: SchemaVersion = SCHEMA_VERSION
    idempotency_key: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    provider_connection_id: str = Field(min_length=1)
    provider_capability: ProviderCapability
    command_type: ProviderCommandType
    aggregate_type: ProviderAggregateType
    aggregate_id: UUID
    expected_order_version: int | None = Field(default=None, ge=1)
    payload: ProviderCommandPayload
    status: OutboxDeliveryStatus
    delivery_cycle: int = Field(default=1, ge=1)
    attempts_in_cycle: int = Field(default=0, ge=0)
    available_at: AwareDatetime
    lease_id: UUID | None = None
    lease_owner: str | None = Field(default=None, min_length=1)
    lease_expires_at: AwareDatetime | None = None
    last_failure_kind: ProviderFailureKind | None = None
    last_error_code: str | None = Field(default=None, max_length=_ERROR_MESSAGE_MAX)
    last_error_message: str | None = Field(default=None, max_length=_ERROR_MESSAGE_MAX)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    published_at: AwareDatetime | None = None
    dead_at: AwareDatetime | None = None

    @field_validator(
        "available_at",
        "lease_expires_at",
        "created_at",
        "updated_at",
        "published_at",
        "dead_at",
        mode="after",
    )
    @classmethod
    def _normalize_utc(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        """Normalize every timestamp to UTC."""
        return _to_utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        """Enforce status, lease, timestamp, and aggregate invariants."""
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        if self.status == "processing":
            if self.lease_id is None or self.lease_owner is None:
                raise ValueError("processing requires lease_id and lease_owner")
            if self.lease_expires_at is None:
                raise ValueError("processing requires lease_expires_at")
        else:
            if (
                self.lease_id is not None
                or self.lease_owner is not None
                or self.lease_expires_at is not None
            ):
                raise ValueError("non-processing status must not carry a lease")
        if self.status == "published" and self.published_at is None:
            raise ValueError("published requires published_at")
        if self.status != "published" and self.published_at is not None:
            raise ValueError("only published may carry published_at")
        if self.status == "dead" and self.dead_at is None:
            raise ValueError("dead requires dead_at")
        if self.status != "dead" and self.dead_at is not None:
            raise ValueError("only dead may carry dead_at")
        if self.aggregate_type == "order_operation":
            if self.expected_order_version is None:
                raise ValueError("order_operation requires expected_order_version")
            if self.command_type == "delivery_investigation":
                raise ValueError(
                    "order_operation aggregate cannot carry a delivery command"
                )
        else:
            if self.expected_order_version is not None:
                raise ValueError("support_case must not carry expected_order_version")
            if self.command_type != "delivery_investigation":
                raise ValueError(
                    "support_case aggregate requires a delivery_investigation command"
                )
        return self

    def to_envelope(self) -> ProviderCommandEnvelope:
        """Rebuild and re-validate the canonical command envelope."""
        return ProviderCommandEnvelope(
            schema_version=self.schema_version,
            command_id=self.command_id,
            idempotency_key=self.idempotency_key,
            source_message_id=self.source_message_id,
            aggregate_type=self.aggregate_type,
            aggregate_id=self.aggregate_id,
            expected_order_version=self.expected_order_version,
            tenant_id=self.tenant_id,
            customer_id=self.customer_id,
            connection_id=self.provider_connection_id,
            command_type=self.command_type,
            payload=self.payload,
            created_at=self.created_at,
        )


class OutboxDeliveryAttempt(BaseModel):
    """One immutable delivery attempt for an outbox command."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    attempt_id: UUID
    command_id: UUID
    delivery_cycle: int = Field(ge=1)
    attempt_number: int = Field(ge=1)
    lease_id: UUID
    worker_id: str = Field(min_length=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    outcome: DeliveryAttemptOutcome | None = None
    failure_kind: ProviderFailureKind | None = None
    http_status: int | None = Field(default=None, ge=100)
    provider_operation_id: str | None = Field(default=None, min_length=1)
    provider_reference: str | None = Field(default=None, min_length=1)
    safe_error_code: str | None = Field(default=None, max_length=_ERROR_MESSAGE_MAX)
    safe_error_message: str | None = Field(
        default=None, max_length=_ERROR_MESSAGE_MAX
    )
    retry_after_seconds: float | None = Field(default=None, ge=0)
    next_available_at: AwareDatetime | None = None

    @field_validator(
        "started_at",
        "finished_at",
        "next_available_at",
        mode="after",
    )
    @classmethod
    def _normalize_utc(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        """Normalize every timestamp to UTC."""
        return _to_utc(value) if value is not None else None

    @field_validator("retry_after_seconds", mode="after")
    @classmethod
    def _reject_non_finite(cls, value: float | None) -> float | None:
        """Reject NaN and infinite retry delays."""
        if value is not None and not isfinite(value):
            raise ValueError("retry_after_seconds must be finite")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        """Keep outcome and finish time consistent."""
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        if (self.outcome is None) != (self.finished_at is None):
            raise ValueError("outcome and finished_at must be present together")
        if self.outcome in ("provider_rejected", "retry_scheduled", "terminal_failure"):
            if self.failure_kind is None:
                raise ValueError(f"{self.outcome} requires failure_kind")
        elif self.failure_kind is not None:
            raise ValueError(
                "accepted and lease_expired must not carry failure_kind"
            )
        return self


class ClaimedOutboxMessage(OutboxMessage):
    """An outbox message claimed by a worker with its lease and attempt row."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    attempt: OutboxDeliveryAttempt

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        """Keep the claimed row, lease, and attempt consistent."""
        if self.status != "processing":
            raise ValueError("a claimed message must be processing")
        if self.attempt.command_id != self.command_id:
            raise ValueError("attempt command_id must match the message")
        if self.attempt.lease_id != self.lease_id:
            raise ValueError("attempt lease_id must match the message lease")
        if self.attempt.worker_id != self.lease_owner:
            raise ValueError("attempt worker_id must match the lease owner")
        return self


class OutboxRedrive(BaseModel):
    """One immutable manual redrive record for a dead outbox command."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    redrive_id: UUID
    command_id: UUID
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)
    previous_cycle: int = Field(ge=1)
    new_cycle: int = Field(ge=2)
    created_at: AwareDatetime

    @field_validator("created_at", mode="after")
    @classmethod
    def _normalize_utc(cls, value: AwareDatetime) -> AwareDatetime:
        """Normalize the timestamp to UTC."""
        return _to_utc(value)

    @model_validator(mode="after")
    def validate_cycle(self) -> Self:
        """Require the redrive to open exactly the next cycle."""
        if self.new_cycle != self.previous_cycle + 1:
            raise ValueError("new_cycle must equal previous_cycle + 1")
        return self


class InboxMessage(BaseModel):
    """One verified webhook event row awaiting or undergoing processing."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    inbox_id: UUID
    provider_connection_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    schema_version: SchemaVersion = SCHEMA_VERSION
    event_type: ProviderWebhookEventType
    command_id: UUID
    aggregate_type: ProviderAggregateType
    aggregate_id: UUID
    payload: ProviderWebhookEventData
    raw_body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: InboxProcessingStatus
    processing_attempts: int = Field(default=0, ge=0)
    available_at: AwareDatetime
    lease_id: UUID | None = None
    lease_owner: str | None = Field(default=None, min_length=1)
    lease_expires_at: AwareDatetime | None = None
    last_error_code: str | None = Field(default=None, max_length=_ERROR_MESSAGE_MAX)
    last_error_message: str | None = Field(default=None, max_length=_ERROR_MESSAGE_MAX)
    received_at: AwareDatetime
    updated_at: AwareDatetime
    processed_at: AwareDatetime | None = None
    failed_at: AwareDatetime | None = None

    @field_validator(
        "available_at",
        "lease_expires_at",
        "received_at",
        "updated_at",
        "processed_at",
        "failed_at",
        mode="after",
    )
    @classmethod
    def _normalize_utc(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        """Normalize every timestamp to UTC."""
        return _to_utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        """Enforce status, lease, and timestamp invariants."""
        if self.updated_at < self.received_at:
            raise ValueError("updated_at must not be earlier than received_at")
        if self.status == "processing":
            if self.lease_id is None or self.lease_owner is None:
                raise ValueError("processing requires lease_id and lease_owner")
            if self.lease_expires_at is None:
                raise ValueError("processing requires lease_expires_at")
        else:
            if (
                self.lease_id is not None
                or self.lease_owner is not None
                or self.lease_expires_at is not None
            ):
                raise ValueError("non-processing status must not carry a lease")
        if self.status == "processed" and self.processed_at is None:
            raise ValueError("processed requires processed_at")
        if self.status != "processed" and self.processed_at is not None:
            raise ValueError("only processed may carry processed_at")
        if self.status == "failed" and self.failed_at is None:
            raise ValueError("failed requires failed_at")
        if self.status != "failed" and self.failed_at is not None:
            raise ValueError("only failed may carry failed_at")
        return self


class InboxProcessingAttempt(BaseModel):
    """One immutable processing attempt for an inbox message."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    attempt_id: UUID
    inbox_id: UUID
    attempt_number: int = Field(ge=1)
    lease_id: UUID
    worker_id: str = Field(min_length=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    outcome: InboxAttemptOutcome | None = None
    safe_error_code: str | None = Field(default=None, max_length=_ERROR_MESSAGE_MAX)
    safe_error_message: str | None = Field(
        default=None, max_length=_ERROR_MESSAGE_MAX
    )

    @field_validator("started_at", "finished_at", mode="after")
    @classmethod
    def _normalize_utc(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        """Normalize every timestamp to UTC."""
        return _to_utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        """Keep outcome and finish time consistent."""
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        if (self.outcome is None) != (self.finished_at is None):
            raise ValueError("outcome and finished_at must be present together")
        return self


class ClaimedInboxMessage(InboxMessage):
    """An inbox message claimed by a worker with its lease and attempt row."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    attempt: InboxProcessingAttempt

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        """Keep the claimed row, lease, and attempt consistent."""
        if self.status != "processing":
            raise ValueError("a claimed message must be processing")
        if self.attempt.inbox_id != self.inbox_id:
            raise ValueError("attempt inbox_id must match the message")
        if self.attempt.lease_id != self.lease_id:
            raise ValueError("attempt lease_id must match the message lease")
        if self.attempt.worker_id != self.lease_owner:
            raise ValueError("attempt worker_id must match the lease owner")
        return self
