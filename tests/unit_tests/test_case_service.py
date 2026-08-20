"""Unit tests for support-case application service behavior."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from agent.auth.directory import (
    DirectoryInfrastructureUnavailableError,
    DirectoryUser,
)
from agent.auth.visibility import ForbiddenError
from agent.cases.models import (
    CaseTrigger,
    HandoffDecision,
    HandoffPolicyInput,
)
from agent.cases.policy import (
    InvalidCaseStatusTransition,
    determine_handoff_policy,
)
from agent.cases.repository import CaseNotFoundError
from agent.cases.service import AssignmentTargetUnavailableError, CaseService
from tests.fakes.identity import make_scope, staff_directory
from tests.support_cases import InMemoryCaseRepository

NOW = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
SCOPE = make_scope("customer")
SUPERVISOR_SCOPE = make_scope("supervisor", user_id="sup-1")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def repository() -> InMemoryCaseRepository:
    return InMemoryCaseRepository()


@pytest.fixture
def service(repository: InMemoryCaseRepository) -> CaseService:
    return CaseService(
        repository,
        identity_directory=staff_directory(),
        clock=lambda: NOW,
    )


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
    repository: InMemoryCaseRepository,
) -> None:
    result = await service.record_handoff(
        SCOPE,
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
    repository: InMemoryCaseRepository,
) -> None:
    result = await service.record_handoff(
        SCOPE,
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
    assert result.case.customer_id == "customer-a"
    assert result.case.tenant_id == "tenant-demo"
    assert result.case.created_by == "tenant-demo:customer-a"
    assert result.event.event_type == "case_created"
    assert result.event.source_message_id == "message-1"
    assert result.event.actor == "system"
    assert len(repository.cases) == 1
    assert len(repository.events) == 1


@pytest.mark.anyio
async def test_duplicate_source_message_is_ignored(
    service: CaseService,
    repository: InMemoryCaseRepository,
) -> None:
    trigger = _trigger("message-1")
    decision = _medium_safety_decision()

    first = await service.record_handoff(SCOPE, trigger=trigger, decision=decision)
    duplicate = await service.record_handoff(SCOPE, trigger=trigger, decision=decision)

    assert duplicate.action == "duplicate_ignored"
    assert duplicate.case == first.case
    assert duplicate.event is None
    assert len(repository.cases) == 1
    assert len(repository.events) == 1


@pytest.mark.anyio
async def test_same_thread_and_type_appends_event_and_merges_summary(
    service: CaseService,
    repository: InMemoryCaseRepository,
) -> None:
    first = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    second = await service.record_handoff(
        SCOPE,
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
        SCOPE,
        trigger=_trigger(
            "message-1",
            risk_level="high",
            risk_categories=("violence",),
        ),
        decision=_high_safety_decision(),
    )
    result = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-2"),
        decision=_medium_safety_decision(),
    )

    assert result.case is not None
    assert result.case.priority == "p0"
    assert result.case.risk_level == "high"


@pytest.mark.anyio
async def test_same_thread_with_different_type_creates_another_case(
    service: CaseService,
    repository: InMemoryCaseRepository,
) -> None:
    await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    refund_decision = determine_handoff_policy(
        HandoffPolicyInput(refund_requires_manual_review=True)
    )
    result = await service.record_handoff(
        SCOPE,
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
    repository: InMemoryCaseRepository,
) -> None:
    first = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert first.case is not None
    await service.change_status(
        SUPERVISOR_SCOPE,
        case_id=first.case.case_id,
        target_status="in_progress",
        request_id="status-1",
    )
    await service.change_status(
        SUPERVISOR_SCOPE,
        case_id=first.case.case_id,
        target_status="resolved",
        request_id="status-2",
    )

    result = await service.record_handoff(
        SCOPE,
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
    repository: InMemoryCaseRepository,
) -> None:
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None

    result = await service.change_status(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-1",
    )

    assert result.action == "status_changed"
    assert result.case is not None
    assert result.event is not None
    assert result.case.status == "in_progress"
    assert result.case.version == 2
    assert result.event.previous_status == "open"
    assert result.event.current_status == "in_progress"
    assert result.event.actor == "tenant-demo:sup-1"
    assert len(repository.events) == 2


@pytest.mark.anyio
async def test_putting_case_on_hold_requires_reason(service: CaseService) -> None:
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None
    await service.change_status(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-1",
    )

    with pytest.raises(ValueError, match="on_hold_reason is required"):
        await service.change_status(
            SUPERVISOR_SCOPE,
            case_id=created.case.case_id,
            target_status="on_hold",
            request_id="status-2",
        )


@pytest.mark.anyio
async def test_on_hold_reason_is_saved_and_cleared_after_resume(
    service: CaseService,
) -> None:
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None
    await service.change_status(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-1",
    )
    held = await service.change_status(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        target_status="on_hold",
        request_id="status-2",
        on_hold_reason="waiting_customer",
    )
    assert held.case is not None
    assert held.case.on_hold_reason == "waiting_customer"

    resumed = await service.change_status(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-3",
    )

    assert resumed.case is not None
    assert resumed.case.on_hold_reason is None


@pytest.mark.anyio
async def test_repeating_current_status_is_an_idempotent_noop(
    service: CaseService,
    repository: InMemoryCaseRepository,
) -> None:
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None
    event_count = len(repository.events)

    result = await service.change_status(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        target_status="open",
        request_id="status-1",
    )

    assert result.action == "status_unchanged"
    assert result.event is None
    assert len(repository.events) == event_count


@pytest.mark.anyio
async def test_repeating_status_request_does_not_create_a_second_event(
    service: CaseService,
    repository: InMemoryCaseRepository,
) -> None:
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None
    first = await service.change_status(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-1",
    )
    duplicate = await service.change_status(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        target_status="in_progress",
        request_id="status-1",
    )

    assert first.action == "status_changed"
    assert duplicate.action == "status_unchanged"
    assert len(repository.events) == 2


@pytest.mark.anyio
async def test_invalid_status_transition_is_rejected(service: CaseService) -> None:
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None

    with pytest.raises(InvalidCaseStatusTransition):
        await service.change_status(
            SUPERVISOR_SCOPE,
            case_id=created.case.case_id,
            target_status="resolved",
            request_id="status-1",
        )


@pytest.mark.anyio
async def test_unknown_case_is_rejected(service: CaseService) -> None:
    with pytest.raises(CaseNotFoundError):
        await service.change_status(
            SUPERVISOR_SCOPE,
            case_id=UUID("00000000-0000-0000-0000-000000000001"),
            target_status="in_progress",
            request_id="status-1",
        )


@pytest.mark.anyio
async def test_customer_cannot_change_status(
    service: CaseService,
    repository: InMemoryCaseRepository,
) -> None:
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None

    with pytest.raises(ForbiddenError):
        await service.change_status(
            SCOPE,
            case_id=created.case.case_id,
            target_status="in_progress",
            request_id="status-1",
        )
    assert len(repository.events) == 1


@pytest.mark.anyio
async def test_only_supervisor_can_assign(
    service: CaseService,
) -> None:
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None

    with pytest.raises(ForbiddenError):
        await service.assign_case(
            SCOPE,
            case_id=created.case.case_id,
            agent_id="agent-7",
            request_id="assign-1",
        )
    with pytest.raises(ForbiddenError):
        await service.assign_case(
            make_scope("support_agent", user_id="agent-7"),
            case_id=created.case.case_id,
            agent_id="agent-8",
            request_id="assign-1",
        )


@pytest.mark.anyio
async def test_supervisor_assigns_case_idempotently(
    service: CaseService,
    repository: InMemoryCaseRepository,
) -> None:
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None

    first = await service.assign_case(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        agent_id="agent-7",
        request_id="assign-1",
    )
    duplicate = await service.assign_case(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        agent_id="agent-7",
        request_id="assign-1",
    )
    same_agent = await service.assign_case(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        agent_id="agent-7",
        request_id="assign-2",
    )

    assert first.action == "assigned"
    assert first.case is not None
    assert first.event is not None
    assert first.case.assigned_agent_id == "agent-7"
    assert first.event.event_type == "assigned"
    assert first.event.current_assigned_agent_id == "agent-7"
    assert first.event.previous_assigned_agent_id is None
    assert first.event.actor == "tenant-demo:sup-1"
    assert duplicate.action == "status_unchanged"
    assert same_agent.action == "status_unchanged"
    assert len(repository.events) == 2


@pytest.mark.anyio
async def test_supervisor_reassigns_case_with_previous_agent(
    service: CaseService,
    repository: InMemoryCaseRepository,
) -> None:
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None

    await service.assign_case(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        agent_id="agent-7",
        request_id="assign-1",
    )
    result = await service.assign_case(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        agent_id="agent-8",
        request_id="assign-2",
    )

    assert result.action == "assigned"
    assert result.case is not None
    assert result.event is not None
    assert result.case.assigned_agent_id == "agent-8"
    assert result.event.previous_assigned_agent_id == "agent-7"
    assert result.event.current_assigned_agent_id == "agent-8"
    assert len(repository.events) == 3


@pytest.mark.anyio
@pytest.mark.parametrize("agent_id", ["", "system", "legacy", "a:b", "x" * 129])
async def test_assign_rejects_invalid_agent_ids(
    service: CaseService,
    agent_id: str,
) -> None:
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None

    with pytest.raises(ValueError):
        await service.assign_case(
            SUPERVISOR_SCOPE,
            case_id=created.case.case_id,
            agent_id=agent_id,
            request_id=f"assign-{len(agent_id)}",
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "candidate",
    [
        None,
        DirectoryUser(
            tenant_id="tenant-other",
            user_id="agent-7",
            active=True,
            roles=frozenset({"support_agent"}),
        ),
        DirectoryUser(
            tenant_id="tenant-demo",
            user_id="agent-7",
            active=False,
            roles=frozenset({"support_agent"}),
        ),
        DirectoryUser(
            tenant_id="tenant-demo",
            user_id="agent-7",
            active=True,
            roles=frozenset({"customer"}),
        ),
    ],
)
async def test_ineligible_assignment_targets_share_one_result_and_do_not_write(
    repository: InMemoryCaseRepository,
    candidate: DirectoryUser | None,
) -> None:
    created = await CaseService(repository, clock=lambda: NOW).record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None

    class StaticDirectory:
        async def find_user(self, *, tenant_id: str, user_id: str):
            return candidate

    service = CaseService(
        repository,
        identity_directory=StaticDirectory(),
        clock=lambda: NOW,
    )
    before_case = repository.cases[created.case.case_id]
    before_events = tuple(repository.events)

    with pytest.raises(AssignmentTargetUnavailableError) as error:
        await service.assign_case(
            SUPERVISOR_SCOPE,
            case_id=created.case.case_id,
            agent_id="agent-7",
            request_id="assign-ineligible",
        )

    assert str(error.value) == "assignment target is unavailable"
    assert repository.cases[created.case.case_id] == before_case
    assert tuple(repository.events) == before_events


@pytest.mark.anyio
async def test_directory_outage_does_not_write_case_or_event(
    repository: InMemoryCaseRepository,
) -> None:
    created = await CaseService(repository, clock=lambda: NOW).record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None

    class OutageDirectory:
        async def find_user(self, *, tenant_id: str, user_id: str):
            raise DirectoryInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            )

    service = CaseService(
        repository,
        identity_directory=OutageDirectory(),
        clock=lambda: NOW,
    )
    before_case = repository.cases[created.case.case_id]
    before_events = tuple(repository.events)

    with pytest.raises(DirectoryInfrastructureUnavailableError):
        await service.assign_case(
            SUPERVISOR_SCOPE,
            case_id=created.case.case_id,
            agent_id="agent-7",
            request_id="assign-outage",
        )

    assert repository.cases[created.case.case_id] == before_case
    assert tuple(repository.events) == before_events


@pytest.mark.anyio
async def test_successful_idempotent_replay_does_not_depend_on_directory(
    repository: InMemoryCaseRepository,
) -> None:
    directory = staff_directory()
    service = CaseService(
        repository,
        identity_directory=directory,
        clock=lambda: NOW,
    )
    created = await service.record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None
    await service.assign_case(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        agent_id="agent-7",
        request_id="assign-1",
    )

    class OutageDirectory:
        async def find_user(self, *, tenant_id: str, user_id: str):
            raise AssertionError("idempotent replay must not query the directory")

    replay_service = CaseService(
        repository,
        identity_directory=OutageDirectory(),
        clock=lambda: NOW,
    )
    replay = await replay_service.assign_case(
        SUPERVISOR_SCOPE,
        case_id=created.case.case_id,
        agent_id="agent-7",
        request_id="assign-1",
    )

    assert replay.action == "status_unchanged"
    assert len(repository.events) == 2


@pytest.mark.anyio
async def test_concurrent_same_assignment_writes_one_event(
    repository: InMemoryCaseRepository,
) -> None:
    created = await CaseService(repository, clock=lambda: NOW).record_handoff(
        SCOPE,
        trigger=_trigger("message-1"),
        decision=_medium_safety_decision(),
    )
    assert created.case is not None
    ready = 0
    release = asyncio.Event()

    class BarrierDirectory:
        async def find_user(self, *, tenant_id: str, user_id: str):
            nonlocal ready
            ready += 1
            if ready == 2:
                release.set()
            await release.wait()
            return DirectoryUser(
                tenant_id=tenant_id,
                user_id=user_id,
                active=True,
                roles=frozenset({"support_agent"}),
            )

    service = CaseService(
        repository,
        identity_directory=BarrierDirectory(),
        clock=lambda: NOW,
    )
    results = await asyncio.gather(
        *(
            service.assign_case(
                SUPERVISOR_SCOPE,
                case_id=created.case.case_id,
                agent_id="agent-7",
                request_id="assign-concurrent",
            )
            for _ in range(2)
        )
    )

    assert {result.action for result in results} == {"assigned", "status_unchanged"}
    assert len(repository.events) == 2
