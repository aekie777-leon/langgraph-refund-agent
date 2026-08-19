"""Unit tests for provider-messaging persistence models."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent.integrations.models import (
    DeliveryInvestigationCommandPayload,
    ProviderCommandEnvelope,
    ProviderWebhookEventData,
)
from agent.integrations.persistence_models import (
    ClaimedOutboxMessage,
    InboxMessage,
    InboxProcessingAttempt,
    OutboxDeliveryAttempt,
    OutboxMessage,
    OutboxRedrive,
)
from agent.operations.models import OrderOperation

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
OPERATION_ID = UUID("11111111-1111-1111-1111-111111111111")
CASE_ID = UUID("22222222-2222-2222-2222-222222222222")
LEASE_ID = UUID("33333333-3333-3333-3333-333333333333")


def _operation() -> OrderOperation:
    return OrderOperation(
        operation_id=OPERATION_ID,
        idempotency_key="operation:thread-1:message-1:created",
        thread_id="thread-1",
        source_message_id="message-1",
        order_id="ORD-10001",
        operation_type="return",
        request_reason_code="damaged_item",
        policy_reason_codes=("return_eligible",),
        display_reason="This order is eligible for return.",
        order_version=3,
        amount=Decimal("69.99"),
        currency="USD",
        requires_manual_review=False,
        request_excerpt="Return this item.",
        status="queued",
        created_at=NOW,
        updated_at=NOW,
        customer_id="customer-a",
        tenant_id="tenant-demo",
        created_by="tenant-demo:customer-a",
    )


def _envelope() -> ProviderCommandEnvelope:
    return ProviderCommandEnvelope.for_order_operation(
        operation=_operation(),
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )


def _outbox(
    *,
    envelope: ProviderCommandEnvelope | None = None,
    **overrides: object,
) -> OutboxMessage:
    envelope = envelope or _envelope()
    values: dict[str, object] = {
        "command_id": envelope.command_id,
        "schema_version": envelope.schema_version,
        "idempotency_key": envelope.idempotency_key,
        "tenant_id": envelope.tenant_id,
        "customer_id": envelope.customer_id,
        "source_message_id": envelope.source_message_id,
        "provider_connection_id": envelope.connection_id,
        "provider_capability": "order_operation",
        "command_type": envelope.command_type,
        "aggregate_type": envelope.aggregate_type,
        "aggregate_id": envelope.aggregate_id,
        "expected_order_version": envelope.expected_order_version,
        "payload": envelope.payload,
        "status": "pending",
        "delivery_cycle": 1,
        "attempts_in_cycle": 0,
        "available_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return OutboxMessage.model_validate(values)


def _attempt(**overrides: object) -> OutboxDeliveryAttempt:
    values: dict[str, object] = {
        "attempt_id": uuid4(),
        "command_id": OPERATION_ID,
        "delivery_cycle": 1,
        "attempt_number": 1,
        "lease_id": LEASE_ID,
        "worker_id": "worker-1",
        "started_at": NOW,
    }
    values.update(overrides)
    return OutboxDeliveryAttempt.model_validate(values)


def _inbox(**overrides: object) -> InboxMessage:
    values: dict[str, object] = {
        "inbox_id": uuid4(),
        "provider_connection_id": "wc-1",
        "event_id": "evt-1",
        "tenant_id": "tenant-demo",
        "schema_version": 1,
        "event_type": "provider_command_status_changed",
        "command_id": OPERATION_ID,
        "aggregate_type": "order_operation",
        "aggregate_id": OPERATION_ID,
        "payload": ProviderWebhookEventData(
            command_id=OPERATION_ID,
            aggregate_type="order_operation",
            aggregate_id=OPERATION_ID,
            command_status="processing",
            provider_operation_id="provider-op-1",
            occurred_at=NOW,
        ),
        "raw_body_sha256": "a" * 64,
        "status": "received",
        "processing_attempts": 0,
        "available_at": NOW,
        "received_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return InboxMessage.model_validate(values)


def _inbox_attempt(**overrides: object) -> InboxProcessingAttempt:
    values: dict[str, object] = {
        "attempt_id": uuid4(),
        "inbox_id": uuid4(),
        "attempt_number": 1,
        "lease_id": LEASE_ID,
        "worker_id": "worker-1",
        "started_at": NOW,
    }
    values.update(overrides)
    return InboxProcessingAttempt.model_validate(values)


def test_pending_outbox_is_valid_without_lease() -> None:
    outbox = _outbox()

    assert outbox.status == "pending"
    assert outbox.lease_id is None


def test_processing_outbox_requires_full_lease() -> None:
    with pytest.raises(ValidationError, match="lease"):
        _outbox(status="processing")

    processing = _outbox(
        status="processing",
        lease_id=LEASE_ID,
        lease_owner="worker-1",
        lease_expires_at=NOW + timedelta(seconds=90),
    )
    assert processing.lease_owner == "worker-1"


def test_non_processing_outbox_must_not_carry_lease() -> None:
    with pytest.raises(ValidationError, match="lease"):
        _outbox(lease_id=LEASE_ID, lease_owner="worker-1", lease_expires_at=NOW)


def test_published_and_dead_require_their_timestamps() -> None:
    with pytest.raises(ValidationError, match="published_at"):
        _outbox(status="published")
    published = _outbox(status="published", published_at=NOW)
    assert published.published_at == NOW

    with pytest.raises(ValidationError, match="dead_at"):
        _outbox(status="dead")
    dead = _outbox(status="dead", dead_at=NOW)
    assert dead.dead_at == NOW


def test_aggregate_version_invariants() -> None:
    with pytest.raises(ValidationError, match="expected_order_version"):
        _outbox(expected_order_version=None)
    with pytest.raises(ValidationError, match="expected_order_version"):
        _outbox(
            aggregate_type="support_case",
            aggregate_id=CASE_ID,
            command_type="delivery_investigation",
            expected_order_version=1,
        )


def test_support_case_delivery_outbox_is_valid() -> None:
    outbox = _outbox(
        command_type="delivery_investigation",
        aggregate_type="support_case",
        aggregate_id=CASE_ID,
        expected_order_version=None,
        idempotency_key=f"delivery-investigation:{CASE_ID}",
        payload=DeliveryInvestigationCommandPayload(
            order_id="ORD-10010", issue_type="tracking_stalled"
        ),
    )

    assert outbox.aggregate_type == "support_case"
    assert outbox.expected_order_version is None


def test_outbox_error_message_is_length_limited() -> None:
    with pytest.raises(ValidationError, match="last_error_message"):
        _outbox(last_error_message="x" * 501)


def test_outbox_timestamps_are_normalized_to_utc() -> None:
    local = datetime(2026, 8, 17, 20, 0, tzinfo=timezone(timedelta(hours=8)))

    outbox = _outbox(available_at=local)

    assert outbox.available_at == datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def test_outbox_envelope_round_trip() -> None:
    envelope = _envelope()
    outbox = _outbox(envelope=envelope)

    rebuilt = outbox.to_envelope()

    assert rebuilt.command_id == envelope.command_id
    assert rebuilt.idempotency_key == envelope.idempotency_key
    assert rebuilt.aggregate_id == envelope.aggregate_id
    assert rebuilt.payload == envelope.payload


def test_outbox_models_do_not_carry_secrets() -> None:
    rendered = str(_outbox()).lower()
    for name in ("secret", "token", "password", "api_key", "signature"):
        assert name not in rendered


def test_claim_requires_processing_with_matching_attempt() -> None:
    attempt = _attempt()
    claimed = ClaimedOutboxMessage.model_validate(
        {
            **_outbox(
                command_id=OPERATION_ID,
                idempotency_key="order-operation:11111111-1111-1111-1111-111111111111",
                status="processing",
                lease_id=LEASE_ID,
                lease_owner="worker-1",
                lease_expires_at=NOW + timedelta(seconds=90),
            ).model_dump(mode="python"),
            "attempt": attempt,
        }
    )
    assert claimed.attempt.worker_id == "worker-1"

    with pytest.raises(ValidationError, match="worker_id"):
        ClaimedOutboxMessage.model_validate(
            {
                **_outbox(
                    command_id=OPERATION_ID,
                    idempotency_key="order-operation:11111111-1111-1111-1111-111111111111",
                    status="processing",
                    lease_id=LEASE_ID,
                    lease_owner="worker-1",
                    lease_expires_at=NOW + timedelta(seconds=90),
                ).model_dump(mode="python"),
                "attempt": _attempt(worker_id="worker-2"),
            }
        )


def test_delivery_attempt_requires_failure_kind_for_failures() -> None:
    with pytest.raises(ValidationError, match="failure_kind"):
        _attempt(
            finished_at=NOW,
            outcome="retry_scheduled",
        )
    with pytest.raises(ValidationError, match="failure_kind"):
        _attempt(
            finished_at=NOW,
            outcome="accepted",
            failure_kind="network_error",
        )

    attempt = _attempt(
        finished_at=NOW,
        outcome="retry_scheduled",
        failure_kind="http_retryable",
        safe_error_code="http_500",
        safe_error_message="upstream unavailable",
        retry_after_seconds=30,
        next_available_at=NOW + timedelta(seconds=30),
    )
    assert attempt.outcome == "retry_scheduled"


def test_delivery_attempt_rejects_non_finite_retry_after() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError, match="retry_after_seconds"):
            _attempt(retry_after_seconds=value)


def test_delivery_attempt_rejects_overlong_error() -> None:
    with pytest.raises(ValidationError, match="safe_error_message"):
        _attempt(
            finished_at=NOW,
            outcome="terminal_failure",
            failure_kind="validation_error",
            safe_error_message="y" * 501,
        )


def test_redrive_requires_next_cycle() -> None:
    redrive = OutboxRedrive(
        redrive_id=uuid4(),
        command_id=OPERATION_ID,
        tenant_id="tenant-demo",
        request_id="redrive-1",
        requested_by="sup-1",
        reason="provider recovered",
        previous_cycle=2,
        new_cycle=3,
        created_at=NOW,
    )
    assert redrive.new_cycle == redrive.previous_cycle + 1

    with pytest.raises(ValidationError, match="new_cycle"):
        OutboxRedrive(
            redrive_id=uuid4(),
            command_id=OPERATION_ID,
            tenant_id="tenant-demo",
            request_id="redrive-2",
            requested_by="sup-1",
            reason="provider recovered",
            previous_cycle=2,
            new_cycle=5,
            created_at=NOW,
        )


def test_inbox_processed_and_failed_require_timestamps() -> None:
    with pytest.raises(ValidationError, match="processed_at"):
        _inbox(status="processed")
    with pytest.raises(ValidationError, match="failed_at"):
        _inbox(status="failed")

    processed = _inbox(status="processed", processed_at=NOW)
    assert processed.status == "processed"


def test_inbox_rejects_invalid_body_hash() -> None:
    with pytest.raises(ValidationError, match="raw_body_sha256"):
        _inbox(raw_body_sha256="ZZZ")


def test_inbox_processing_attempt_keeps_outcome_and_finish_together() -> None:
    with pytest.raises(ValidationError, match="outcome"):
        _inbox_attempt(finished_at=NOW)
    attempt = _inbox_attempt(
        finished_at=NOW,
        outcome="processed",
    )
    assert attempt.outcome == "processed"


def test_inbox_models_do_not_carry_secrets() -> None:
    rendered = str(_inbox()).lower()
    for name in ("secret", "token", "password", "signature", "authorization"):
        assert name not in rendered


def test_error_fields_reject_oversize_and_blank_values() -> None:
    from agent.integrations.postgres_writes import _validate_error_fields

    with pytest.raises(ValueError, match="error_code"):
        _validate_error_fields(error_code="x" * 501, error_message=None)
    with pytest.raises(ValueError, match="error_message"):
        _validate_error_fields(error_code=None, error_message="x" * 501)
    with pytest.raises(ValueError, match="error_code"):
        _validate_error_fields(error_code="   ", error_message=None)
    with pytest.raises(ValueError, match="error_message"):
        _validate_error_fields(error_code=None, error_message="   ")
    _validate_error_fields(error_code="ok", error_message="fine")
    _validate_error_fields(error_code=None, error_message=None)


def test_claim_parameters_reject_invalid_values() -> None:
    from agent.integrations.postgres_repository import _validate_claim_parameters

    with pytest.raises(ValueError, match="worker_id"):
        _validate_claim_parameters(worker_id="  ", batch_size=1, lease_seconds=10)
    with pytest.raises(ValueError, match="batch_size"):
        _validate_claim_parameters(worker_id="w", batch_size=0, lease_seconds=10)
    with pytest.raises(ValueError, match="lease_seconds"):
        _validate_claim_parameters(worker_id="w", batch_size=1, lease_seconds=0)
    with pytest.raises(ValueError, match="lease_seconds"):
        _validate_claim_parameters(
            worker_id="w", batch_size=1, lease_seconds=float("nan")
        )
    with pytest.raises(ValueError, match="lease_seconds"):
        _validate_claim_parameters(
            worker_id="w", batch_size=1, lease_seconds=float("inf")
        )
    _validate_claim_parameters(worker_id="w", batch_size=1, lease_seconds=0.5)
