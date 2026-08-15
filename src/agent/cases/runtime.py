"""Provide the application-scoped case service used by graph nodes."""

from agent.cases.service import CaseService

_case_service: CaseService | None = None


def configure_case_service(service: CaseService) -> None:
    """Register the case service after application startup."""
    global _case_service

    if _case_service is not None:
        raise RuntimeError("Case service has already been configured")
    _case_service = service


def get_case_service() -> CaseService:
    """Return the configured case service."""
    if _case_service is None:
        raise RuntimeError(
            "Case service is unavailable because application startup has not completed"
        )
    return _case_service


def clear_case_service() -> None:
    """Remove the configured service during application shutdown."""
    global _case_service
    _case_service = None
