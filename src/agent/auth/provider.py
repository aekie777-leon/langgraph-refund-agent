"""Define the authentication boundary without choosing a vendor."""

from typing import Protocol

from agent.auth.models import AuthenticatedIdentity


class UnauthenticatedError(RuntimeError):
    """Report missing, malformed, or unknown credentials."""


class IdentityProvider(Protocol):
    """Resolve HTTP credentials into a trusted identity."""

    def resolve(self, *, authorization_header: str | None) -> AuthenticatedIdentity:
        """Return a trusted identity or raise UnauthenticatedError."""
        ...
