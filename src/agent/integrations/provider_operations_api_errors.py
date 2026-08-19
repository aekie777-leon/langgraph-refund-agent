"""Map Provider operations failures to stable, non-sensitive HTTP responses."""

from collections.abc import Awaitable
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from agent.auth.visibility import ForbiddenError
from agent.cases.api_models import ApiErrorDetail, ApiErrorResponse
from agent.integrations.provider_operations_repository import (
    ProviderOperationsConflictError,
    ProviderOperationsNotFoundError,
    ProviderOperationsPersistenceError,
)

_PROVIDER_OPERATIONS_PATH_PREFIX: Final = "/internal/provider-operations"
_CONFLICT_MESSAGES: Final[dict[str, str]] = {
    "status_not_redrivable": (
        "The Provider resource is not eligible for redrive in its current state."
    ),
    "active_lease": "The Provider resource has an active processing lease.",
    "provider_rejection": "Provider business rejections cannot be redriven.",
    "current_cycle_terminal_evidence_required": (
        "Current-cycle terminal evidence is required before redrive."
    ),
    "technical_terminal_failure_required": (
        "Only technical terminal failures can be redriven."
    ),
    "lease_expiry_not_attempt_exhausting": (
        "Lease-expiry recovery has not exhausted the current attempt cycle."
    ),
    "redrive_state_changed": (
        "The Provider resource changed concurrently. Refresh and retry."
    ),
    "request_id_conflict": (
        "The redrive request identifier was already used for different parameters."
    ),
    "audit_conflict": "The redrive audit could not be recorded safely.",
    "aggregate_association_mismatch": (
        "The associated aggregate is not eligible for redrive."
    ),
    "aggregate_state_mismatch": (
        "The associated aggregate is not eligible for redrive."
    ),
    "review_case_association_mismatch": (
        "The associated aggregate is not eligible for redrive."
    ),
    "aggregate_state_changed": (
        "The associated aggregate changed concurrently. Refresh and retry."
    ),
}


def _error_response(*, status_code: int, code: str, message: str) -> JSONResponse:
    """Build the shared API error envelope from fixed safe values."""
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


async def _not_found_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    assert isinstance(error, ProviderOperationsNotFoundError)
    return _error_response(
        status_code=404,
        code="provider_operations_not_found",
        message="The requested Provider operations resource does not exist.",
    )


async def _conflict_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    assert isinstance(error, ProviderOperationsConflictError)
    if error.code in _CONFLICT_MESSAGES:
        code = error.code
        message = _CONFLICT_MESSAGES[error.code]
    else:
        code = "provider_operations_conflict"
        message = "The Provider operations request conflicts with the current state."
    return _error_response(status_code=409, code=code, message=message)


async def _persistence_handler(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    assert isinstance(error, ProviderOperationsPersistenceError)
    return _error_response(
        status_code=503,
        code="provider_operations_storage_unavailable",
        message="Provider operations storage is temporarily unavailable.",
    )


async def _safe_request_validation_handler(
    request: Request,
    error: Exception,
) -> Response:
    assert isinstance(error, RequestValidationError)
    if request.url.path.startswith(_PROVIDER_OPERATIONS_PATH_PREFIX):
        return _error_response(
            status_code=422,
            code="provider_operations_request_invalid",
            message="The Provider operations request is invalid.",
        )
    return await request_validation_exception_handler(request, error)


def register_provider_operations_exception_handlers(app: FastAPI) -> None:
    """Register safe handlers without replacing an existing shared 403 handler."""
    if ForbiddenError not in app.exception_handlers:
        app.add_exception_handler(ForbiddenError, _forbidden_handler)
    app.add_exception_handler(ProviderOperationsNotFoundError, _not_found_handler)
    app.add_exception_handler(ProviderOperationsConflictError, _conflict_handler)
    app.add_exception_handler(
        ProviderOperationsPersistenceError,
        _persistence_handler,
    )
    existing_validation_handler = app.exception_handlers.get(RequestValidationError)
    if existing_validation_handler is None:
        app.add_exception_handler(
            RequestValidationError,
            _safe_request_validation_handler,
        )
        return

    async def chained_validation_handler(
        request: Request,
        error: Exception,
    ) -> Response:
        if request.url.path.startswith(_PROVIDER_OPERATIONS_PATH_PREFIX):
            return await _safe_request_validation_handler(request, error)
        handler = existing_validation_handler
        result = handler(request, error)
        if isinstance(result, Awaitable):
            return await result
        return result

    app.add_exception_handler(RequestValidationError, chained_validation_handler)
