"""Read the authenticated identity from a trusted LangGraph config."""

from typing import Any, cast, get_args

from langchain_core.runnables import RunnableConfig

from agent.auth.models import (
    AccessScope,
    AuthenticatedIdentity,
    Permission,
    Role,
    decode_identity_key,
)
from agent.auth.provider import UnauthenticatedError
from agent.auth.rbac import derive_role

_AUTH_USER_KEY = "langgraph_auth_user"
_VALID_ROLES = frozenset(get_args(Role))
_VALID_PERMISSIONS = frozenset(get_args(Permission))


def identity_from_user_payload(payload: Any) -> AuthenticatedIdentity:
    """Rebuild a trusted identity from the LangGraph auth-user payload."""
    if payload is None:
        raise UnauthenticatedError("authenticated user is missing")

    identity_key = _read(payload, "identity")
    if not isinstance(identity_key, str) or not identity_key.strip():
        raise UnauthenticatedError("authenticated user has no identity key")
    try:
        tenant_id, user_id = decode_identity_key(identity_key)
    except ValueError as error:
        raise UnauthenticatedError(
            "authenticated user has an invalid identity key"
        ) from error

    raw_permissions = _read(payload, "permissions")
    if not isinstance(raw_permissions, (list, tuple, set, frozenset)):
        raise UnauthenticatedError("authenticated user has no permissions")
    validated_permissions: set[Permission] = set()
    for permission in raw_permissions:
        if permission not in _VALID_PERMISSIONS:
            raise UnauthenticatedError(f"unknown permission: {permission!r}")
        validated_permissions.add(cast(Permission, permission))
    permissions = frozenset(validated_permissions)

    try:
        derived_role = derive_role(permissions)
    except ValueError as error:
        raise UnauthenticatedError(
            "permission set does not map to a known role"
        ) from error

    role_value = _read(payload, "role")
    if isinstance(role_value, str) and role_value in _VALID_ROLES:
        role = cast(Role, role_value)
        if role != derived_role:
            raise UnauthenticatedError("role and permissions do not match")
    else:
        role = derived_role

    return AuthenticatedIdentity(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        permissions=permissions,
    )


def require_identity(config: RunnableConfig) -> AuthenticatedIdentity:
    """Return the caller identity from config, failing closed when missing."""
    configurable = config.get("configurable") or {}
    return identity_from_user_payload(configurable.get(_AUTH_USER_KEY))


def require_scope(config: RunnableConfig) -> AccessScope:
    """Return the caller access scope from config, failing closed when missing."""
    return require_identity(config).scope()


def owner_filter(identity: AuthenticatedIdentity) -> dict[str, str]:
    """Return a LangGraph filter restricting resources to one owner."""
    return {
        "owner_user_id": identity.user_id,
        "tenant_id": identity.tenant_id,
    }


def _read(payload: Any, key: str) -> Any:
    """Read one field from a dict-shaped or object-shaped auth user."""
    if isinstance(payload, dict):
        return payload.get(key)
    if hasattr(payload, key):
        return getattr(payload, key)
    try:
        return payload[key]
    except (KeyError, TypeError, IndexError):
        return None
