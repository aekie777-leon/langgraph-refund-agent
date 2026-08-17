"""Provide the application-scoped refund service used by graph nodes."""

from agent.refunds.service import RefundService

_refund_service: RefundService | None = None


def configure_refund_service(service: RefundService) -> None:
    """Register the refund service after application startup."""
    global _refund_service

    if _refund_service is not None:
        raise RuntimeError("Refund service has already been configured")
    _refund_service = service


def get_refund_service() -> RefundService:
    """Return the configured refund service."""
    if _refund_service is None:
        raise RuntimeError(
            "Refund service is unavailable because application startup has not completed"
        )
    return _refund_service


def clear_refund_service() -> None:
    """Remove the configured service during application shutdown."""
    global _refund_service
    _refund_service = None
