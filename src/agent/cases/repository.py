"""Define persistence boundaries for support cases."""

from typing import Protocol
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


class DuplicateSourceMessageError(RuntimeError):
    """Report that a source message has already been persisted."""


class DuplicateIdempotencyKeyError(RuntimeError):
    """Report that an operation idempotency key has already been persisted."""


class ActiveCaseConflictError(RuntimeError):
    """Report a conflicting unresolved case created concurrently."""


class ConcurrentCaseUpdateError(RuntimeError):
    """Report an optimistic-lock version conflict."""


class CaseNotFoundError(LookupError):
    """Report that the requested case does not exist."""


class CasePersistenceError(RuntimeError):
    """Report an unexpected support-case persistence failure."""


class CaseRepository(Protocol):
    """Define storage operations required by the case service."""

    async def get_case(self, scope: AccessScope, case_id: UUID) -> SupportCase | None:
        """Return a case by ID within the caller's access scope."""
        ...

    async def find_by_source_message(
        self,
        scope: AccessScope,
        *,
        thread_id: str,
        source_message_id: str,
    ) -> SupportCase | None:
        """Find the case already associated with a triggering message."""
        ...

    async def find_event_by_idempotency_key(
        self,
        scope: AccessScope,
        idempotency_key: str,
    ) -> SupportCaseEvent | None:
        """Find a previously recorded operation event."""
        ...

    async def find_unresolved_case(
        self,
        scope: AccessScope,
        *,
        thread_id: str,
        case_type: CaseType,
    ) -> SupportCase | None:
        """Find an unresolved case with the same thread and type."""
        ...

    async def list_cases(
        self,
        scope: AccessScope,
        query: CaseListQuery,
    ) -> SupportCasePage:
        """Return a filtered and stably ordered page of cases."""
        ...

    async def list_case_events(
        self,
        scope: AccessScope,
        *,
        case_id: UUID,
        limit: int,
        offset: int,
    ) -> SupportCaseEventPage:
        """Return a stably ordered page of immutable case events."""
        ...

    async def create_case_with_event(
        self,
        scope: AccessScope,
        *,
        case: SupportCase,
        event: SupportCaseEvent,
    ) -> None:
        """Atomically create a case and its first event."""
        ...

    async def update_case_with_event(
        self,
        scope: AccessScope,
        *,
        case: SupportCase,
        event: SupportCaseEvent,
        expected_version: int,
    ) -> None:
        """Atomically update a case and append an event."""
        ...
