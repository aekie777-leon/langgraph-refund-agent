"""Unit tests for per-order delivery-investigation case isolation."""

from datetime import UTC, datetime

import pytest

from agent.cases.models import CaseTrigger, HandoffPolicyInput
from agent.cases.policy import determine_handoff_policy
from agent.cases.service import CaseService
from tests.fakes.identity import make_scope
from tests.support_cases import InMemoryCaseRepository

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
SCOPE = make_scope("customer")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _delivery_decision():
    return determine_handoff_policy(
        HandoffPolicyInput(
            domain_case_reason_codes=("delivery_tracking_stalled",),
        )
    )


def _delivery_trigger(message_id: str, order_id: str) -> CaseTrigger:
    return CaseTrigger(
        thread_id="thread-1",
        source_message_id=message_id,
        order_id=order_id,
        risk_level=None,
        risk_categories=(),
        triggering_message_excerpt="Tracking has not updated.",
    )


async def test_same_thread_same_order_reuses_delivery_case() -> None:
    service = CaseService(InMemoryCaseRepository(), clock=lambda: NOW)

    first = await service.record_handoff(
        SCOPE,
        trigger=_delivery_trigger("message-1", "ORD-10010"),
        decision=_delivery_decision(),
    )
    second = await service.record_handoff(
        SCOPE,
        trigger=_delivery_trigger("message-2", "ORD-10010"),
        decision=_delivery_decision(),
    )

    assert first.case is not None
    assert second.case is not None
    assert second.case.case_id == first.case.case_id
    assert second.action == "event_appended"


async def test_same_thread_different_order_creates_different_delivery_cases() -> None:
    service = CaseService(InMemoryCaseRepository(), clock=lambda: NOW)

    first = await service.record_handoff(
        SCOPE,
        trigger=_delivery_trigger("message-1", "ORD-10010"),
        decision=_delivery_decision(),
    )
    second = await service.record_handoff(
        SCOPE,
        trigger=_delivery_trigger("message-2", "ORD-10011"),
        decision=_delivery_decision(),
    )

    assert first.case is not None
    assert second.case is not None
    assert second.case.case_id != first.case.case_id
    assert second.action == "created"


async def test_different_tenants_do_not_conflict() -> None:
    repository = InMemoryCaseRepository()
    service = CaseService(repository, clock=lambda: NOW)
    scope_a = make_scope("customer", tenant_id="tenant-a")
    scope_b = make_scope("customer", tenant_id="tenant-b")

    first = await service.record_handoff(
        scope_a,
        trigger=_delivery_trigger("message-1", "ORD-10010"),
        decision=_delivery_decision(),
    )
    second = await service.record_handoff(
        scope_b,
        trigger=_delivery_trigger("message-2", "ORD-10010"),
        decision=_delivery_decision(),
    )

    assert first.action == "created"
    assert second.action == "created"
    assert first.case is not None
    assert second.case is not None
    assert first.case.case_id != second.case.case_id
    assert len(repository.cases) == 2


async def test_normal_case_still_reuses_by_thread_and_type() -> None:
    service = CaseService(InMemoryCaseRepository(), clock=lambda: NOW)
    decision = determine_handoff_policy(
        HandoffPolicyInput(
            semantic_risk_level="medium",
            semantic_risk_categories=("self_harm",),
        )
    )

    first = await service.record_handoff(
        SCOPE,
        trigger=CaseTrigger(
            thread_id="thread-1",
            source_message_id="message-1",
            order_id="ORD-10001",
            risk_level="medium",
            risk_categories=("self_harm",),
            triggering_message_excerpt="I need urgent help.",
        ),
        decision=decision,
    )
    second = await service.record_handoff(
        SCOPE,
        trigger=CaseTrigger(
            thread_id="thread-1",
            source_message_id="message-2",
            order_id="ORD-10002",
            risk_level="medium",
            risk_categories=("self_harm",),
            triggering_message_excerpt="Still need urgent help.",
        ),
        decision=decision,
    )

    assert first.case is not None
    assert second.case is not None
    assert second.case.case_id == first.case.case_id
    assert second.action == "event_appended"
