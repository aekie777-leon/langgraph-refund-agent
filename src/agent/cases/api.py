"""Expose internal HTTP routes for support-case operations."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from agent.cases.api_models import ApiErrorResponse, ChangeCaseStatusRequest
from agent.cases.models import (
    CaseListQuery,
    CasePriority,
    CaseServiceResult,
    CaseStatus,
    CaseType,
    SupportCase,
    SupportCaseEventPage,
    SupportCasePage,
)
from agent.cases.runtime import get_case_service
from agent.cases.service import CaseService

router = APIRouter(
    prefix="/internal/support-cases",
    tags=["Internal Support Cases"],
)

CaseServiceDependency = Annotated[
    CaseService,
    Depends(get_case_service),
]
Limit = Annotated[int, Query(ge=1, le=100)]
EventLimit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]
NonEmptyFilter = Annotated[
    str | None,
    Query(min_length=1, pattern=r".*\S.*"),
]

_COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ApiErrorResponse},
    409: {"model": ApiErrorResponse},
    503: {"model": ApiErrorResponse},
}


@router.get(
    "",
    response_model=SupportCasePage,
    responses={503: {"model": ApiErrorResponse}},
)
async def list_support_cases(
    service: CaseServiceDependency,
    status: CaseStatus | None = None,
    priority: CasePriority | None = None,
    case_type: CaseType | None = None,
    thread_id: NonEmptyFilter = None,
    order_id: NonEmptyFilter = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> SupportCasePage:
    """Return a filtered and stably ordered support-case page."""
    return await service.list_cases(
        CaseListQuery(
            status=status,
            priority=priority,
            case_type=case_type,
            thread_id=thread_id,
            order_id=order_id,
            limit=limit,
            offset=offset,
        )
    )


@router.get(
    "/{case_id}",
    response_model=SupportCase,
    responses=_COMMON_ERROR_RESPONSES,
)
async def get_support_case(
    case_id: UUID,
    service: CaseServiceDependency,
) -> SupportCase:
    """Return one support case by its stable identifier."""
    return await service.get_case(case_id)


@router.get(
    "/{case_id}/events",
    response_model=SupportCaseEventPage,
    responses=_COMMON_ERROR_RESPONSES,
)
async def list_support_case_events(
    case_id: UUID,
    service: CaseServiceDependency,
    limit: EventLimit = 100,
    offset: Offset = 0,
) -> SupportCaseEventPage:
    """Return one page of immutable support-case audit events."""
    return await service.list_case_events(
        case_id=case_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{case_id}/status",
    response_model=CaseServiceResult,
    responses=_COMMON_ERROR_RESPONSES,
)
async def change_support_case_status(
    case_id: UUID,
    request: ChangeCaseStatusRequest,
    service: CaseServiceDependency,
) -> CaseServiceResult:
    """Apply one idempotent support-case status transition."""
    return await service.change_status(
        case_id=case_id,
        target_status=request.target_status,
        request_id=request.request_id,
        actor=request.actor,
        on_hold_reason=request.on_hold_reason,
    )
