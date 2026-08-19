"""Unit tests for v0.7 case model additions (provider_update, delivery order)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent.cases.models import SupportCase, SupportCaseEvent
from agent.integrations.models import ProviderCommandEnvelope
from tests.fakes.identity import make_scope
from tests.support_cases import InMemoryCaseRepository

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
CASE_ID = UUID("22222222-2222-2222-2222-222222222222")
COMMAND_ID = UUID("33333333-3333-3333-3333-333333333333")
SCOPE = make_scope("customer")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _case(
    *, case_type: str = "delivery_investigation", order_id: str | None = "ORD-10010"
) -> SupportCase:
    return SupportCase(
        case_id=CASE_ID,
        thread_id="thread-1",
        source_message_id="message-1",
        order_id=order_id,
        case_type=case_type,
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


def _provider_update_event(**overrides: object) -> SupportCaseEvent:
    values: dict[str, object] = {
        "event_id": uuid4(),
        "idempotency_key": "provider-update:1",
        "case_id": CASE_ID,
        "event_type": "provider_update",
        "provider_command_id": COMMAND_ID,
        "provider_command_status": "completed",
        "provider_reference": "provider-ref-1",
        "actor": "system",
        "customer_id": "customer-a",
        "tenant_id": "tenant-demo",
        "created_at": NOW,
    }
    values.update(overrides)
    return SupportCaseEvent.model_validate(values)


def test_delivery_investigation_case_requires_order_id() -> None:
    with pytest.raises(ValidationError, match="order_id"):
        _case(order_id=None)


def test_non_delivery_case_may_omit_order_id() -> None:
    case = _case(case_type="safety_review", order_id=None)

    assert case.case_type == "safety_review"
    assert case.order_id is None


def test_provider_update_event_is_valid() -> None:
    event = _provider_update_event()

    assert event.provider_command_id == COMMAND_ID
    assert event.provider_command_status == "completed"
    assert event.actor == "system"
    assert event.previous_status is None
    assert event.current_status is None


def test_provider_update_requires_command_id() -> None:
    with pytest.raises(ValidationError, match="provider_command_id"):
        _provider_update_event(provider_command_id=None)


def test_provider_update_requires_command_status() -> None:
    with pytest.raises(ValidationError, match="provider_command_status"):
        _provider_update_event(provider_command_status=None)


def test_provider_update_requires_actor() -> None:
    with pytest.raises(ValidationError, match="actor"):
        _provider_update_event(actor=None)


def test_provider_update_must_not_carry_case_status_fields() -> None:
    with pytest.raises(ValidationError, match="status fields"):
        _provider_update_event(previous_status="open", current_status="resolved")


def test_provider_update_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError, match="provider_command_status"):
        _provider_update_event(provider_command_status="manual_review")


def test_provider_delivery_failed_reason_is_a_valid_handoff_reason() -> None:
    event = _provider_update_event(
        reason_codes=("provider_delivery_failed",),
    )

    assert "provider_delivery_failed" in event.reason_codes


def _delivery_command(case: SupportCase) -> ProviderCommandEnvelope:
    return ProviderCommandEnvelope.for_delivery_investigation(
        case=case,
        issue_type="tracking_stalled",
        connection_id="conn-1",
        command_id=uuid4(),
        created_at=NOW,
    )


def _created_event(case: SupportCase) -> SupportCaseEvent:
    return SupportCaseEvent(
        event_id=uuid4(),
        idempotency_key=f"message:{case.thread_id}:message-1",
        case_id=case.case_id,
        event_type="case_created",
        source_message_id="message-1",
        order_id=case.order_id,
        reason_codes=("delivery_tracking_stalled",),
        triggering_message_excerpt="Tracking has not updated.",
        current_priority="p1",
        current_status="open",
        actor="system",
        customer_id="customer-a",
        tenant_id=case.tenant_id,
        created_at=NOW,
    )


async def test_case_atomic_write_rejects_mismatched_aggregate() -> None:
    repository = InMemoryCaseRepository()
    case = _case()
    event = _created_event(case)
    other_id = uuid4()
    # Keep the tampered envelope internally valid (idempotency key follows the
    # new aggregate id) so the association check itself is exercised.
    mismatched = _delivery_command(case).model_copy(
        update={
            "aggregate_id": other_id,
            "idempotency_key": f"delivery-investigation:{other_id}",
        }
    )

    with pytest.raises(ValueError, match="aggregate_id"):
        await repository.create_case_with_event_and_command(
            SCOPE,
            case=case,
            event=event,
            command=mismatched,
        )
    assert repository.outbox_commands == {}
    assert case.case_id not in repository.cases


async def test_case_atomic_write_rejects_mismatched_tenant() -> None:
    repository = InMemoryCaseRepository()
    case = _case()
    event = _created_event(case)
    mismatched = _delivery_command(case).model_copy(
        update={"tenant_id": "tenant-other"}
    )

    with pytest.raises(ValueError, match="tenant_id"):
        await repository.create_case_with_event_and_command(
            SCOPE,
            case=case,
            event=event,
            command=mismatched,
        )
    assert repository.outbox_commands == {}


async def test_case_atomic_write_rejects_mismatched_payload_order() -> None:
    repository = InMemoryCaseRepository()
    case = _case()
    event = _created_event(case)
    command = _delivery_command(case)
    mismatched = command.model_copy(
        update={"payload": command.payload.model_copy(update={"order_id": "ORD-99999"})}
    )

    with pytest.raises(ValueError, match="order_id"):
        await repository.create_case_with_event_and_command(
            SCOPE,
            case=case,
            event=event,
            command=mismatched,
        )
    assert repository.outbox_commands == {}


async def test_case_atomic_write_rejects_mismatched_event_case_id() -> None:
    repository = InMemoryCaseRepository()
    case = _case()
    mismatched_event = _created_event(case).model_copy(update={"case_id": uuid4()})

    with pytest.raises(ValueError, match="event.case_id"):
        await repository.create_case_with_event_and_command(
            SCOPE,
            case=case,
            event=mismatched_event,
            command=_delivery_command(case),
        )
    assert repository.outbox_commands == {}
    assert case.case_id not in repository.cases


async def test_case_atomic_write_rejects_tampered_idempotency_key() -> None:
    repository = InMemoryCaseRepository()
    case = _case()
    tampered = _delivery_command(case).model_copy(
        update={
            "idempotency_key": "delivery-investigation:00000000-0000-0000-0000-000000000000"
        }
    )

    with pytest.raises(ValueError, match="command envelope is invalid"):
        await repository.create_case_with_event_and_command(
            SCOPE,
            case=case,
            event=_created_event(case),
            command=tampered,
        )
    assert repository.outbox_commands == {}
    assert case.case_id not in repository.cases


async def test_case_atomic_write_rejects_order_payload_in_delivery_command() -> None:
    from agent.integrations.models import OrderOperationCommandPayload

    repository = InMemoryCaseRepository()
    case = _case()
    tampered = _delivery_command(case).model_copy(
        update={
            "payload": OrderOperationCommandPayload(
                order_id="ORD-10010",
                operation_type="return",
                reason="damaged_item",
            )
        }
    )

    with pytest.raises(ValueError, match="command envelope is invalid"):
        await repository.create_case_with_event_and_command(
            SCOPE,
            case=case,
            event=_created_event(case),
            command=tampered,
        )
    assert repository.outbox_commands == {}
    assert case.case_id not in repository.cases


async def test_case_atomic_write_rejects_tampered_command_type() -> None:
    repository = InMemoryCaseRepository()
    case = _case()
    tampered = _delivery_command(case).model_copy(
        update={"command_type": "return_order"}
    )

    with pytest.raises(ValueError, match="command envelope is invalid"):
        await repository.create_case_with_event_and_command(
            SCOPE,
            case=case,
            event=_created_event(case),
            command=tampered,
        )
    assert repository.outbox_commands == {}


def test_provider_redrive_case_event_is_fixed_and_payload_free() -> None:
    event = SupportCaseEvent(
        event_id=uuid4(),
        idempotency_key="provider-redrive:tenant-demo:redrive-1",
        case_id=uuid4(),
        event_type="provider_redrive",
        provider_command_id=uuid4(),
        provider_redrive_reason_code="dependency_or_configuration_restored",
        actor="tenant-demo:supervisor-a",
        customer_id="customer-a",
        tenant_id="tenant-demo",
        created_at=NOW,
    )

    assert event.provider_reference is None
    assert event.provider_command_status is None
    assert "payload" not in SupportCaseEvent.model_fields

    with pytest.raises(ValidationError, match="provider result fields"):
        SupportCaseEvent.model_validate(
            {**event.model_dump(), "provider_reference": "provider-ref-sensitive"}
        )

    with pytest.raises(ValidationError, match="only provider_redrive"):
        SupportCaseEvent.model_validate(
            {
                **_created_event(_case()).model_dump(),
                "provider_redrive_reason_code": "manual_retry_approved",
            }
        )
