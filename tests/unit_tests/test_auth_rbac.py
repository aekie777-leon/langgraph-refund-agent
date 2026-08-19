"""Unit tests for role and permission mapping."""

import pytest

from agent.auth.models import Role
from agent.auth.rbac import (
    ROLE_PERMISSIONS,
    derive_role,
    has_any_permission,
    has_permission,
    has_provider_operations_permission,
    role_permissions,
)
from tests.fakes.identity import make_identity


def test_role_permissions_are_stable_per_role() -> None:
    assert role_permissions("customer") == frozenset(
        {"orders:read:own", "orders:operate:own", "cases:read:own"}
    )
    assert role_permissions("support_agent") == frozenset(
        {"cases:read:assigned", "cases:update:assigned"}
    )
    assert role_permissions("supervisor") == frozenset(
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
    )


def test_has_permission_reads_stable_permission_codes() -> None:
    scope = make_identity("customer").scope()

    assert has_permission(scope, "orders:read:own") is True
    assert has_permission(scope, "cases:read:all") is False


def test_has_any_permission_matches_any_requested_code() -> None:
    scope = make_identity("support_agent").scope()

    assert (
        has_any_permission(scope, "cases:read:assigned", "cases:update:assigned")
        is True
    )
    assert has_any_permission(scope, "cases:read:own", "cases:assign") is False
    assert has_any_permission(scope) is False


@pytest.mark.parametrize("role", ["customer", "support_agent", "supervisor"])
def test_derive_role_round_trips_every_role(role: Role) -> None:
    assert derive_role(role_permissions(role)) == role


def test_derive_role_rejects_unknown_permission_sets() -> None:
    with pytest.raises(ValueError):
        derive_role(frozenset({"cases:read:all"}))


def test_roles_and_permissions_are_modeled_separately() -> None:
    assert "cases:update:assigned" in ROLE_PERMISSIONS["supervisor"]
    assert derive_role(ROLE_PERMISSIONS["supervisor"]) == "supervisor"
    assert derive_role(ROLE_PERMISSIONS["supervisor"]) != "support_agent"


def test_provider_operations_requires_supervisor_and_matching_permission() -> None:
    supervisor = make_identity("supervisor").scope()

    assert has_provider_operations_permission(supervisor, "provider_ops:read") is True
    assert (
        has_provider_operations_permission(supervisor, "provider_ops:redrive") is True
    )

    missing_read = supervisor.model_copy(
        update={"permissions": supervisor.permissions - {"provider_ops:read"}}
    )
    assert (
        has_provider_operations_permission(missing_read, "provider_ops:read") is False
    )

    read_only = supervisor.model_copy(
        update={"permissions": supervisor.permissions - {"provider_ops:redrive"}}
    )
    assert has_provider_operations_permission(read_only, "provider_ops:read") is True
    assert (
        has_provider_operations_permission(read_only, "provider_ops:redrive") is False
    )

    forged_support_agent = (
        make_identity("support_agent")
        .scope()
        .model_copy(
            update={
                "permissions": frozenset(
                    {"cases:read:assigned", "provider_ops:read", "provider_ops:redrive"}
                )
            }
        )
    )
    assert (
        has_provider_operations_permission(forged_support_agent, "provider_ops:read")
        is False
    )
    assert (
        has_provider_operations_permission(forged_support_agent, "provider_ops:redrive")
        is False
    )


def test_derive_role_rejects_pre_provider_ops_supervisor_permission_set() -> None:
    previous_supervisor_permissions = ROLE_PERMISSIONS["supervisor"] - {
        "provider_ops:read",
        "provider_ops:redrive",
    }

    with pytest.raises(ValueError):
        derive_role(previous_supervisor_permissions)
