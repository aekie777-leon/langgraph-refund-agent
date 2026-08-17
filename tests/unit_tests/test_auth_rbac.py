"""Unit tests for role and permission mapping."""

import pytest

from agent.auth.models import Role
from agent.auth.rbac import (
    ROLE_PERMISSIONS,
    derive_role,
    has_any_permission,
    has_permission,
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
