"""Unit tests for support-case application service behavior."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from agent.cases.models import (
    CaseListQuery,
    CaseTrigger,
    HandoffDecision,
    HandoffPolicyInput,
    SupportCase,
    SupportCaseEvent,
    SupportCaseEventPage,
    SupportCasePage,
)
from agent.cases.policy import (
    InvalidCaseStatusTransition,
    determine_handoff_policy,
)
from agent.cases.repository import (
    ActiveCaseConflictError,
    CaseNotFoundError,
    ConcurrentCaseUpdateError,
    DuplicateIdempotencyKeyError,
    DuplicateSourceMessageError,
)
from agent.cases.service import CaseService

NOW = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
UNRESOLVED_STATUSES = {"open", "in_progress", "on_hold"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _InMemoryCaseRepository:
    """Enforce the repository contract without external infrastructure."""

    def __init__(self) -> None:
        self.cases: dict[UUID, SupportCase] = {}
        self.events: list[SupportCaseEvent] = []

    async def get_case(self, case_id: UUID) -> SupportCase | None:
        return self.cases.get(case_id)

    async def find_by_source_message(
        self,
        *,
        thread_id: str,
        source_message_id: str,
    ) -> SupportCase | None:
        for event in self.events:
            if event.source_message_id != source_message_id:
                continue
            case = self.cases[event.case_id]
            if case.thread_id == thread_id:
                return case
        return None

    async def find_event_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> SupportCaseEvent | None:
        return next(
            (
                event
                for event in self.events
                if event.idempotency_key == idempotency_key
            ),
            None,
        )

    async def find_unresolved_case(
        self,
        *,
        thread_id: str,
        case_type,
    ) -> SupportCase | None:
        return next(
            (
                case
                for case in self.cases.values()
                if case.thread_id == thread_id
                and case.case_type == case_type
                and case.status in UNRESOLVED_STATUSES
            ),
            None,
        )

    async def list_cases(self, query: CaseListQuery) -> SupportCasePage:
        items = tuple(self.cases.values())
        return SupportCasePage(
            items=items[query.offset : query.offset + query.limit],
            total=len(items),
            limit=query.limit,
            offset=query.offset,
        )

    async def list_case_events(
        self,
        *,
        case_id: UUID,
        limit: int,
        offset: int,
    ) -> SupportCaseEventPage:
        items = tuple(event for event in self.events if event.case_id == case_id)
        return SupportCaseEventPage(
            items=items[offset : offset + limit],
            total=len(items),
            limit=limit,
            offset=offset,
        )

    async def create_case_with_event(
        self,
        *,
        case: SupportCase,
        event: SupportCaseEvent,
    ) -> None:
        if await self.find_event_by_idempotency_key(event.idempotency_key):
            raise DuplicateIdempotencyKeyError(event.idempotency_key)
        if event.source_message_id is not None and await self.find_by_source_message(
            thread_id=case.thread_id,
            source_message_id=event.source_message_id,
        ):
            raise DuplicateSourceMessageError(event.source_message_id)
        if await self.find_unresolved_case(
            thread_id=case.thread_id,
            case_type=case.case_type,
        ):
            raise ActiveCaseConflictError(case.thread_id)

        self.cases[case.case_id] = case
        self.events.append(event)

    async def update_case_with_event(
        self,
        *,
        case: SupportCase,
        event: SupportCaseEvent,
        expected_version: int,
    ) -> None:
        current = self.cases.get(case.case_id)
        if current is None:
            raise CaseNotFoundError(str(case.case_id))
        if current.version != expected_version:
            raise ConcurrentCaseUpdateError(str(case.case_id))
        if await self.find_event_by_idempotency_key(event.idempotency_key):
            raise DuplicateIdempotencyKeyError(event.idempotency_key)
        if event.source_message_id is not None and await self.find_by_source_message(
            thread_id=current.thread_id,
            source_message_id=event.source_message_id,
        ):
            raise DuplicateSourceMessageError(event.source_message_id)

        self.cases[case.case_id] = case
        self.events.append(event)


@pytest.fixture
def repository() -> _InMemoryCaseRepository:
    return _InMemoryCaseRepository()


@pytest.fixture
def service(repository: _InMemoryCaseRepository) -> CaseService:
    return CaseService(repository, clock=lambda: NOW)


def _trigger(
    message_id: str,
    *,
    thread_id: str = "thread-1",
    order_id: str | None = "ORD-10001",
    risk_level="medium",
    risk_categories=("self_harm",),
    excerpt: str = "Please help with this situation.",
) -> CaseTrigger:
    return CaseTrigger(
        thread_id=thread_id,
        source_message_id=message_id,
        order_id=order_id,
        risk_level=risk_level,
        risk_categories=risk_categories,
        triggering_message_excerpt=excerpt,
    )


def _medium_safety_decision() -> HandoffDecision:
    return determine_handoff_policy(
        HandoffPolicyInput(
            semantic_risk_level="medium",
            semantic_risk_categories=("self_harm",),
        )
    )


def _high_safety_decision() -> HandoffDecision:
    return determine_handoff_policy(
        HandoffPolicyInput(
            semantic_risk_level="high",
            semantic_risk_categories=("violence",),
        )
    )


@pytest.mark.anyio
async def test_negative_decision_does_not_write_repository(
    service: CaseService,
    repository: _InMemoryCaseRepository,
) -> None:
    result = await service.record_handoff(
        trigger=_trigger("message-1"),
        decision=HandoffDecision(should_create_case=False),
    )

    assert result.action == "not_created"
    assert result.case is None
    assert result.event is None
    assert repository.cases == {}
    assert repository.events == []


@pytest.mark.anyio
async def test_first_trigger_creates_case_and_initial_event(
    service: CaseService,
    repository: _InMemoryCaseRepository,
) -> None:
    result = await service.record_handoff(
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )

    assert result.action == "created"
    assert result.case is not None
    assert result.event is not None
    assert result.case.case_type == "safety_review"
    assert result.case.priority == "p2"
    assert result.case.status == "open"
    assert result.case.version == 1
    assert result.event.event_type == "case_created"
    assert result.event.source_message_id == "message-1"
    assert len(repository.cases) == 1
    assert len(repository.events) == 1


@pytest.mark.anyio
async def test_duplicate_source_message_is_ignored(
    service: CaseService,
    repository: _InMemoryCaseRepository,
) -> None:
    trigger = _trigger("message-1")
    decision = _medium_safety_decision()

    first = await service.record_handoff(trigger=trigger, decision=decision)
    duplicate = await service.record_handoff(trigger=trigger, decision=decision)

    assert duplicate.action == "duplicate_ignored"
    assert duplicate.case == first.case
    assert duplicate.event is None
    assert len(repository.cases) == 1
    assert len(repository.events) == 1


@pytest.mark.anyio
async def test_same_thread_and_type_appends_event_and_merges_summary(
    service: CaseService,
    repository: _InMemoryCaseRepository,
) -> None:
    first = await service.record_handoff(
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    second = await service.record_handoff(
        trigger=_trigger(
            "message-2",
            risk_level="high",
            risk_categories=("violence",),
            excerpt="I may hurt someone.",
        ),
        decision=_high_safety_decision(),
    )

    assert first.case is not None
    assert second.action == "event_appended"
    assert second.case is not None
    assert second.case.case_id == first.case.case_id
    assert second.case.priority == "p0"
    assert second.case.risk_level == "high"
    assert second.case.risk_categories == ("self_harm", "violence")
    assert second.case.reason_codes == (
        "semantic_medium_self_harm",
        "semantic_high_violence",
    )
    assert second.case.version == 2
    assert len(repository.cases) == 1
    assert len(repository.events) == 2


@pytest.mark.anyio
async def test_later_trigger_cannot_downgrade_priority(
    service: CaseService,
) -> None:
    await service.record_handoff(
        trigger=_trigger(
            "message-1",
            risk_level="high",
            risk_categories=("violence",),
        ),
        decision=_high_safety_decision(),
    )
    result = await service.record_handoff(
        trigger=_trigger("message-2"),
        decision=_medium_safety_decision(),
    )

    assert result.case is not None
    assert result.case.priority == "p0"
    assert result.case.risk_level == "high"


@pytest.mark.anyio
async def test_same_thread_with_different_type_creates_another_case(
    service: CaseService,
    repository: _InMemoryCaseRepository,
) -> None:
    await service.record_handoff(
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    refund_decision = determine_handoff_policy(
        HandoffPolicyInput(refund_requires_manual_review=True)
    )
    result = await service.record_handoff(
        trigger=_trigger(
            "message-2",
            risk_level=None,
            risk_categories=(),
            excerpt="Please review my refund.",
        ),
        decision=refund_decision,
    )

    assert result.action == "created"
    assert result.case is not None
    assert result.case.case_type == "refund_review"
    assert len(repository.cases) == 2


@pytest.mark.anyio
async def test_resolved_case_does_not_receive_a_new_trigger(
    service: CaseService,
    repository: _InMemoryCaseRepository,
) -> None:
    first = await service.record_handoff(
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert first.case is not None
    await service.change_status(
        case_id=first.case.case_id,
        target_status="in_progress",
        request_id="status-1",
        actor="agent-1",
    )
    await service.change_status(
        case_id=first.case.case_id,
        target_status="resolved",
        request_id="status-2",
        actor="agent-1",
    )

    result = await service.record_handoff(
        trigger=_trigger("message-2"),
        decision=_medium_safety_decision(),
    )

    assert result.action == "created"
    assert result.case is not None
    assert result.case.case_id != first.case.case_id
    assert len(repository.cases) == 2


@pytest.mark.anyio
async def test_valid_status_change_updates_version_and_creates_event(
    service: CaseService,
    repository: _InMemoryCaseRepository,
) -> None:
    created = await service.record_handoff(
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None

    result = await service.change_status(
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-1",
        actor="agent-1",
    )

    assert result.action == "status_changed"
    assert result.case is not None
    assert result.event is not None
    assert result.case.status == "in_progress"
    assert result.case.version == 2
    assert result.event.previous_status == "open"
    assert result.event.current_status == "in_progress"
    assert result.event.actor == "agent-1"
    assert len(repository.events) == 2


@pytest.mark.anyio
async def test_putting_case_on_hold_requires_reason(service: CaseService) -> None:
    created = await service.record_handoff(
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None
    await service.change_status(
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-1",
        actor="agent-1",
    )

    with pytest.raises(ValueError, match="on_hold_reason is required"):
        await service.change_status(
            case_id=created.case.case_id,
            target_status="on_hold",
            request_id="status-2",
            actor="agent-1",
        )


@pytest.mark.anyio
async def test_on_hold_reason_is_saved_and_cleared_after_resume(
    service: CaseService,
) -> None:
    created = await service.record_handoff(
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None
    await service.change_status(
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-1",
        actor="agent-1",
    )
    held = await service.change_status(
        case_id=created.case.case_id,
        target_status="on_hold",
        request_id="status-2",
        actor="agent-1",
        on_hold_reason="waiting_customer",
    )
    assert held.case is not None
    assert held.case.on_hold_reason == "waiting_customer"

    resumed = await service.change_status(
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-3",
        actor="agent-1",
    )

    assert resumed.case is not None
    assert resumed.case.on_hold_reason is None


@pytest.mark.anyio
async def test_repeating_current_status_is_an_idempotent_noop(
    service: CaseService,
    repository: _InMemoryCaseRepository,
) -> None:
    created = await service.record_handoff(
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None
    event_count = len(repository.events)

    result = await service.change_status(
        case_id=created.case.case_id,
        target_status="open",
        request_id="status-1",
        actor="agent-1",
    )

    assert result.action == "status_unchanged"
    assert result.event is None
    assert len(repository.events) == event_count


@pytest.mark.anyio
async def test_repeating_status_request_does_not_create_a_second_event(
    service: CaseService,
    repository: _InMemoryCaseRepository,
) -> None:
    created = await service.record_handoff(
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None
    first = await service.change_status(
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-1",
        actor="agent-1",
    )
    duplicate = await service.change_status(
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-1",
        actor="agent-1",
    )

    assert first.action == "status_changed"
    assert duplicate.action == "status_unchanged"
    assert len(repository.events) == 2


@pytest.mark.anyio
async def test_invalid_status_transition_is_rejected(service: CaseService) -> None:
    created = await service.record_handoff(
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None

    with pytest.raises(InvalidCaseStatusTransition):
        await service.change_status(
            case_id=created.case.case_id,
            target_status="resolved",
            request_id="status-1",
            actor="agent-1",
        )


@pytest.mark.anyio
async def test_unknown_case_is_rejected(service: CaseService) -> None:
    with pytest.raises(CaseNotFoundError):
        await service.change_status(
            case_id=UUID("00000000-0000-0000-0000-000000000001"),
            target_status="in_progress",
            request_id="status-1",
            actor="agent-1",
        )
