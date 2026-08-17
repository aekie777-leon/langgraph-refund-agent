"""Unit tests for reading identities from trusted LangGraph config."""

import pytest

from agent.auth.context import (
    identity_from_user_payload,
    owner_filter,
    require_identity,
    require_scope,
)
from agent.auth.provider import UnauthenticatedError


class FakeUser:
    """Object-shaped auth user exposing identity and permission attributes."""

    def __init__(
        self,
        *,
        identity: str,
        permissions: list[str],
        role: str | None = None,
    ) -> None:
        self.identity = identity
        self.permissions = permissions
        self.role = role


def test_identity_from_dict_with_role_field() -> None:
    payload = {
        "identity": "tenant-demo:customer-a",
        "permissions": ["orders:read:own", "orders:operate:own", "cases:read:own"],
        "role": "customer",
    }

    identity = identity_from_user_payload(payload)

    assert identity.user_id == "customer-a"
    assert identity.tenant_id == "tenant-demo"
    assert identity.role == "customer"


def test_identity_from_dict_derives_role_when_field_missing() -> None:
    payload = {
        "identity": "tenant-demo:agent-7",
        "permissions": ["cases:read:assigned", "cases:update:assigned"],
    }

    identity = identity_from_user_payload(payload)

    assert identity.role == "support_agent"


def test_identity_from_object_shape() -> None:
    user = FakeUser(
        identity="tenant-demo:customer-a",
        permissions=["orders:read:own", "orders:operate:own", "cases:read:own"],
        role="customer",
    )

    identity = identity_from_user_payload(user)

    assert identity.user_id == "customer-a"
    assert identity.tenant_id == "tenant-demo"
    assert identity.role == "customer"


def test_missing_payload_is_rejected() -> None:
    with pytest.raises(UnauthenticatedError):
        identity_from_user_payload(None)


def test_invalid_identity_key_is_rejected() -> None:
    with pytest.raises(UnauthenticatedError):
        identity_from_user_payload(
            {"identity": "no-separator", "permissions": ["orders:read:own"]}
        )


def test_unknown_permission_is_rejected() -> None:
    with pytest.raises(UnauthenticatedError):
        identity_from_user_payload(
            {
                "identity": "tenant-demo:customer-a",
                "permissions": ["not-a-permission"],
                "role": "customer",
            }
        )


def test_role_and_permissions_mismatch_is_rejected() -> None:
    with pytest.raises(UnauthenticatedError):
        identity_from_user_payload(
            {
                "identity": "tenant-demo:customer-a",
                "permissions": [
                    "orders:read:own",
                    "orders:operate:own",
                    "cases:read:own",
                ],
                "role": "supervisor",
            }
        )


def test_require_identity_reads_trusted_config() -> None:
    config = {
        "configurable": {
            "langgraph_auth_user": {
                "identity": "tenant-demo:customer-a",
                "permissions": [
                    "orders:read:own",
                    "orders:operate:own",
                    "cases:read:own",
                ],
                "role": "customer",
            }
        }
    }

    identity = require_identity(config)

    assert identity.role == "customer"


def test_require_identity_fails_closed_when_missing() -> None:
    with pytest.raises(UnauthenticatedError):
        require_identity({"configurable": {}})


def test_require_scope_derives_an_access_scope() -> None:
    config = {
        "configurable": {
            "langgraph_auth_user": {
                "identity": "tenant-demo:customer-a",
                "permissions": [
                    "orders:read:own",
                    "orders:operate:own",
                    "cases:read:own",
                ],
                "role": "customer",
            }
        }
    }

    scope = require_scope(config)

    assert scope.user_id == "customer-a"
    assert scope.tenant_id == "tenant-demo"


def test_owner_filter_restricts_to_one_owner() -> None:
    identity = identity_from_user_payload(
        {
            "identity": "tenant-demo:customer-a",
            "permissions": [
                "orders:read:own",
                "orders:operate:own",
                "cases:read:own",
            ],
            "role": "customer",
        }
    )

    assert owner_filter(identity) == {
        "owner_user_id": "customer-a",
        "tenant_id": "tenant-demo",
    }
