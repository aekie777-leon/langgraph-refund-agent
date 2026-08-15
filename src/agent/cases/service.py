"""Coordinate support-case creation, merging, and status changes."""

from collections.abc import Callable, Hashable
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID, uuid4

from agent.cases.models import (
    CaseListQuery,
    CaseServiceResult,
    CaseStatus,
    CaseTrigger,
    HandoffDecision,
    OnHoldReason,
    SupportCase,
    SupportCaseEvent,
    SupportCaseEventPage,
    SupportCasePage,
)
from agent.cases.policy import (
    build_display_reason,
    select_higher_priority,
    validate_case_status_transition,
)
from agent.cases.repository import (
    ActiveCaseConflictError,
    CaseNotFoundError,
    CaseRepository,
    ConcurrentCaseUpdateError,
    DuplicateIdempotencyKeyError,
    DuplicateSourceMessageError,
)
from agent.schemas import SemanticRiskLevel

Clock = Callable[[], datetime]
IdFactory = Callable[[], UUID]
T = TypeVar("T", bound=Hashable)

_RISK_LEVEL_RANK: dict[SemanticRiskLevel, int] = {
    "none": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def _utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


def _merge_unique(
    current: tuple[T, ...],
    incoming: tuple[T, ...],
) -> tuple[T, ...]:
    """Merge ordered values without retaining duplicates."""
    return tuple(dict.fromkeys((*current, *incoming)))


def _select_higher_risk_level(
    current: SemanticRiskLevel | None,
    incoming: SemanticRiskLevel | None,
) -> SemanticRiskLevel | None:
    """Retain the highest semantic risk level recorded on a case."""
    if current is None:
        return incoming
    if incoming is None:
        return current
    if _RISK_LEVEL_RANK[incoming] > _RISK_LEVEL_RANK[current]:
        return incoming
    return current


class CaseService:
    """Apply application-level case persistence behavior."""

    def __init__(
        self,
        repository: CaseRepository,
        *,
        clock: Clock = _utc_now,
        id_factory: IdFactory = uuid4,
        max_write_attempts: int = 3,
    ) -> None:
        """Initialize the service with persistence and deterministic test hooks."""
        if max_write_attempts < 1:
            raise ValueError("max_write_attempts must be at least 1")
        self._repository = repository
        self._clock = clock
        self._id_factory = id_factory
        self._max_write_attempts = max_write_attempts

    async def get_case(self, case_id: UUID) -> SupportCase:
        """Return one support case or raise when it does not exist."""
        case = await self._repository.get_case(case_id)
        if case is None:
            raise CaseNotFoundError(str(case_id))
        return case

    async def list_cases(
        self,
        query: CaseListQuery,
    ) -> SupportCasePage:
        """Return support cases matching validated filters."""
        return await self._repository.list_cases(query)

    async def list_case_events(
        self,
        *,
        case_id: UUID,
        limit: int = 100,
        offset: int = 0,
    ) -> SupportCaseEventPage:
        """Return the immutable event timeline for an existing case."""
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("offset must not be negative")
        if await self._repository.get_case(case_id) is None:
            raise CaseNotFoundError(str(case_id))
        return await self._repository.list_case_events(
            case_id=case_id,
            limit=limit,
            offset=offset,
        )

    async def record_handoff(
        self,
        *,
        trigger: CaseTrigger,
        decision: HandoffDecision,
    ) -> CaseServiceResult:
        """Create a case or append the trigger to a matching unresolved case."""
        if not decision.should_create_case:
            return CaseServiceResult(action="not_created")

        if decision.case_type is None or decision.priority is None:
            raise ValueError("A positive handoff decision must be complete")

        last_conflict: RuntimeError | None = None
        for _attempt in range(self._max_write_attempts):
            duplicate = await self._repository.find_by_source_message(
                thread_id=trigger.thread_id,
                source_message_id=trigger.source_message_id,
            )
            if duplicate is not None:
                return CaseServiceResult(
                    action="duplicate_ignored",
                    case=duplicate,
                )

            existing = await self._repository.find_unresolved_case(
                thread_id=trigger.thread_id,
                case_type=decision.case_type,
            )

            try:
                if existing is None:
                    return await self._create_case(
                        trigger=trigger,
                        decision=decision,
                    )
                return await self._append_trigger(
                    existing=existing,
                    trigger=trigger,
                    decision=decision,
                )
            except (
                ActiveCaseConflictError,
                ConcurrentCaseUpdateError,
                DuplicateIdempotencyKeyError,
                DuplicateSourceMessageError,
            ) as error:
                last_conflict = error

        raise ConcurrentCaseUpdateError(
            "Could not record the handoff after concurrent write conflicts"
        ) from last_conflict

    async def change_status(
        self,
        *,
        case_id: UUID,
        target_status: CaseStatus,
        request_id: str,
        actor: str,
        on_hold_reason: OnHoldReason | None = None,
    ) -> CaseServiceResult:
        """Change case status and record an immutable audit event."""
        request_id = request_id.strip()
        actor = actor.strip()
        if not request_id:
            raise ValueError("request_id must not be empty")
        if not actor:
            raise ValueError("actor must not be empty")

        idempotency_key = f"status:{case_id}:{request_id}"
        previous_event = await self._repository.find_event_by_idempotency_key(
            idempotency_key
        )
        if previous_event is not None:
            previous_case = await self._repository.get_case(previous_event.case_id)
            if previous_case is None:
                raise CaseNotFoundError(str(previous_event.case_id))
            return CaseServiceResult(
                action="status_unchanged",
                case=previous_case,
            )

        last_conflict: RuntimeError | None = None
        for _attempt in range(self._max_write_attempts):
            current = await self._repository.get_case(case_id)
            if current is None:
                raise CaseNotFoundError(str(case_id))

            validate_case_status_transition(current.status, target_status)
            if current.status == target_status:
                return CaseServiceResult(
                    action="status_unchanged",
                    case=current,
                )

            self._validate_on_hold_request(target_status, on_hold_reason)
            now = self._clock()
            updated = SupportCase.model_validate(
                {
                    **current.model_dump(mode="python"),
                    "status": target_status,
                    "on_hold_reason": on_hold_reason,
                    "updated_at": now,
                    "version": current.version + 1,
                }
            )
            event = SupportCaseEvent(
                event_id=self._id_factory(),
                idempotency_key=idempotency_key,
                case_id=current.case_id,
                event_type="status_changed",
                previous_priority=current.priority,
                current_priority=current.priority,
                previous_status=current.status,
                current_status=target_status,
                on_hold_reason=on_hold_reason,
                actor=actor,
                created_at=now,
            )

            try:
                await self._repository.update_case_with_event(
                    case=updated,
                    event=event,
                    expected_version=current.version,
                )
            except DuplicateIdempotencyKeyError:
                duplicate_event = await self._repository.find_event_by_idempotency_key(
                    idempotency_key
                )
                if duplicate_event is not None:
                    duplicate_case = await self._repository.get_case(
                        duplicate_event.case_id
                    )
                    if duplicate_case is not None:
                        return CaseServiceResult(
                            action="status_unchanged",
                            case=duplicate_case,
                        )
                last_conflict = DuplicateIdempotencyKeyError(idempotency_key)
                continue
            except ConcurrentCaseUpdateError as error:
                last_conflict = error
                continue

            return CaseServiceResult(
                action="status_changed",
                case=updated,
                event=event,
            )

        raise ConcurrentCaseUpdateError(
            "Could not change case status after concurrent write conflicts"
        ) from last_conflict

    async def _create_case(
        self,
        *,
        trigger: CaseTrigger,
        decision: HandoffDecision,
    ) -> CaseServiceResult:
        """Create a case and its initial event as one repository operation."""
        assert decision.case_type is not None
        assert decision.priority is not None

        now = self._clock()
        case_id = self._id_factory()
        case = SupportCase(
            case_id=case_id,
            thread_id=trigger.thread_id,
            source_message_id=trigger.source_message_id,
            order_id=trigger.order_id,
            case_type=decision.case_type,
            priority=decision.priority,
            status="open",
            risk_level=trigger.risk_level,
            risk_categories=trigger.risk_categories,
            reason_codes=decision.reason_codes,
            display_reason=decision.display_reason,
            triggering_message_excerpt=trigger.triggering_message_excerpt,
            created_at=now,
            updated_at=now,
            version=1,
        )
        event = SupportCaseEvent(
            event_id=self._id_factory(),
            idempotency_key=self._message_idempotency_key(trigger),
            case_id=case_id,
            event_type="case_created",
            source_message_id=trigger.source_message_id,
            order_id=trigger.order_id,
            risk_level=trigger.risk_level,
            risk_categories=trigger.risk_categories,
            reason_codes=decision.reason_codes,
            triggering_message_excerpt=trigger.triggering_message_excerpt,
            current_priority=decision.priority,
            current_status="open",
            actor="system",
            created_at=now,
        )

        await self._repository.create_case_with_event(case=case, event=event)
        return CaseServiceResult(action="created", case=case, event=event)

    async def _append_trigger(
        self,
        *,
        existing: SupportCase,
        trigger: CaseTrigger,
        decision: HandoffDecision,
    ) -> CaseServiceResult:
        """Merge summary fields and append one immutable trigger event."""
        assert decision.priority is not None

        now = self._clock()
        merged_priority = select_higher_priority(
            existing.priority,
            decision.priority,
        )
        merged_reasons = _merge_unique(
            existing.reason_codes,
            decision.reason_codes,
        )
        merged_categories = _merge_unique(
            existing.risk_categories,
            trigger.risk_categories,
        )
        updated = SupportCase.model_validate(
            {
                **existing.model_dump(mode="python"),
                "order_id": existing.order_id or trigger.order_id,
                "priority": merged_priority,
                "risk_level": _select_higher_risk_level(
                    existing.risk_level,
                    trigger.risk_level,
                ),
                "risk_categories": merged_categories,
                "reason_codes": merged_reasons,
                "display_reason": build_display_reason(merged_reasons),
                "updated_at": now,
                "version": existing.version + 1,
            }
        )
        event = SupportCaseEvent(
            event_id=self._id_factory(),
            idempotency_key=self._message_idempotency_key(trigger),
            case_id=existing.case_id,
            event_type="trigger_appended",
            source_message_id=trigger.source_message_id,
            order_id=trigger.order_id,
            risk_level=trigger.risk_level,
            risk_categories=trigger.risk_categories,
            reason_codes=decision.reason_codes,
            triggering_message_excerpt=trigger.triggering_message_excerpt,
            previous_priority=existing.priority,
            current_priority=merged_priority,
            current_status=existing.status,
            actor="system",
            created_at=now,
        )

        await self._repository.update_case_with_event(
            case=updated,
            event=event,
            expected_version=existing.version,
        )
        return CaseServiceResult(
            action="event_appended",
            case=updated,
            event=event,
        )

    @staticmethod
    def _message_idempotency_key(trigger: CaseTrigger) -> str:
        """Build a stable key for a triggering message."""
        return f"message:{trigger.thread_id}:{trigger.source_message_id}"

    @staticmethod
    def _validate_on_hold_request(
        target_status: CaseStatus,
        on_hold_reason: OnHoldReason | None,
    ) -> None:
        """Validate metadata supplied with a status-change request."""
        if target_status == "on_hold" and on_hold_reason is None:
            raise ValueError("on_hold_reason is required when putting a case on hold")
        if target_status != "on_hold" and on_hold_reason is not None:
            raise ValueError("on_hold_reason is only valid for the on_hold status")
