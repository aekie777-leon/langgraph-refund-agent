"""Map support-case domain failures to safe HTTP responses."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.auth.directory import DirectoryInfrastructureUnavailableError
from agent.auth.visibility import ForbiddenError
from agent.cases.api_models import ApiErrorDetail, ApiErrorResponse
from agent.cases.policy import InvalidCaseStatusTransition
from agent.cases.repository import (
    CaseNotFoundError,
    CasePersistenceError,
    ConcurrentCaseUpdateError,
)
from agent.cases.service import AssignmentTargetUnavailableError


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    """Build the shared error envelope without exposing internal details."""
    payload = ApiErrorResponse(error=ApiErrorDetail(code=code, message=message))
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
    )


async def _forbidden_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    assert isinstance(error, ForbiddenError)
    return _error_response(
        status_code=403,
        code="forbidden",
        message="The authenticated caller is not allowed to perform this action.",
    )


async def _case_not_found_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    assert isinstance(error, CaseNotFoundError)
    return _error_response(
        status_code=404,
        code="case_not_found",
        message="The requested support case does not exist.",
    )


async def _assignment_target_unavailable_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    assert isinstance(error, AssignmentTargetUnavailableError)
    return _error_response(
        status_code=404,
        code="assignment_target_unavailable",
        message="The requested assignment target is not available.",
    )


async def _directory_unavailable_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    assert isinstance(error, DirectoryInfrastructureUnavailableError)
    return _error_response(
        status_code=503,
        code="identity_directory_unavailable",
        message="The identity directory is temporarily unavailable.",
    )


async def _invalid_transition_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    assert isinstance(error, InvalidCaseStatusTransition)
    return _error_response(
        status_code=409,
        code="invalid_case_status_transition",
        message="The requested support-case status transition is not allowed.",
    )


async def _concurrent_update_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    assert isinstance(error, ConcurrentCaseUpdateError)
    return _error_response(
        status_code=409,
        code="concurrent_case_update",
        message="The support case changed concurrently. Retry the request.",
    )


async def _persistence_failure_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    assert isinstance(error, CasePersistenceError)
    return _error_response(
        status_code=503,
        code="case_storage_unavailable",
        message="Support-case storage is temporarily unavailable.",
    )


def register_case_exception_handlers(app: FastAPI) -> None:
    """Register domain-specific handlers on the custom FastAPI app."""
    app.add_exception_handler(ForbiddenError, _forbidden_handler)
    app.add_exception_handler(CaseNotFoundError, _case_not_found_handler)
    app.add_exception_handler(
        AssignmentTargetUnavailableError,
        _assignment_target_unavailable_handler,
    )
    app.add_exception_handler(
        DirectoryInfrastructureUnavailableError,
        _directory_unavailable_handler,
    )
    app.add_exception_handler(
        InvalidCaseStatusTransition,
        _invalid_transition_handler,
    )
    app.add_exception_handler(
        ConcurrentCaseUpdateError,
        _concurrent_update_handler,
    )
    app.add_exception_handler(
        CasePersistenceError,
        _persistence_failure_handler,
    )
