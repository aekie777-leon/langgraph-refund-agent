"""Map roles to permissions and answer deterministic authorization questions."""

from agent.auth.models import (
    AccessScope,
    Permission,
    ProviderOperationsPermission,
    Role,
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    "customer": frozenset({"orders:read:own", "orders:operate:own", "cases:read:own"}),
    "support_agent": frozenset({"cases:read:assigned", "cases:update:assigned"}),
    "supervisor": frozenset(
        {
            "cases:read:own",
            "cases:read:assigned",
            "cases:read:all",
            "cases:update:assigned",
            "cases:update:all",
            "cases:assign",
            "provider_ops:read",
            "provider_ops:redrive",
        }
    ),
}


def role_permissions(role: Role) -> frozenset[Permission]:
    """Return the canonical permission set for a role."""
    return ROLE_PERMISSIONS[role]


def has_permission(scope: AccessScope, permission: Permission) -> bool:
    """Return whether the scope grants a single permission."""
    return permission in scope.permissions


def has_any_permission(scope: AccessScope, *permissions: Permission) -> bool:
    """Return whether the scope grants any of the requested permissions."""
    return any(permission in scope.permissions for permission in permissions)


def has_provider_operations_permission(
    scope: AccessScope,
    permission: ProviderOperationsPermission,
) -> bool:
    """Require both the supervisor role and one explicit Provider permission."""
    return scope.role == "supervisor" and permission in scope.permissions


def derive_role(permissions: frozenset[Permission]) -> Role:
    """Infer a role from a permission set, failing closed on unknown sets."""
    for role in ("customer", "support_agent", "supervisor"):
        if permissions == ROLE_PERMISSIONS[role]:
            return role
    raise ValueError("permission set does not exactly match a known role")
