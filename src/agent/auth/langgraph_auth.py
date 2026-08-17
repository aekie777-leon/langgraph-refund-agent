"""LangGraph authentication and resource authorization handlers."""

from typing import Any

from langgraph_sdk import Auth

from agent.auth.context import identity_from_user_payload, owner_filter
from agent.auth.demo_provider import DemoIdentityProvider
from agent.auth.models import AuthenticatedIdentity
from agent.auth.provider import UnauthenticatedError

auth = Auth()

_provider = DemoIdentityProvider.from_env()


@auth.authenticate
async def authenticate(authorization: str | None) -> dict[str, Any]:
    """Authenticate a request and return its trusted identity claims."""
    try:
        identity = _provider.resolve(authorization_header=authorization)
    except UnauthenticatedError as error:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    return {
        "identity": identity.identity_key,
        "display_name": f"{identity.role}:{identity.user_id}",
        "is_authenticated": True,
        "permissions": sorted(identity.permissions),
        "user_id": identity.user_id,
        "tenant_id": identity.tenant_id,
        "role": identity.role,
    }


@auth.on
async def deny_by_default(ctx, value) -> bool:
    """Deny every request that lacks a specific authorization handler."""
    return False


@auth.on.threads.create
async def on_threads_create(ctx, value) -> None:
    """Stamp thread ownership metadata before creation."""
    identity = _identity_from_context(ctx)
    value.setdefault("metadata", {}).update(
        {
            "owner_user_id": identity.user_id,
            "tenant_id": identity.tenant_id,
            "owner_role": identity.role,
        }
    )


@auth.on(resources="threads", actions=["read", "search", "create_run"])
async def on_threads_scoped(ctx, value) -> dict[str, str]:
    """Scope thread reads, searches, and runs to the caller's own threads."""
    return owner_filter(_identity_from_context(ctx))


@auth.on(resources="threads", actions=["update", "delete"])
async def on_threads_mutation(ctx, value) -> bool:
    """Reject thread updates and deletions in v0.6."""
    return False


@auth.on(resources="assistants", actions=["read", "search"])
async def on_assistants_read(ctx, value) -> bool:
    """Allow authenticated users to read and search assistants."""
    return True


@auth.on(resources="assistants", actions=["create", "update", "delete"])
async def on_assistants_write(ctx, value) -> bool:
    """Reject assistant creation and mutation in v0.6."""
    return False


def _identity_from_context(ctx: Any) -> AuthenticatedIdentity:
    """Rebuild the trusted identity from a resource-handler context."""
    return identity_from_user_payload(ctx.user)
