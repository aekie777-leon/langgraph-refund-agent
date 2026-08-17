"""Reusable in-memory support-case repository for offline tests."""

from uuid import UUID

from agent.auth.models import AccessScope
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


def _case_visible(scope: AccessScope, case: SupportCase) -> bool:
    """Return whether a case is visible within a caller scope."""
    if case.tenant_id != scope.tenant_id:
        return False
    if scope.role == "customer":
        return case.customer_id == scope.customer_id
    if scope.role == "support_agent":
        return case.assigned_agent_id == scope.user_id
    return True


def _event_visible(scope: AccessScope, event: SupportCaseEvent) -> bool:
    """Return whether an event is visible within a caller scope."""
    if event.tenant_id != scope.tenant_id:
        return False
    if scope.role == "customer":
        return event.customer_id == scope.customer_id
    return True


class InMemoryCaseRepository:
    """Enforce the repository contract without external infrastructure."""

    def __init__(self) -> None:
        self.cases: dict[UUID, SupportCase] = {}
        self.events: list[SupportCaseEvent] = []

    async def get_case(self, scope: AccessScope, case_id: UUID) -> SupportCase | None:
        case = self.cases.get(case_id)
        if case is None or not _case_visible(scope, case):
            return None
        return case

    async def find_by_source_message(
        self,
        scope: AccessScope,
        *,
        thread_id: str,
        source_message_id: str,
    ) -> SupportCase | None:
        for event in self.events:
            if event.source_message_id != source_message_id:
                continue
            if not _event_visible(scope, event):
                continue
            case = self.cases[event.case_id]
            if case.thread_id == thread_id and _case_visible(scope, case):
                return case
        return None

    async def find_event_by_idempotency_key(
        self,
        scope: AccessScope,
        idempotency_key: str,
    ) -> SupportCaseEvent | None:
        return next(
            (
                event
                for event in self.events
                if event.idempotency_key == idempotency_key
                and event.tenant_id == scope.tenant_id
            ),
            None,
        )

    async def find_unresolved_case(
        self,
        scope: AccessScope,
        *,
        thread_id: str,
        case_type: CaseType,
    ) -> SupportCase | None:
        return next(
            (
                case
                for case in self.cases.values()
                if _case_visible(scope, case)
                and case.thread_id == thread_id
                and case.case_type == case_type
                and case.status in _UNRESOLVED_STATUSES
            ),
            None,
        )

    async def list_cases(
        self,
        scope: AccessScope,
        query: CaseListQuery,
    ) -> SupportCasePage:
        items = [
            case
            for case in self.cases.values()
            if _case_visible(scope, case)
            and (query.status is None or case.status == query.status)
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
        scope: AccessScope,
        *,
        case_id: UUID,
        limit: int,
        offset: int,
    ) -> SupportCaseEventPage:
        items = sorted(
            (
                event
                for event in self.events
                if event.case_id == case_id and event.tenant_id == scope.tenant_id
            ),
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
        scope: AccessScope,
        *,
        case: SupportCase,
        event: SupportCaseEvent,
    ) -> None:
        _validate_ownership(scope, case.customer_id, case.tenant_id)
        if await self.find_event_by_idempotency_key(scope, event.idempotency_key):
            raise DuplicateIdempotencyKeyError(event.idempotency_key)
        if event.source_message_id is not None and await self.find_by_source_message(
            scope,
            thread_id=case.thread_id,
            source_message_id=event.source_message_id,
        ):
            raise DuplicateSourceMessageError(event.source_message_id)
        if await self.find_unresolved_case(
            scope,
            thread_id=case.thread_id,
            case_type=case.case_type,
        ):
            raise ActiveCaseConflictError(case.thread_id)

        self.cases[case.case_id] = case
        self.events.append(event)

    async def update_case_with_event(
        self,
        scope: AccessScope,
        *,
        case: SupportCase,
        event: SupportCaseEvent,
        expected_version: int,
    ) -> None:
        current = self.cases.get(case.case_id)
        if current is None or not _case_visible(scope, current):
            raise CaseNotFoundError(str(case.case_id))
        if current.version != expected_version:
            raise ConcurrentCaseUpdateError(str(case.case_id))
        if await self.find_event_by_idempotency_key(scope, event.idempotency_key):
            raise DuplicateIdempotencyKeyError(event.idempotency_key)
        if event.source_message_id is not None and await self.find_by_source_message(
            scope,
            thread_id=current.thread_id,
            source_message_id=event.source_message_id,
        ):
            raise DuplicateSourceMessageError(event.source_message_id)

        self.cases[case.case_id] = case
        self.events.append(event)


def _validate_ownership(scope: AccessScope, customer_id: str, tenant_id: str) -> None:
    """Reject writes whose ownership does not match the caller scope."""
    if tenant_id != scope.tenant_id:
        raise ValueError("tenant_id must match the access scope")
    if scope.role == "customer" and customer_id != scope.customer_id:
        raise ValueError("customer_id must match the access scope")
