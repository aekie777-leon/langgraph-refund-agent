"""Authorization boundary for Provider operations use cases."""

from uuid import UUID

from agent.auth.models import AccessScope, ProviderOperationsPermission
from agent.auth.rbac import has_provider_operations_permission
from agent.auth.visibility import ForbiddenError
from agent.integrations.provider_operations_contracts import (
    ProviderInboxDetail,
    ProviderOutboxDetail,
    ProviderQueueOverview,
    ProviderRedriveRequest,
    ProviderRedriveView,
)
from agent.integrations.provider_operations_repository import (
    ProviderOperationsRepository,
)


def _authorize(scope: AccessScope, permission: ProviderOperationsPermission) -> None:
    """Require both the Supervisor role and the exact Provider permission."""
    if not has_provider_operations_permission(scope, permission):
        raise ForbiddenError("the caller cannot access Provider operations")


class ProviderOperationsService:
    """Expose safe use cases while the repository repeats authorization checks."""

    def __init__(self, repository: ProviderOperationsRepository) -> None:
        """Store the defense-in-depth persistence boundary."""
        self._repository = repository

    async def get_queue_overview(self, scope: AccessScope) -> ProviderQueueOverview:
        """Return safe tenant queue aggregates to an authorized Supervisor."""
        _authorize(scope, "provider_ops:read")
        return await self._repository.get_queue_overview(scope)

    async def get_outbox_detail(
        self, scope: AccessScope, command_id: UUID, *, history_limit: int = 50
    ) -> ProviderOutboxDetail:
        """Return one safe tenant Outbox detail to an authorized Supervisor."""
        _authorize(scope, "provider_ops:read")
        return await self._repository.get_outbox_detail(
            scope, command_id, history_limit=history_limit
        )

    async def get_inbox_detail(
        self, scope: AccessScope, inbox_id: UUID, *, history_limit: int = 50
    ) -> ProviderInboxDetail:
        """Return one safe tenant Inbox detail to an authorized Supervisor."""
        _authorize(scope, "provider_ops:read")
        return await self._repository.get_inbox_detail(
            scope, inbox_id, history_limit=history_limit
        )

    async def redrive_outbox(
        self,
        scope: AccessScope,
        command_id: UUID,
        request: ProviderRedriveRequest,
    ) -> ProviderRedriveView:
        """Request one authorized atomic Outbox recovery."""
        _authorize(scope, "provider_ops:redrive")
        return await self._repository.redrive_outbox(scope, command_id, request)

    async def redrive_inbox(
        self,
        scope: AccessScope,
        inbox_id: UUID,
        request: ProviderRedriveRequest,
    ) -> ProviderRedriveView:
        """Request one authorized atomic Inbox recovery."""
        _authorize(scope, "provider_ops:redrive")
        return await self._repository.redrive_inbox(scope, inbox_id, request)
