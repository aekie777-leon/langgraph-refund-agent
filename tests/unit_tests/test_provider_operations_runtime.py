"""Unit tests for Provider operations app-state dependency resolution."""

from unittest.mock import Mock

from fastapi import FastAPI, Request

from agent.integrations.provider_operations_repository import (
    ProviderOperationsRepository,
)
from agent.integrations.provider_operations_runtime import (
    get_provider_operations_service,
)
from agent.integrations.provider_operations_service import ProviderOperationsService


def _request(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/internal/provider-operations/queues",
            "headers": [],
        }
    )


def test_configured_provider_operations_service_is_resolved() -> None:
    app = FastAPI()
    service = ProviderOperationsService(Mock(spec=ProviderOperationsRepository))
    app.state.provider_operations_service = service

    assert get_provider_operations_service(_request(app)) is service


def test_missing_provider_operations_service_fails_clearly() -> None:
    app = FastAPI()

    try:
        get_provider_operations_service(_request(app))
    except RuntimeError as error:
        assert "startup has not completed" in str(error)
    else:
        raise AssertionError("missing application service must fail")
