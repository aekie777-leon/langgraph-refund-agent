"""Define persistence boundaries for support cases."""

from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from agent.auth.models import AccessScope
from agent.cases.models import (
    CaseListQuery,
    CaseType,
    SupportCase,
    SupportCaseEvent,
    SupportCaseEventPage,
    SupportCasePage,
)
from agent.integrations.models import ProviderCommandEnvelope


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


def _revalidate_command_envelope(
    command: ProviderCommandEnvelope,
) -> ProviderCommandEnvelope:
    """Re-run the full envelope validation from the serialized form.

    Pydantic may return an existing model instance unchanged from
    ``model_validate(command)``, so a ``model_copy``-tampered envelope could
    bypass the validators. Serializing first forces every validator to run
    again. The original ``ValidationError`` is preserved as the cause.
    """
    try:
        return ProviderCommandEnvelope.model_validate(
            command.model_dump(mode="python")
        )
    except ValidationError as error:
        raise ValueError("command envelope is invalid") from error


def validate_case_command_association(
    *,
    case: SupportCase,
    event: SupportCaseEvent,
    command: ProviderCommandEnvelope,
) -> None:
    """Reject atomic case+command writes whose parts do not describe the same aggregate.

    Called before any connection or transaction is acquired; a failure leaves
    no domain row, event, or outbox write behind. Association is verified on
    typed identifiers only, never on display text.
    """
    command = _revalidate_command_envelope(command)
    if case.case_type != "delivery_investigation":
        raise ValueError(
            "case command writes require a delivery_investigation case"
        )
    if case.order_id is None:
        raise ValueError("delivery_investigation cases require an order_id")
    if command.command_type != "delivery_investigation":
        raise ValueError("command.command_type must be 'delivery_investigation'")
    if command.aggregate_type != "support_case":
        raise ValueError("command.aggregate_type must be 'support_case'")
    if command.aggregate_id != case.case_id:
        raise ValueError("command.aggregate_id must match the case_id")
    if command.tenant_id != case.tenant_id:
        raise ValueError("command.tenant_id must match the case tenant_id")
    if command.customer_id != case.customer_id:
        raise ValueError("command.customer_id must match the case customer_id")
    if command.source_message_id != case.source_message_id:
        raise ValueError(
            "command.source_message_id must match the case source_message_id"
        )
    if command.expected_order_version is not None:
        raise ValueError(
            "delivery commands must not carry expected_order_version"
        )
    if command.payload.order_id != case.order_id:
        raise ValueError("command payload order_id must match the case order_id")
    if event.case_id != case.case_id:
        raise ValueError("event.case_id must match the case_id")
    if event.tenant_id != case.tenant_id:
        raise ValueError("event.tenant_id must match the case tenant_id")
    if event.customer_id != case.customer_id:
        raise ValueError("event.customer_id must match the case customer_id")


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
        order_id: str | None = None,
    ) -> SupportCase | None:
        """Find an unresolved case with the same thread and type.

        Delivery-investigation cases are isolated per order: when
        ``case_type`` is ``delivery_investigation``, ``order_id`` is required
        and part of the match; other case types ignore it.
        """
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

    async def create_case_with_event_and_command(
        self,
        scope: AccessScope,
        *,
        case: SupportCase,
        event: SupportCaseEvent,
        command: ProviderCommandEnvelope,
    ) -> None:
        """Atomically create a case, its first event, and the outbox command."""
        ...

    async def update_case_with_event_and_command(
        self,
        scope: AccessScope,
        *,
        case: SupportCase,
        event: SupportCaseEvent,
        command: ProviderCommandEnvelope,
        expected_version: int,
    ) -> None:
        """Atomically update a case, append an event, and enqueue the command."""
        ...

    async def append_delivery_trigger_and_ensure_command(
        self,
        scope: AccessScope,
        *,
        case: SupportCase,
        event: SupportCaseEvent,
        command: ProviderCommandEnvelope,
        expected_version: int,
    ) -> bool:
        """Append a delivery trigger and insert its command only when absent.

        The aggregate update, event, existence check, and optional Outbox
        insert are one transaction.  Returns whether a new command was added.
        """
        ...
