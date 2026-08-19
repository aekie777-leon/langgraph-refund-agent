"""Expose the internal Provider operations control-plane routes."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from agent.auth.dependencies import require_access_scope
from agent.auth.models import AccessScope
from agent.cases.api_models import ApiErrorResponse
from agent.integrations.provider_operations_contracts import (
    ProviderInboxDetail,
    ProviderOutboxDetail,
    ProviderQueueOverview,
    ProviderRedriveRequest,
    ProviderRedriveView,
)
from agent.integrations.provider_operations_runtime import (
    get_provider_operations_service,
)
from agent.integrations.provider_operations_service import ProviderOperationsService

router = APIRouter(
    prefix="/internal/provider-operations",
    tags=["Internal Provider Operations"],
)

ProviderOperationsServiceDependency = Annotated[
    ProviderOperationsService,
    Depends(get_provider_operations_service),
]
AccessScopeDependency = Annotated[
    AccessScope,
    Depends(require_access_scope),
]
HistoryLimit = Annotated[int, Query(ge=1, le=100)]

_UNAUTHORIZED_RESPONSE: dict[int | str, dict[str, Any]] = {
    401: {"description": "Authentication is required."},
}
_FORBIDDEN_AND_STORAGE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_UNAUTHORIZED_RESPONSE,
    403: {"model": ApiErrorResponse},
    503: {"model": ApiErrorResponse},
}
_READ_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_FORBIDDEN_AND_STORAGE_RESPONSES,
    404: {"model": ApiErrorResponse},
    422: {"model": ApiErrorResponse},
}
_REDRIVE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_READ_RESPONSES,
    409: {"model": ApiErrorResponse},
}


@router.get(
    "/queues",
    response_model=ProviderQueueOverview,
    responses=_FORBIDDEN_AND_STORAGE_RESPONSES,
)
async def get_provider_queue_overview(
    scope: AccessScopeDependency,
    service: ProviderOperationsServiceDependency,
) -> ProviderQueueOverview:
    """Return safe tenant-scoped Provider queue aggregates."""
    return await service.get_queue_overview(scope)


@router.get(
    "/outbox/{command_id}",
    response_model=ProviderOutboxDetail,
    responses=_READ_RESPONSES,
)
async def get_provider_outbox_detail(
    command_id: UUID,
    scope: AccessScopeDependency,
    service: ProviderOperationsServiceDependency,
    history_limit: HistoryLimit = 50,
) -> ProviderOutboxDetail:
    """Return one safe tenant-scoped Outbox detail and bounded history."""
    return await service.get_outbox_detail(
        scope,
        command_id,
        history_limit=history_limit,
    )


@router.get(
    "/inbox/{inbox_id}",
    response_model=ProviderInboxDetail,
    responses=_READ_RESPONSES,
)
async def get_provider_inbox_detail(
    inbox_id: UUID,
    scope: AccessScopeDependency,
    service: ProviderOperationsServiceDependency,
    history_limit: HistoryLimit = 50,
) -> ProviderInboxDetail:
    """Return one safe tenant-scoped Inbox detail and bounded history."""
    return await service.get_inbox_detail(
        scope,
        inbox_id,
        history_limit=history_limit,
    )


@router.post(
    "/outbox/{command_id}/redrives",
    response_model=ProviderRedriveView,
    responses=_REDRIVE_RESPONSES,
)
async def redrive_provider_outbox(
    command_id: UUID,
    request: ProviderRedriveRequest,
    scope: AccessScopeDependency,
    service: ProviderOperationsServiceDependency,
) -> ProviderRedriveView:
    """Coordinate one synchronous Outbox recovery without invoking a worker."""
    return await service.redrive_outbox(scope, command_id, request)


@router.post(
    "/inbox/{inbox_id}/redrives",
    response_model=ProviderRedriveView,
    responses=_REDRIVE_RESPONSES,
)
async def redrive_provider_inbox(
    inbox_id: UUID,
    request: ProviderRedriveRequest,
    scope: AccessScopeDependency,
    service: ProviderOperationsServiceDependency,
) -> ProviderRedriveView:
    """Open one new Inbox processing cycle without invoking a worker."""
    return await service.redrive_inbox(scope, inbox_id, request)
