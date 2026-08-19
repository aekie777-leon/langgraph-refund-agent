"""Resolve the application-scoped Provider operations service for FastAPI."""

from fastapi import Request

from agent.integrations.provider_operations_service import ProviderOperationsService


def get_provider_operations_service(request: Request) -> ProviderOperationsService:
    """Return the service configured on the current FastAPI application."""
    service = getattr(request.app.state, "provider_operations_service", None)
    if not isinstance(service, ProviderOperationsService):
        raise RuntimeError(
            "Provider operations service is unavailable because application startup "
            "has not completed"
        )
    return service
