"""Reusable in-memory support-case repository for offline tests."""

from uuid import UUID

from agent.cases.models import (
    CaseListQuery,
    CaseType,
    SupportCase,
    SupportCaseEvent,
    SupportCaseEventPage,
    SupportCasePage,
)
from agent.cases.repository import (
    ActiveCaseConflictError,
    CaseNotFoundError,
    ConcurrentCaseUpdateError,
    DuplicateIdempotencyKeyError,
    DuplicateSourceMessageError,
)

_UNRESOLVED_STATUSES = {"open", "in_progress", "on_hold"}
_PRIORITY_RANK = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}


class InMemoryCaseRepository:
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
        case_type: CaseType,
    ) -> SupportCase | None:
        return next(
            (
                case
                for case in self.cases.values()
                if case.thread_id == thread_id
                and case.case_type == case_type
                and case.status in _UNRESOLVED_STATUSES
            ),
            None,
        )

    async def list_cases(self, query: CaseListQuery) -> SupportCasePage:
        items = [
            case
            for case in self.cases.values()
            if (query.status is None or case.status == query.status)
            and (query.priority is None or case.priority == query.priority)
            and (query.case_type is None or case.case_type == query.case_type)
            and (query.thread_id is None or case.thread_id == query.thread_id)
            and (query.order_id is None or case.order_id == query.order_id)
        ]
        items.sort(
            key=lambda case: (
                _PRIORITY_RANK[case.priority],
                case.created_at,
                str(case.case_id),
            )
        )
        return SupportCasePage(
            items=tuple(items[query.offset : query.offset + query.limit]),
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
        items = sorted(
            (event for event in self.events if event.case_id == case_id),
            key=lambda event: (event.created_at, str(event.event_id)),
        )
        return SupportCaseEventPage(
            items=tuple(items[offset : offset + limit]),
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
