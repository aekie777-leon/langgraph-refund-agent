"""Unit tests for the field-whitelisted Provider operations API contracts."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from agent.integrations.provider_operations_contracts import (
    ProviderInboxAttemptView,
    ProviderInboxDetail,
    ProviderInboxQueueSummary,
    ProviderOutboxAttemptView,
    ProviderOutboxDetail,
    ProviderOutboxQueueSummary,
    ProviderQueueOverview,
    ProviderRedriveReasonCode,
    ProviderRedriveRequest,
    ProviderRedriveView,
)

NOW = datetime(2026, 8, 19, 10, 30, tzinfo=UTC)
COMMAND_ID = UUID("00000000-0000-4000-8000-000000000101")
INBOX_ID = UUID("00000000-0000-4000-8000-000000000102")
AGGREGATE_ID = UUID("00000000-0000-4000-8000-000000000103")


def _outbox_attempt() -> ProviderOutboxAttemptView:
    return ProviderOutboxAttemptView(
        delivery_cycle=2,
        attempt_number=1,
        outcome="terminal_failure",
        failure_kind="network_error",
        safe_error_code="provider_connection_error",
        started_at=NOW,
        finished_at=NOW,
    )


def _inbox_attempt() -> ProviderInboxAttemptView:
    return ProviderInboxAttemptView(
        processing_cycle=2,
        attempt_number=1,
        outcome="terminal_failure",
        safe_error_code="outbox_not_finalized",
        started_at=NOW,
        finished_at=NOW,
    )


def _redrive() -> ProviderRedriveView:
    return ProviderRedriveView(
        request_id="ops:req-101",
        reason_code="transient_incident_resolved",
        actor="tenant-demo:supervisor-1",
        previous_cycle=1,
        new_cycle=2,
        created_at=NOW,
    )


def _outbox_detail_values() -> dict[str, Any]:
    return {
        "command_id": COMMAND_ID,
        "aggregate_type": "order_operation",
        "aggregate_id": AGGREGATE_ID,
        "status": "dead",
        "delivery_cycle": 2,
        "attempts_in_cycle": 1,
        "available_at": NOW,
        "last_failure_kind": "network_error",
        "last_error_code": "provider_connection_error",
        "created_at": NOW,
        "updated_at": NOW,
        "dead_at": NOW,
        "attempts": (_outbox_attempt(),),
        "redrives": (_redrive(),),
    }


def _inbox_detail_values() -> dict[str, Any]:
    return {
        "inbox_id": INBOX_ID,
        "command_id": COMMAND_ID,
        "aggregate_type": "order_operation",
        "aggregate_id": AGGREGATE_ID,
        "status": "failed",
        "processing_cycle": 2,
        "attempts_in_cycle": 1,
        "total_attempts": 6,
        "available_at": NOW,
        "last_error_code": "outbox_not_finalized",
        "received_at": NOW,
        "updated_at": NOW,
        "failed_at": NOW,
        "attempts": (_inbox_attempt(),),
        "redrives": (_redrive(),),
    }


RESPONSE_FIELD_WHITELISTS: tuple[
    tuple[type[BaseModel], set[str]],
    ...,
] = (
    (
        ProviderOutboxQueueSummary,
        {"status", "count", "oldest_available_at"},
    ),
    (
        ProviderInboxQueueSummary,
        {"status", "count", "oldest_available_at"},
    ),
    (ProviderQueueOverview, {"outbox", "inbox", "generated_at"}),
    (
        ProviderOutboxAttemptView,
        {
            "delivery_cycle",
            "attempt_number",
            "outcome",
            "failure_kind",
            "http_status",
            "safe_error_code",
            "started_at",
            "finished_at",
            "next_available_at",
        },
    ),
    (
        ProviderInboxAttemptView,
        {
            "processing_cycle",
            "attempt_number",
            "outcome",
            "safe_error_code",
            "started_at",
            "finished_at",
        },
    ),
    (
        ProviderRedriveView,
        {
            "request_id",
            "reason_code",
            "actor",
            "previous_cycle",
            "new_cycle",
            "created_at",
        },
    ),
    (
        ProviderOutboxDetail,
        {
            "command_id",
            "aggregate_type",
            "aggregate_id",
            "status",
            "delivery_cycle",
            "attempts_in_cycle",
            "available_at",
            "last_failure_kind",
            "last_error_code",
            "created_at",
            "updated_at",
            "published_at",
            "dead_at",
            "attempts",
            "redrives",
        },
    ),
    (
        ProviderInboxDetail,
        {
            "inbox_id",
            "command_id",
            "aggregate_type",
            "aggregate_id",
            "status",
            "processing_cycle",
            "attempts_in_cycle",
            "total_attempts",
            "available_at",
            "last_error_code",
            "received_at",
            "updated_at",
            "processed_at",
            "failed_at",
            "attempts",
            "redrives",
        },
    ),
)

SENSITIVE_FIELDS = {
    "payload",
    "idempotency_key",
    "customer_id",
    "source_message_id",
    "provider_connection_id",
    "connection_id",
    "provider_operation_id",
    "provider_reference",
    "raw_body_sha256",
    "signature",
    "secret",
    "safe_error_message",
    "last_error_message",
    "lease_id",
    "lease_owner",
    "worker_id",
}


@pytest.mark.parametrize(("model", "whitelist"), RESPONSE_FIELD_WHITELISTS)
def test_response_models_have_exact_strict_field_whitelists(
    model: type[BaseModel],
    whitelist: set[str],
) -> None:
    assert set(model.model_fields) == whitelist
    assert model.model_config.get("extra") == "forbid"
    assert whitelist.isdisjoint(SENSITIVE_FIELDS)


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (ProviderOutboxDetail, _outbox_detail_values()),
        (ProviderInboxDetail, _inbox_detail_values()),
    ],
)
def test_detail_models_reject_sensitive_persistence_fields(
    model: type[BaseModel],
    values: dict[str, Any],
) -> None:
    for field in SENSITIVE_FIELDS:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            model.model_validate({**values, field: f"sensitive-{field}"})


def test_serialized_details_contain_no_sensitive_fields_or_values() -> None:
    payload = {
        "outbox": ProviderOutboxDetail.model_validate(
            _outbox_detail_values()
        ).model_dump(mode="json"),
        "inbox": ProviderInboxDetail.model_validate(_inbox_detail_values()).model_dump(
            mode="json"
        ),
    }
    serialized = json.dumps(payload, sort_keys=True)

    assert all(field not in serialized for field in SENSITIVE_FIELDS)
    assert "sensitive-provider-token" not in serialized
    assert "raw provider rejection message" not in serialized


def test_queue_overview_is_a_strict_safe_aggregate() -> None:
    overview = ProviderQueueOverview(
        outbox=(
            ProviderOutboxQueueSummary(
                status="dead",
                count=2,
                oldest_available_at=NOW,
            ),
        ),
        inbox=(ProviderInboxQueueSummary(status="failed", count=1),),
        generated_at=NOW,
    )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProviderQueueOverview.model_validate(
            {**overview.model_dump(), "tenant_id": "tenant-demo"}
        )


def test_redrive_request_normalizes_safe_request_id_and_fixed_reason() -> None:
    request = ProviderRedriveRequest(
        request_id="  ops:req-101  ",
        reason_code="dependency_or_configuration_restored",
    )

    assert request.request_id == "ops:req-101"
    assert (
        request.reason_code
        is ProviderRedriveReasonCode.DEPENDENCY_OR_CONFIGURATION_RESTORED
    )
    assert request.model_dump(mode="json") == {
        "request_id": "ops:req-101",
        "reason_code": "dependency_or_configuration_restored",
    }


def test_redrive_reason_codes_are_closed_and_deterministic() -> None:
    assert {reason.value for reason in ProviderRedriveReasonCode} == {
        "dependency_or_configuration_restored",
        "transient_incident_resolved",
        "manual_retry_approved",
    }

    with pytest.raises(ValidationError):
        ProviderRedriveRequest(
            request_id="ops:req-101",
            reason_code="operator_wrote_a_free_form_reason",
        )


@pytest.mark.parametrize(
    "request_id",
    ["", " ", "a" * 129, "contains spaces", "!starts-with-punctuation"],
)
def test_redrive_request_id_is_bounded_and_identifier_shaped(
    request_id: str,
) -> None:
    with pytest.raises(ValidationError):
        ProviderRedriveRequest(
            request_id=request_id,
            reason_code="manual_retry_approved",
        )


def test_redrive_request_rejects_free_form_reason_and_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProviderRedriveRequest.model_validate(
            {
                "request_id": "ops:req-101",
                "reason_code": "manual_retry_approved",
                "reason": "dependency fixed; retry this payload",
            }
        )


def test_redrive_view_requires_exactly_one_new_cycle() -> None:
    with pytest.raises(ValidationError, match="new_cycle must equal"):
        ProviderRedriveView(
            request_id="ops:req-101",
            reason_code="manual_retry_approved",
            actor="tenant-demo:supervisor-1",
            previous_cycle=1,
            new_cycle=3,
            created_at=NOW,
        )


def test_legacy_redrive_view_is_unclassified_without_exposing_legacy_reason() -> None:
    view = ProviderRedriveView(
        request_id="legacy-request",
        reason_code=None,
        actor="tenant-demo:supervisor-1",
        previous_cycle=1,
        new_cycle=2,
        created_at=NOW,
    )

    assert view.reason_code is None
    assert "reason" not in view.model_dump()
