"""Unit tests for provider integration domain models."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent.cases.models import SupportCase
from agent.integrations.models import (
    INBOX_PROCESSING_TRANSITIONS,
    OUTBOX_DELIVERY_TRANSITIONS,
    DeliveryInvestigationCommandPayload,
    OrderOperationCommandPayload,
    ProviderAuthentication,
    ProviderCommandEnvelope,
    ProviderCommandResult,
    ProviderConnection,
    ProviderTimeout,
    ProviderWebhookConnection,
    ProviderWebhookEnvelope,
    ProviderWebhookEventData,
    map_provider_command_status,
)
from agent.operations.models import OrderOperation

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
OPERATION_ID = UUID("11111111-1111-1111-1111-111111111111")
CASE_ID = UUID("22222222-2222-2222-2222-222222222222")


def _auth(scheme: str = "bearer") -> ProviderAuthentication:
    if scheme == "api_key":
        return ProviderAuthentication(
            scheme="api_key",
            credential="hunter2",
            api_key_header="X-Api-Key",
        )
    if scheme == "none":
        return ProviderAuthentication(scheme="none")
    return ProviderAuthentication(scheme="bearer", credential="hunter2")


def _connection(
    *,
    capability: str = "order_query",
    connection_id: str = "conn-1",
    base_url: str = "https://provider.example.com:8443",
    endpoint: str = "/orders",
    requests_per_second: float | None = None,
) -> ProviderConnection:
    return ProviderConnection(
        connection_id=connection_id,
        tenant_id="tenant-demo",
        capability=capability,
        base_url=base_url,
        endpoint=endpoint,
        authentication=_auth(),
        requests_per_second=requests_per_second,
    )


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
        status="pending_confirmation",
        created_at=NOW,
        updated_at=NOW,
        customer_id="customer-a",
        tenant_id="tenant-demo",
        created_by="tenant-demo:customer-a",
    )


def _delivery_case() -> SupportCase:
    return SupportCase(
        case_id=CASE_ID,
        thread_id="thread-1",
        source_message_id="message-9",
        order_id="ORD-10010",
        case_type="delivery_investigation",
        priority="p1",
        status="open",
        risk_level=None,
        risk_categories=(),
        reason_codes=("delivery_tracking_stalled",),
        display_reason="Tracking has not updated for 72 hours.",
        triggering_message_excerpt="Tracking has not updated.",
        created_at=NOW,
        updated_at=NOW,
        version=1,
        customer_id="customer-a",
        tenant_id="tenant-demo",
        created_by="tenant-demo:customer-a",
    )


def _envelope(**overrides: object) -> ProviderCommandEnvelope:
    values: dict[str, object] = {
        "command_id": uuid4(),
        "idempotency_key": "order-operation:11111111-1111-1111-1111-111111111111",
        "source_message_id": "message-1",
        "aggregate_type": "order_operation",
        "aggregate_id": OPERATION_ID,
        "expected_order_version": 3,
        "tenant_id": "tenant-demo",
        "customer_id": "customer-a",
        "connection_id": "conn-1",
        "command_type": "return_order",
        "payload": OrderOperationCommandPayload(
            order_id="ORD-10001",
            operation_type="return",
            reason="damaged_item",
        ),
        "created_at": NOW,
    }
    values.update(overrides)
    return ProviderCommandEnvelope.model_validate(values)


def _webhook_data(**overrides: object) -> ProviderWebhookEventData:
    values: dict[str, object] = {
        "command_id": uuid4(),
        "aggregate_type": "order_operation",
        "aggregate_id": OPERATION_ID,
        "command_status": "processing",
        "provider_operation_id": "provider-op-1",
        "provider_reference": "ref-1",
        "order_id": "ORD-10001",
        "occurred_at": NOW,
    }
    values.update(overrides)
    return ProviderWebhookEventData.model_validate(values)


def _webhook_envelope(
    *,
    timestamp: datetime = NOW,
    data: ProviderWebhookEventData | None = None,
    **overrides: object,
) -> ProviderWebhookEnvelope:
    values: dict[str, object] = {
        "event_id": "evt-1",
        "provider_connection_id": "wc-1",
        "tenant_id": "tenant-demo",
        "timestamp": timestamp,
        "event_type": "provider_command_status_changed",
        "data": data if data is not None else _webhook_data(),
    }
    values.update(overrides)
    return ProviderWebhookEnvelope.model_validate(values)


def test_invalid_capability_is_rejected() -> None:
    with pytest.raises(ValidationError, match="capability"):
        _connection(capability="bogus")


def test_invalid_base_url_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _connection(base_url="not-a-url")


def test_endpoint_must_be_an_absolute_path() -> None:
    with pytest.raises(ValidationError, match="endpoint"):
        _connection(endpoint="orders")


def test_empty_tenant_and_connection_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _connection(connection_id="")
    with pytest.raises(ValidationError):
        ProviderConnection(
            connection_id="conn-1",
            tenant_id="",
            capability="order_query",
            base_url="https://provider.example.com",
            endpoint="/orders",
            authentication=_auth(),
        )


def test_secret_never_appears_in_repr_or_serialization() -> None:
    connection = _connection()

    assert "hunter2" not in repr(connection)
    assert "hunter2" not in str(connection)
    assert "hunter2" not in str(connection.model_dump())
    assert "hunter2" not in connection.model_dump_json()


def test_multiple_capabilities_can_share_a_connection_id() -> None:
    query = _connection(capability="order_query", connection_id="conn-1")
    operations = _connection(
        capability="order_operation",
        connection_id="conn-1",
        endpoint="/commands",
    )

    assert query.connection_id == operations.connection_id == "conn-1"
    assert query.capability != operations.capability


def test_different_capabilities_can_map_to_different_urls_and_ports() -> None:
    query = _connection(
        capability="order_query",
        connection_id="conn-1",
        base_url="https://provider.example.com:8443",
        endpoint="/orders",
    )
    inventory = _connection(
        capability="inventory_query",
        connection_id="conn-2",
        base_url="https://inventory.example.com:9443",
        endpoint="/stock",
    )

    assert str(query.base_url) != str(inventory.base_url)
    assert query.endpoint != inventory.endpoint


def test_connection_carries_shared_pool_hints() -> None:
    connection = _connection()

    assert connection.max_concurrency >= 1
    assert isinstance(connection.timeout, ProviderTimeout)
    assert connection.authentication.scheme == "bearer"
    assert connection.authentication.credential.get_secret_value() == "hunter2"


def test_webhook_connection_masks_signing_secret() -> None:
    connection = ProviderWebhookConnection(
        connection_id="wc-1",
        tenant_id="tenant-demo",
        signing_secret="wh-secret",
    )

    assert "wh-secret" not in repr(connection)
    assert "wh-secret" not in str(connection)
    assert "wh-secret" not in str(connection.model_dump())
    assert "wh-secret" not in connection.model_dump_json()
    assert connection.signing_secret.get_secret_value() == "wh-secret"


def test_webhook_connection_rejects_empty_signing_secret() -> None:
    with pytest.raises(ValidationError, match="signing_secret"):
        ProviderWebhookConnection(
            connection_id="wc-1",
            tenant_id="tenant-demo",
            signing_secret="",
        )


def test_bearer_auth_rejects_empty_credential() -> None:
    with pytest.raises(ValidationError, match="credential"):
        ProviderAuthentication(scheme="bearer", credential="")


def test_api_key_auth_rejects_empty_credential() -> None:
    with pytest.raises(ValidationError, match="credential"):
        ProviderAuthentication(
            scheme="api_key",
            credential="",
            api_key_header="X-Api-Key",
        )


def test_validation_errors_never_contain_raw_secret() -> None:
    builders = [
        lambda: ProviderAuthentication(scheme="none", credential="hunter2"),
        lambda: ProviderAuthentication(
            scheme="bearer", credential="hunter2", api_key_header="X-Api-Key"
        ),
        lambda: ProviderAuthentication(scheme="bogus", credential="hunter2"),
        lambda: ProviderWebhookConnection(
            connection_id="wc-1",
            tenant_id="tenant-demo",
            signing_secret="wh-secret",
            validity_window_seconds=0,
        ),
    ]

    for builder in builders:
        with pytest.raises(ValidationError) as error:
            builder()
        rendered = str(error.value)
        assert "hunter2" not in rendered
        assert "wh-secret" not in rendered


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_timeout_config_rejects_non_finite_values(value: float) -> None:
    with pytest.raises(ValidationError):
        ProviderTimeout(connect_seconds=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_webhook_connection_rejects_non_finite_window(value: float) -> None:
    with pytest.raises(ValidationError):
        ProviderWebhookConnection(
            connection_id="wc-1",
            tenant_id="tenant-demo",
            signing_secret="wh-secret",
            validity_window_seconds=value,
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_connection_rejects_non_finite_rate_limit(value: float) -> None:
    with pytest.raises(ValidationError):
        _connection(requests_per_second=value)


def test_command_envelope_schema_version_and_typed_payload() -> None:
    envelope = ProviderCommandEnvelope.for_order_operation(
        operation=_operation(),
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )

    assert envelope.schema_version == 1
    assert envelope.command_type == "return_order"
    assert envelope.source_message_id == "message-1"
    assert envelope.aggregate_type == "order_operation"
    assert envelope.aggregate_id == OPERATION_ID
    assert envelope.expected_order_version == 3
    assert envelope.tenant_id == "tenant-demo"
    assert envelope.customer_id == "customer-a"
    assert isinstance(envelope.payload, OrderOperationCommandPayload)
    assert envelope.payload.order_id == "ORD-10001"
    assert envelope.payload.operation_type == "return"
    assert envelope.payload.reason == "damaged_item"


def test_command_envelope_idempotency_key_is_stable_and_bound_to_operation() -> None:
    first = ProviderCommandEnvelope.for_order_operation(
        operation=_operation(),
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )
    second = ProviderCommandEnvelope.for_order_operation(
        operation=_operation(),
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )

    assert first.idempotency_key == second.idempotency_key
    assert first.idempotency_key == "order-operation:11111111-1111-1111-1111-111111111111"


def test_delivery_investigation_envelope_is_valid_with_support_case() -> None:
    envelope = ProviderCommandEnvelope.for_delivery_investigation(
        case=_delivery_case(),
        issue_type="tracking_stalled",
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )

    assert envelope.schema_version == 1
    assert envelope.command_type == "delivery_investigation"
    assert envelope.source_message_id == "message-9"
    assert envelope.aggregate_type == "support_case"
    assert envelope.aggregate_id == CASE_ID
    assert envelope.expected_order_version is None
    assert envelope.idempotency_key == "delivery-investigation:22222222-2222-2222-2222-222222222222"
    assert isinstance(envelope.payload, DeliveryInvestigationCommandPayload)
    assert envelope.payload.order_id == "ORD-10010"
    assert envelope.payload.issue_type == "tracking_stalled"


def test_delivery_investigation_idempotency_key_is_stable() -> None:
    first = ProviderCommandEnvelope.for_delivery_investigation(
        case=_delivery_case(),
        issue_type="tracking_stalled",
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )
    second = ProviderCommandEnvelope.for_delivery_investigation(
        case=_delivery_case(),
        issue_type="tracking_stalled",
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )

    assert first.idempotency_key == second.idempotency_key == (
        "delivery-investigation:22222222-2222-2222-2222-222222222222"
    )


def test_delivery_investigation_rejects_order_operation_aggregate() -> None:
    with pytest.raises(ValidationError, match="aggregate_type"):
        _envelope(
            command_type="delivery_investigation",
            aggregate_type="order_operation",
            payload=DeliveryInvestigationCommandPayload(
                order_id="ORD-10010",
                issue_type="tracking_stalled",
            ),
        )


def test_delivery_investigation_rejects_expected_order_version() -> None:
    with pytest.raises(ValidationError, match="expected_order_version"):
        _envelope(
            command_type="delivery_investigation",
            aggregate_type="support_case",
            aggregate_id=CASE_ID,
            expected_order_version=1,
            idempotency_key="delivery-investigation:22222222-2222-2222-2222-222222222222",
            payload=DeliveryInvestigationCommandPayload(
                order_id="ORD-10010",
                issue_type="tracking_stalled",
            ),
        )


def test_order_operation_rejects_support_case_aggregate() -> None:
    with pytest.raises(ValidationError, match="aggregate_type"):
        _envelope(
            aggregate_type="support_case",
            aggregate_id=CASE_ID,
            idempotency_key="order-operation:11111111-1111-1111-1111-111111111111",
        )


def test_order_operation_rejects_missing_expected_order_version() -> None:
    with pytest.raises(ValidationError, match="expected_order_version"):
        _envelope(expected_order_version=None)


def test_envelope_rejects_non_uuid_aggregate_id() -> None:
    with pytest.raises(ValidationError, match="aggregate_id"):
        _envelope(aggregate_id="not-a-uuid")


def test_envelope_rejects_blank_source_message_id() -> None:
    with pytest.raises(ValidationError, match="source_message_id"):
        _envelope(source_message_id="   ")


def test_envelope_rejects_mismatched_idempotency_key() -> None:
    with pytest.raises(ValidationError, match="idempotency_key"):
        _envelope(idempotency_key="order-operation:99999999-9999-9999-9999-999999999999")


def test_command_envelope_rejects_non_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        _envelope(schema_version=2)


def test_command_result_rejects_non_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        ProviderCommandResult(
            command_id=uuid4(),
            status="accepted",
            received_at=NOW,
            schema_version=2,
        )


def test_webhook_envelope_rejects_non_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        _webhook_envelope(schema_version=2)


def test_webhook_envelope_rejects_unknown_event_type() -> None:
    with pytest.raises(ValidationError, match="event_type"):
        _webhook_envelope(event_type="order_operation_status_changed")


def test_command_envelope_normalizes_timestamps_to_utc() -> None:
    local = datetime(2026, 8, 17, 20, 0, tzinfo=timezone(timedelta(hours=8)))

    envelope = _envelope(created_at=local)

    assert envelope.created_at == datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def test_command_envelope_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="created_at"):
        _envelope(created_at=datetime(2026, 8, 17, 12, 0))


def test_command_envelope_rejects_invalid_command_type() -> None:
    with pytest.raises(ValidationError, match="command_type"):
        _envelope(command_type="bogus")


def test_command_envelope_rejects_command_type_operation_mismatch() -> None:
    with pytest.raises(ValidationError, match="operation_type"):
        _envelope(
            command_type="return_order",
            payload=OrderOperationCommandPayload(
                order_id="ORD-10001",
                operation_type="cancellation",
                reason="no_longer_needed",
            ),
        )


def test_delivery_investigation_requires_delivery_payload() -> None:
    with pytest.raises(ValidationError, match="delivery payload"):
        _envelope(
            command_type="delivery_investigation",
            aggregate_type="support_case",
            aggregate_id=CASE_ID,
            expected_order_version=None,
            idempotency_key="delivery-investigation:22222222-2222-2222-2222-222222222222",
        )


def test_delivery_investigation_payload_has_no_redundant_flag() -> None:
    DeliveryInvestigationCommandPayload(
        order_id="ORD-10010",
        issue_type="tracking_stalled",
    )

    assert "investigation_requested" not in DeliveryInvestigationCommandPayload.model_fields


def test_provider_command_status_mapping_is_deterministic() -> None:
    assert map_provider_command_status("accepted") == "submitted"
    assert map_provider_command_status("processing") == "processing"
    assert map_provider_command_status("completed") == "completed"
    assert map_provider_command_status("rejected") == "rejected"


def test_order_operation_webhook_event_is_valid() -> None:
    envelope = _webhook_envelope()

    assert envelope.event_type == "provider_command_status_changed"
    assert envelope.data.aggregate_type == "order_operation"
    assert envelope.data.aggregate_id == OPERATION_ID
    assert envelope.data.command_status == "processing"
    assert envelope.data.command_id is not None


def test_support_case_webhook_event_is_valid() -> None:
    envelope = _webhook_envelope(
        data=_webhook_data(
            aggregate_type="support_case",
            aggregate_id=CASE_ID,
            command_status="accepted",
            order_id=None,
        )
    )

    assert envelope.data.aggregate_type == "support_case"
    assert envelope.data.aggregate_id == CASE_ID
    assert envelope.data.command_status == "accepted"
    assert envelope.data.order_id is None


def test_webhook_rejects_non_uuid_command_id() -> None:
    with pytest.raises(ValidationError, match="command_id"):
        _webhook_data(command_id="not-a-uuid")


def test_webhook_rejects_non_uuid_aggregate_id() -> None:
    with pytest.raises(ValidationError, match="aggregate_id"):
        _webhook_data(aggregate_id="not-a-uuid")


def test_webhook_rejects_local_operation_statuses() -> None:
    with pytest.raises(ValidationError, match="command_status"):
        _webhook_data(command_status="manual_review")


def test_webhook_does_not_rely_on_order_id_for_association() -> None:
    data = _webhook_data(order_id=None)

    assert data.order_id is None
    assert data.command_id is not None
    assert data.aggregate_id is not None


def test_webhook_event_timestamp_is_normalized_to_utc() -> None:
    local = datetime(2026, 8, 17, 20, 0, tzinfo=timezone(timedelta(hours=8)))

    envelope = _webhook_envelope(timestamp=local)

    assert envelope.timestamp == datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def test_command_result_rejects_confirmed_status() -> None:
    with pytest.raises(ValidationError, match="status"):
        ProviderCommandResult(
            command_id=uuid4(),
            status="confirmed",
            received_at=NOW,
        )


def test_outbox_and_inbox_transition_maps() -> None:
    assert "processing" in OUTBOX_DELIVERY_TRANSITIONS["pending"]
    assert "published" in OUTBOX_DELIVERY_TRANSITIONS["processing"]
    assert "retry_scheduled" in OUTBOX_DELIVERY_TRANSITIONS["processing"]
    assert "dead" in OUTBOX_DELIVERY_TRANSITIONS["processing"]
    assert OUTBOX_DELIVERY_TRANSITIONS["published"] == frozenset()

    assert "processing" in INBOX_PROCESSING_TRANSITIONS["received"]
    assert "processed" in INBOX_PROCESSING_TRANSITIONS["processing"]
    assert "received" in INBOX_PROCESSING_TRANSITIONS["processing"]
    assert "failed" in INBOX_PROCESSING_TRANSITIONS["processing"]
    assert INBOX_PROCESSING_TRANSITIONS["processed"] == frozenset()
    assert INBOX_PROCESSING_TRANSITIONS["failed"] == frozenset()
