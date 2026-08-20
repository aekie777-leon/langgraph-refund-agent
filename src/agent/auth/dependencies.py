"""FastAPI dependencies that enforce the shared identity boundary."""

from typing import Annotated

from fastapi import Header, HTTPException, status

from agent.auth.models import AccessScope
from agent.auth.provider import (
    IdentityInfrastructureUnavailableError,
    IdentityProvider,
    UnauthenticatedError,
)
from agent.auth.runtime import get_identity_provider


async def parse_access_scope(
    *,
    authorization_header: str | None,
    provider: IdentityProvider,
) -> AccessScope:
    """Resolve an authorization header into an access scope."""
    identity = await provider.resolve(authorization_header=authorization_header)
    return identity.scope()


async def require_access_scope(
    authorization: Annotated[str | None, Header()] = None,
) -> AccessScope:
    """Return the caller access scope as a FastAPI dependency."""
    try:
        return await parse_access_scope(
            authorization_header=authorization,
            provider=get_identity_provider(),
        )
    except UnauthenticatedError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    except IdentityInfrastructureUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Identity service unavailable",
        ) from error
