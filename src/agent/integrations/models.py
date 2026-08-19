"""Stable integration domain models for provider connectivity.

These models are the canonical wire contracts for v0.7 provider integration:
command envelopes, command results, webhook envelopes, and the delivery /
processing status vocabularies. They never contain raw provider JSON; adapter
code is responsible for translating provider responses into these models.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_serializer,
    model_validator,
)

from agent.operations.models import (
    DeliveryIssueType,
    OperationReason,
    OperationRecordStatus,
    OperationType,
    OrderOperation,
)

if TYPE_CHECKING:
    from agent.cases.models import SupportCase

SchemaVersion = Literal[1]
SCHEMA_VERSION: SchemaVersion = 1

ProviderCapability = Literal["order_query", "inventory_query", "order_operation"]
ProviderAggregateType = Literal["order_operation", "support_case"]
AuthScheme = Literal["bearer", "api_key", "none"]
ProviderCommandType = Literal[
    "cancel_order",
    "return_order",
    "exchange_order",
    "delivery_investigation",
]
ProviderCommandAcceptanceStatus = Literal["accepted", "rejected"]
ProviderCommandExecutionStatus = Literal[
    "accepted",
    "processing",
    "completed",
    "rejected",
]
ProviderWebhookEventType = Literal["provider_command_status_changed"]

OutboxDeliveryStatus = Literal[
    "pending",
    "processing",
    "retry_scheduled",
    "published",
    "dead",
]
InboxProcessingStatus = Literal["received", "processing", "processed", "failed"]

ProviderFailureKind = Literal[
    "network_error",
    "timeout",
    "http_retryable",
    "http_client_error",
    "provider_rejection",
    "validation_error",
]

# Retryable kinds per the v0.7 delivery rules: network/connection failures,
# timeouts, and HTTP 408/429/5xx. Everything else is not retried by default.
RETRYABLE_FAILURE_KINDS = frozenset(
    {"network_error", "timeout", "http_retryable"}
)

OUTBOX_DELIVERY_TRANSITIONS: dict[OutboxDeliveryStatus, frozenset[OutboxDeliveryStatus]] = {
    "pending": frozenset({"processing", "dead"}),
    "processing": frozenset({"published", "retry_scheduled", "dead"}),
    "retry_scheduled": frozenset({"processing", "dead"}),
    "published": frozenset(),
    "dead": frozenset(),
}

INBOX_PROCESSING_TRANSITIONS: dict[InboxProcessingStatus, frozenset[InboxProcessingStatus]] = {
    "received": frozenset({"processing", "failed"}),
    "processing": frozenset({"received", "processed", "failed"}),
    "processed": frozenset(),
    "failed": frozenset(),
}

_PROVIDER_TO_LOCAL_COMMAND_STATUS: dict[
    ProviderCommandExecutionStatus, OperationRecordStatus
] = {
    "accepted": "submitted",
    "processing": "processing",
    "completed": "completed",
    "rejected": "rejected",
}


def map_provider_command_status(
    status: ProviderCommandExecutionStatus,
) -> OperationRecordStatus:
    """Map a provider command status to the local operation status.

    This is the deterministic write boundary for the future inbox processor:
    the mapping itself is stable and testable before any database write exists.
    """
    return _PROVIDER_TO_LOCAL_COMMAND_STATUS[status]


class _MaskedSecretModel(BaseModel):
    """Base class that never serializes SecretStr values."""

    @model_serializer(mode="wrap")
    def _mask_secret_fields(self, handler) -> dict[str, Any]:
        dumped = handler(self)
        return {
            key: (None if isinstance(value, SecretStr) else value)
            for key, value in dumped.items()
        }


class ProviderAuthentication(_MaskedSecretModel):
    """Outbound API authentication for one provider connection.

    The credential is a ``SecretStr``: it never appears in ``repr``, ``str``,
    ``model_dump``, JSON output, or error messages. Read the value explicitly
    with ``credential.get_secret_value()`` at call time.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    scheme: AuthScheme
    credential: SecretStr | None = None
    api_key_header: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        """Keep the chosen scheme and its fields consistent."""
        if self.scheme == "none":
            if self.credential is not None or self.api_key_header is not None:
                raise ValueError("none auth must not carry credential or header")
            return self
        if self.scheme == "bearer":
            if self.credential is None:
                raise ValueError("bearer auth requires a credential")
            if not self.credential.get_secret_value():
                raise ValueError("bearer credential must not be empty")
            if self.api_key_header is not None:
                raise ValueError("bearer auth must not set api_key_header")
            return self
        if self.credential is None or self.api_key_header is None:
            raise ValueError("api_key auth requires credential and api_key_header")
        if not self.credential.get_secret_value():
            raise ValueError("api_key credential must not be empty")
        return self


class ProviderWebhookConnection(_MaskedSecretModel):
    """Trusted inbound webhook configuration for one provider connection.

    The tenant and signing secret are resolved from ``connection_id`` by
    ``ProviderWebhookConnectionResolver``; they are never taken from the
    webhook request body. The signing secret is a ``SecretStr`` and never
    appears in ``repr``, ``str``, ``model_dump``, JSON, or error messages.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    connection_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    signing_secret: SecretStr
    validity_window_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_secret_not_empty(self) -> Self:
        """Reject an empty signing secret before it reaches the verifier."""
        if not self.signing_secret.get_secret_value():
            raise ValueError("signing_secret must not be empty")
        return self


class ProviderTimeout(BaseModel):
    """Timeout configuration for one provider connection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connect_seconds: float = Field(
        default=5.0, ge=0.1, le=300.0, allow_inf_nan=False
    )
    read_seconds: float = Field(default=30.0, ge=0.1, le=300.0, allow_inf_nan=False)
    write_seconds: float = Field(default=30.0, ge=0.1, le=300.0, allow_inf_nan=False)


class ProviderConnection(BaseModel):
    """One resolved outbound provider connection for a tenant and capability.

    URL and port belong to the connection, never to the capability. Multiple
    capabilities may share one ``connection_id`` (and therefore one HTTP
    connection pool) when they point at the same endpoint base. Outbound API
    authentication and inbound webhook signing secrets are separate models;
    see ``ProviderWebhookConnection`` for the inbound side.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    capability: ProviderCapability
    base_url: AnyHttpUrl
    endpoint: str = Field(min_length=1)
    authentication: ProviderAuthentication
    timeout: ProviderTimeout = Field(default_factory=ProviderTimeout)
    max_concurrency: int = Field(default=4, ge=1, le=1000)
    requests_per_second: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_endpoint(self) -> Self:
        """Require an absolute path on the connection endpoint."""
        if not self.endpoint.startswith("/"):
            raise ValueError("endpoint must start with '/'")
        return self


class OrderOperationCommandPayload(BaseModel):
    """Strongly typed cancellation / return / exchange command payload."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    order_id: str = Field(pattern=r"^ORD-\d{5}$")
    operation_type: OperationType
    reason: OperationReason
    replacement_variant_id: str | None = Field(default=None, min_length=1)


class DeliveryInvestigationCommandPayload(BaseModel):
    """Strongly typed delivery-investigation command payload.

    Issuing this command already means an investigation is requested, so there
    is no redundant ``investigation_requested`` flag.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    order_id: str = Field(pattern=r"^ORD-\d{5}$")
    issue_type: DeliveryIssueType


ProviderCommandPayload = (
    OrderOperationCommandPayload | DeliveryInvestigationCommandPayload
)

_COMMAND_TYPE_BY_OPERATION: dict[OperationType, ProviderCommandType] = {
    "cancellation": "cancel_order",
    "return": "return_order",
    "exchange": "exchange_order",
}

_OPERATION_TYPE_BY_COMMAND: dict[ProviderCommandType, OperationType] = {
    "cancel_order": "cancellation",
    "return_order": "return",
    "exchange_order": "exchange",
}


class ProviderCommandEnvelope(BaseModel):
    """Canonical envelope for one provider command (at-least-once delivery).

    ``command_id`` is the command / outbox-message identifier.
    ``source_message_id`` is the triggering chat message identifier.
    ``aggregate_type`` is either ``order_operation`` (cancellation, return,
    exchange; ``aggregate_id`` is the local ``operation_id`` and
    ``expected_order_version`` is required) or ``support_case`` (delivery
    investigation; ``aggregate_id`` is the ``case_id`` and
    ``expected_order_version`` must be None). The ``idempotency_key`` is
    stably bound to the aggregate so retries and duplicate dispatches are
    deduplicated by the provider.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: SchemaVersion = SCHEMA_VERSION
    command_id: UUID
    idempotency_key: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    aggregate_type: ProviderAggregateType
    aggregate_id: UUID
    expected_order_version: int | None = Field(default=None, ge=1)
    tenant_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    connection_id: str = Field(min_length=1)
    command_type: ProviderCommandType
    payload: ProviderCommandPayload
    created_at: AwareDatetime

    @field_validator("created_at", mode="after")
    @classmethod
    def _normalize_created_at(cls, value: datetime) -> datetime:
        """Normalize the timestamp to UTC."""
        return value.astimezone(UTC)

    @classmethod
    def for_order_operation(
        cls,
        *,
        operation: OrderOperation,
        connection_id: str,
        command_id: UUID,
        created_at: AwareDatetime,
    ) -> "ProviderCommandEnvelope":
        """Build the canonical envelope for a persisted order operation."""
        command_type = _COMMAND_TYPE_BY_OPERATION[operation.operation_type]
        return cls(
            command_id=command_id,
            idempotency_key=f"order-operation:{operation.operation_id}",
            source_message_id=operation.source_message_id,
            aggregate_type="order_operation",
            aggregate_id=operation.operation_id,
            expected_order_version=operation.order_version,
            tenant_id=operation.tenant_id,
            customer_id=operation.customer_id,
            connection_id=connection_id,
            command_type=command_type,
            payload=OrderOperationCommandPayload(
                order_id=operation.order_id,
                operation_type=operation.operation_type,
                reason=operation.request_reason_code,
                replacement_variant_id=operation.replacement_variant_id,
            ),
            created_at=created_at,
        )

    @classmethod
    def for_delivery_investigation(
        cls,
        *,
        case: "SupportCase",
        issue_type: DeliveryIssueType,
        connection_id: str,
        command_id: UUID,
        created_at: AwareDatetime,
    ) -> "ProviderCommandEnvelope":
        """Build the canonical envelope for a persisted delivery-investigation case.

        The aggregate is the ``support_case`` identified by the persisted
        ``case_id``; no fake order operation is constructed.
        """
        if case.case_type != "delivery_investigation":
            raise ValueError(
                "for_delivery_investigation requires a delivery_investigation case"
            )
        if case.order_id is None:
            raise ValueError("a delivery_investigation case must carry an order_id")
        return cls(
            command_id=command_id,
            idempotency_key=f"delivery-investigation:{case.case_id}",
            source_message_id=case.source_message_id,
            aggregate_type="support_case",
            aggregate_id=case.case_id,
            expected_order_version=None,
            tenant_id=case.tenant_id,
            customer_id=case.customer_id,
            connection_id=connection_id,
            command_type="delivery_investigation",
            payload=DeliveryInvestigationCommandPayload(
                order_id=case.order_id,
                issue_type=issue_type,
            ),
            created_at=created_at,
        )

    @model_validator(mode="after")
    def validate_payload_type(self) -> Self:
        """Keep aggregate, command type, payload, and idempotency key consistent."""
        if self.command_type == "delivery_investigation":
            if self.aggregate_type != "support_case":
                raise ValueError(
                    "delivery_investigation requires aggregate_type 'support_case'"
                )
            if self.expected_order_version is not None:
                raise ValueError(
                    "delivery_investigation must not carry expected_order_version"
                )
            if not isinstance(self.payload, DeliveryInvestigationCommandPayload):
                raise ValueError(
                    "delivery_investigation requires a delivery payload"
                )
            expected_key = f"delivery-investigation:{self.aggregate_id}"
            if self.idempotency_key != expected_key:
                raise ValueError(
                    f"delivery_investigation idempotency_key must be {expected_key!r}"
                )
            return self

        if self.aggregate_type != "order_operation":
            raise ValueError(
                f"{self.command_type} requires aggregate_type 'order_operation'"
            )
        if self.expected_order_version is None:
            raise ValueError(f"{self.command_type} requires expected_order_version")
        if not isinstance(self.payload, OrderOperationCommandPayload):
            raise ValueError(f"{self.command_type} requires an order-operation payload")
        expected_type = _OPERATION_TYPE_BY_COMMAND[self.command_type]
        if self.payload.operation_type != expected_type:
            raise ValueError(
                f"command_type {self.command_type!r} requires operation_type "
                f"{expected_type!r}, got {self.payload.operation_type!r}"
            )
        expected_key = f"order-operation:{self.aggregate_id}"
        if self.idempotency_key != expected_key:
            raise ValueError(
                f"{self.command_type} idempotency_key must be {expected_key!r}"
            )
        return self


class ProviderCommandResult(BaseModel):
    """Parsed immediate HTTP response for one command (never raw provider JSON).

    The acceptance status is binary: the provider either accepted the command
    for asynchronous processing or rejected it. Async progress arrives later
    through webhooks with ``ProviderCommandExecutionStatus``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: SchemaVersion = SCHEMA_VERSION
    command_id: UUID
    provider_operation_id: str | None = Field(default=None, min_length=1)
    provider_reference: str | None = Field(default=None, min_length=1)
    status: ProviderCommandAcceptanceStatus
    received_at: AwareDatetime

    @field_validator("received_at", mode="after")
    @classmethod
    def _normalize_received_at(cls, value: datetime) -> datetime:
        """Normalize the timestamp to UTC."""
        return value.astimezone(UTC)


class ProviderWebhookEventData(BaseModel):
    """Strongly typed command-status webhook event data.

    The event is associated with the same aggregates as the outbound command
    envelope: ``command_id`` plus ``aggregate_type`` / ``aggregate_id``.
    ``order_id``, when present, is auxiliary business context only; it is never
    used for authorization or unique association. Convert ``command_status``
    to the local status with ``map_provider_command_status`` at the inbox
    processing boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    command_id: UUID
    aggregate_type: ProviderAggregateType
    aggregate_id: UUID
    command_status: ProviderCommandExecutionStatus
    provider_operation_id: str | None = Field(default=None, min_length=1)
    provider_reference: str | None = Field(default=None, min_length=1)
    order_id: str | None = Field(default=None, pattern=r"^ORD-\d{5}$")
    occurred_at: AwareDatetime

    @field_validator("occurred_at", mode="after")
    @classmethod
    def _normalize_occurred_at(cls, value: datetime) -> datetime:
        """Normalize the timestamp to UTC."""
        return value.astimezone(UTC)


class ProviderWebhookEnvelope(BaseModel):
    """Canonical webhook envelope after signature verification.

    The ``tenant_id`` is resolved from ``provider_connection_id`` by the
    trusted ``ProviderWebhookConnectionResolver``; it is never taken from the
    request body.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: SchemaVersion = SCHEMA_VERSION
    event_id: str = Field(min_length=1)
    provider_connection_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    timestamp: AwareDatetime
    event_type: ProviderWebhookEventType
    data: ProviderWebhookEventData

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        """Normalize the timestamp to UTC."""
        return value.astimezone(UTC)


class RetryDecision(BaseModel):
    """Deterministic retry decision for one failed delivery attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    retryable: bool
    kind: ProviderFailureKind
    attempts_so_far: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    delay_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    retry_after_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    exhausted: bool
