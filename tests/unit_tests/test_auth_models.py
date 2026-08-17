"""Unit tests for the identity and access-scope domain models."""

import pytest
from pydantic import ValidationError

from agent.auth.models import (
    AccessScope,
    AuthenticatedIdentity,
    decode_identity_key,
    encode_identity_key,
)


def test_encode_and_decode_identity_key_round_trip() -> None:
    key = encode_identity_key(tenant_id="tenant-demo", user_id="customer-a")

    assert key == "tenant-demo:customer-a"
    assert decode_identity_key(key) == ("tenant-demo", "customer-a")


@pytest.mark.parametrize(
    "key",
    ["no-separator", "a:b:c", ":user", "tenant:", ""],
)
def test_decode_identity_key_rejects_malformed_keys(key: str) -> None:
    with pytest.raises(ValueError):
        decode_identity_key(key)


@pytest.mark.parametrize("value", ["a:b", "a:b:c", ":"])
def test_identity_rejects_separator_in_components(value: str) -> None:
    with pytest.raises(ValidationError):
        AuthenticatedIdentity(
            user_id=value,
            tenant_id="tenant-demo",
            role="customer",
            permissions=frozenset({"orders:read:own"}),
        )


def test_identity_key_matches_encoded_components() -> None:
    identity = AuthenticatedIdentity(
        user_id="customer-a",
        tenant_id="tenant-demo",
        role="customer",
        permissions=frozenset({"orders:read:own"}),
    )

    assert identity.identity_key == "tenant-demo:customer-a"


def test_customer_id_is_derived_only_for_customers() -> None:
    customer = AuthenticatedIdentity(
        user_id="customer-a",
        tenant_id="tenant-demo",
        role="customer",
        permissions=frozenset({"orders:read:own"}),
    )
    agent = AuthenticatedIdentity(
        user_id="agent-7",
        tenant_id="tenant-demo",
        role="support_agent",
        permissions=frozenset({"cases:read:assigned"}),
    )

    assert customer.customer_id == "customer-a"
    assert agent.customer_id is None


def test_scope_derives_the_immutable_access_scope() -> None:
    identity = AuthenticatedIdentity(
        user_id="customer-a",
        tenant_id="tenant-demo",
        role="customer",
        permissions=frozenset({"orders:read:own"}),
    )

    scope = identity.scope()

    assert isinstance(scope, AccessScope)
    assert scope.user_id == "customer-a"
    assert scope.tenant_id == "tenant-demo"
    assert scope.role == "customer"
    assert scope.permissions == frozenset({"orders:read:own"})


def test_identity_rejects_unknown_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AuthenticatedIdentity(
            user_id="customer-a",
            tenant_id="tenant-demo",
            role="customer",
            permissions=frozenset({"orders:read:own"}),
            token="should-not-exist",
        )


def test_identity_rejects_reserved_legacy_tenant() -> None:
    with pytest.raises(ValidationError):
        AuthenticatedIdentity(
            user_id="customer-a",
            tenant_id="legacy",
            role="customer",
            permissions=frozenset({"orders:read:own"}),
        )
