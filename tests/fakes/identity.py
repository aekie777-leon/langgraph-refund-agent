"""Test helpers for building trusted identities and scoped configs."""

from agent.auth.directory import DirectoryUser
from agent.auth.models import AccessScope, AuthenticatedIdentity, Role
from agent.auth.rbac import role_permissions


class FakeIdentityDirectory:
    """Return explicit narrow directory projections for service tests."""

    def __init__(self, users: tuple[DirectoryUser, ...]) -> None:
        self._users = {
            (user.tenant_id, user.user_id): user
            for user in users
        }
        self.calls: list[tuple[str, str]] = []

    async def find_user(
        self, *, tenant_id: str, user_id: str
    ) -> DirectoryUser | None:
        self.calls.append((tenant_id, user_id))
        return self._users.get((tenant_id, user_id))


def staff_directory() -> FakeIdentityDirectory:
    """Build the standard active demo staff directory used by case tests."""
    return FakeIdentityDirectory(
        (
            DirectoryUser(
                tenant_id="tenant-demo",
                user_id="agent-7",
                active=True,
                roles=frozenset({"support_agent"}),
            ),
            DirectoryUser(
                tenant_id="tenant-demo",
                user_id="agent-8",
                active=True,
                roles=frozenset({"support_agent"}),
            ),
            DirectoryUser(
                tenant_id="tenant-demo",
                user_id="sup-1",
                active=True,
                roles=frozenset({"supervisor"}),
            ),
        )
    )


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
