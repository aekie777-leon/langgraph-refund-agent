"""Define the authentication boundary without choosing a vendor."""

from typing import Protocol

from agent.auth.models import AuthenticatedIdentity


class UnauthenticatedError(RuntimeError):
    """Report missing, malformed, or unknown credentials."""


class IdentityInfrastructureUnavailableError(RuntimeError):
    """Report that trusted identity infrastructure cannot be reached safely."""


class IdentityConfigurationError(RuntimeError):
    """Report an invalid or incomplete identity runtime configuration."""


class IdentityProvider(Protocol):
    """Resolve HTTP credentials into a trusted identity."""

    async def resolve(
        self, *, authorization_header: str | None
    ) -> AuthenticatedIdentity:
        """Return a trusted identity or raise UnauthenticatedError."""
        ...
