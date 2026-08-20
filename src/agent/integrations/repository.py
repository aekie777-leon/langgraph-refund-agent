"""Persistence boundary for provider messaging (outbox / inbox)."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent.integrations.models import ProviderFailureKind, ProviderWebhookEventData
from agent.integrations.persistence_models import (
    ClaimedInboxMessage,
    ClaimedOutboxMessage,
    InboxMessage,
    OutboxMessage,
)


class IntegrationPersistenceError(RuntimeError):
    """Report an unexpected provider-messaging persistence failure."""


class InboxEventConflictError(IntegrationPersistenceError):
    """Report an event_id reuse with different trusted content.

    The error message never contains raw bodies, signatures, secrets, or the
    full event payload.
    """


class LeaseConflictError(RuntimeError):
    """Report a lease-guarded update that matched no row (fencing violation)."""


class OutboxAttemptsExhaustedError(RuntimeError):
    """Report an attempt to schedule a 9th delivery in one delivery cycle."""


class InboxAttemptsExhaustedError(RuntimeError):
    """Report an attempt to schedule a sixth Inbox processing attempt."""


class IntegrationRepository(Protocol):
    """Storage operations required by the provider messaging layer.

    Domain-affecting finalizations (accepted, provider-rejected, inbox
    processed) are intentionally NOT exposed here: they must be composed with
    the matching domain-aggregate update in one transaction through the
    cursor-scoped helpers in ``agent.integrations.postgres_writes``.
    """

    async def get_outbox_message(self, command_id: UUID) -> OutboxMessage | None:
        """Return one outbox message by command id."""
        ...

    async def claim_due_outbox(
        self,
        *,
        worker_id: str,
        batch_size: int,
        lease_seconds: float,
    ) -> list[ClaimedOutboxMessage]:
        """Claim due outbox messages with SKIP LOCKED and create attempts."""
        ...

    async def renew_outbox_lease(
        self,
        *,
        command_id: UUID,
        lease_id: UUID,
        lease_owner: str,
        lease_seconds: float,
    ) -> bool:
        """Extend the lease while the worker is still alive."""
        ...

    async def recover_expired_outbox_leases(
        self,
        *,
        batch_size: int,
    ) -> int:
        """Recover outbox messages whose lease expired; return recovered count."""
        ...

    async def schedule_outbox_retry(
        self,
        *,
        command_id: UUID,
        lease_id: UUID,
        lease_owner: str,
        attempt_id: UUID,
        failure_kind: ProviderFailureKind,
        error_code: str | None,
        error_message: str | None,
        retry_after_seconds: float | None,
        next_available_at: datetime,
        http_status: int | None = None,
    ) -> None:
        """Finalize the attempt and schedule the next delivery in one transaction.

        Raises ``LeaseConflictError`` when the attempt does not belong to the
        command/lease/worker or the message is not processing, and
        ``OutboxAttemptsExhaustedError`` when the cycle already consumed its
        8 attempts.
        """
        ...

    async def get_inbox_message(self, inbox_id: UUID) -> InboxMessage | None:
        """Return one inbox message by id."""
        ...

    async def receive_inbox_idempotently(
        self,
        *,
        inbox_id: UUID,
        provider_connection_id: str,
        event_id: str,
        tenant_id: str,
        event: ProviderWebhookEventData,
        raw_body_sha256: str,
        received_at: datetime,
    ) -> InboxMessage:
        """Store one verified webhook event; duplicates return the existing row.

        An exact replay (every trusted field and the canonical typed payload
        identical) returns the existing record. Reusing ``event_id`` with
        different trusted content raises ``InboxEventConflictError`` and never
        overwrites the original row.
        """
        ...

    async def claim_due_inbox(
        self,
        *,
        worker_id: str,
        batch_size: int,
        lease_seconds: float,
    ) -> list[ClaimedInboxMessage]:
        """Claim due inbox messages with SKIP LOCKED and create attempts."""
        ...

    async def renew_inbox_lease(
        self,
        *,
        inbox_id: UUID,
        lease_id: UUID,
        lease_owner: str,
        lease_seconds: float,
    ) -> bool:
        """Extend the inbox lease while the worker is still alive."""
        ...

    async def recover_expired_inbox_leases(
        self,
        *,
        batch_size: int,
    ) -> int:
        """Recover inbox messages whose lease expired; return recovered count."""
        ...

    async def mark_inbox_failed(
        self,
        *,
        inbox_id: UUID,
        lease_id: UUID,
        lease_owner: str,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """Mark a leased inbox message failed (terminal)."""
        ...

    async def schedule_inbox_retry(
        self,
        *,
        inbox_id: UUID,
        lease_id: UUID,
        lease_owner: str,
        attempt_id: UUID,
        next_available_at: datetime,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """Finish one fenced attempt and make its Inbox message due again."""
        ...
