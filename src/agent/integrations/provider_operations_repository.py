"""Persistence boundary for the tenant-scoped Provider operations control plane."""

from typing import Protocol
from uuid import UUID

from agent.auth.models import AccessScope
from agent.integrations.provider_operations_contracts import (
    ProviderInboxDetail,
    ProviderOutboxDetail,
    ProviderQueueOverview,
    ProviderRedriveRequest,
    ProviderRedriveView,
)


class ProviderOperationsNotFoundError(RuntimeError):
    """Hide whether a Provider resource is absent or belongs to another tenant."""


class ProviderOperationsConflictError(RuntimeError):
    """Report only a stable, non-sensitive conflict code."""

    def __init__(self, code: str) -> None:
        """Store the stable conflict code without database details."""
        super().__init__(code)
        self.code = code


class ProviderOperationsPersistenceError(RuntimeError):
    """Hide database, constraint, and SQL details from higher layers."""


class ProviderOperationsRepository(Protocol):
    """Define safe Provider operations reads and atomic recovery commands."""

    async def get_queue_overview(self, scope: AccessScope) -> ProviderQueueOverview:
        """Return tenant-scoped queue aggregates."""

    async def get_outbox_detail(
        self, scope: AccessScope, command_id: UUID, *, history_limit: int = 50
    ) -> ProviderOutboxDetail:
        """Return one payload-free Outbox detail."""

    async def get_inbox_detail(
        self, scope: AccessScope, inbox_id: UUID, *, history_limit: int = 50
    ) -> ProviderInboxDetail:
        """Return one payload-free Inbox detail."""

    async def redrive_outbox(
        self,
        scope: AccessScope,
        command_id: UUID,
        request: ProviderRedriveRequest,
    ) -> ProviderRedriveView:
        """Atomically recover one eligible Outbox command."""

    async def redrive_inbox(
        self,
        scope: AccessScope,
        inbox_id: UUID,
        request: ProviderRedriveRequest,
    ) -> ProviderRedriveView:
        """Atomically open a new processing cycle for one failed Inbox message."""
