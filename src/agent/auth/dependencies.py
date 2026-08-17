"""FastAPI dependencies that enforce the shared identity boundary."""

from typing import Annotated

from fastapi import Header, HTTPException, status

from agent.auth.demo_provider import DemoIdentityProvider
from agent.auth.models import AccessScope
from agent.auth.provider import UnauthenticatedError


def parse_access_scope(
    *,
    authorization_header: str | None,
    provider: DemoIdentityProvider,
) -> AccessScope:
    """Resolve an authorization header into an access scope."""
    return provider.resolve(authorization_header=authorization_header).scope()


def require_access_scope(
    authorization: Annotated[str | None, Header()] = None,
) -> AccessScope:
    """Return the caller access scope as a FastAPI dependency."""
    try:
        return parse_access_scope(
            authorization_header=authorization,
            provider=DemoIdentityProvider.from_env(),
        )
    except UnauthenticatedError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
