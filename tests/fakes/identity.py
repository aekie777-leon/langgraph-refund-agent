"""Test helpers for building trusted identities and scoped configs."""

from agent.auth.models import AccessScope, AuthenticatedIdentity, Role
from agent.auth.rbac import role_permissions


def make_identity(
    role: Role,
    *,
    user_id: str = "customer-a",
    tenant_id: str = "tenant-demo",
) -> AuthenticatedIdentity:
    """Build a canonical identity with permissions derived from its role."""
    return AuthenticatedIdentity(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        permissions=role_permissions(role),
    )


def make_scope(
    role: Role,
    *,
    user_id: str = "customer-a",
    tenant_id: str = "tenant-demo",
) -> AccessScope:
    """Build a canonical access scope for the given role."""
    return make_identity(role, user_id=user_id, tenant_id=tenant_id).scope()


def auth_user_payload(
    role: Role,
    *,
    user_id: str = "customer-a",
    tenant_id: str = "tenant-demo",
) -> dict:
    """Build the LangGraph auth-user payload injected into a runnable config."""
    identity = make_identity(role, user_id=user_id, tenant_id=tenant_id)
    return {
        "identity": identity.identity_key,
        "permissions": sorted(identity.permissions),
        "role": identity.role,
        "user_id": identity.user_id,
        "tenant_id": identity.tenant_id,
    }


def config_with_identity(
    role: Role = "customer",
    *,
    user_id: str = "customer-a",
    tenant_id: str = "tenant-demo",
    thread_id: str = "thread-1",
) -> dict:
    """Build a runnable config carrying a trusted identity and thread id."""
    return {
        "configurable": {
            "thread_id": thread_id,
            "langgraph_auth_user": auth_user_payload(
                role,
                user_id=user_id,
                tenant_id=tenant_id,
            ),
        }
    }
