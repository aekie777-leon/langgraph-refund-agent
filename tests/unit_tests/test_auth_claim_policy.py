"""Unit tests for mapping verified claims into server-owned RBAC scopes."""

import pytest

from agent.auth.claim_policy import identity_from_verified_claims
from agent.auth.config import ClaimMappingConfig
from agent.auth.provider import UnauthenticatedError


def _mapping() -> ClaimMappingConfig:
    return ClaimMappingConfig(
        user_id_claim="app_user_id",
        tenant_id_claim="app_tenant_id",
        groups_claim="app_groups",
        role_groups={
            "customer": frozenset({"refund-customers"}),
            "support_agent": frozenset({"refund-agents"}),
            "supervisor": frozenset({"refund-supervisors"}),
        },
    )


def test_verified_claims_derive_permissions_only_from_server_role_mapping() -> None:
    identity = identity_from_verified_claims(
        {
            "app_user_id": "agent-7",
            "app_tenant_id": "tenant-demo",
            "app_groups": ["refund-agents", "unrelated-group"],
        },
        _mapping(),
    )

    assert identity.role == "support_agent"
    assert identity.permissions == frozenset(
        {"cases:read:assigned", "cases:update:assigned"}
    )


def test_token_permissions_are_rejected_instead_of_trusted() -> None:
    with pytest.raises(UnauthenticatedError, match="permissions"):
        identity_from_verified_claims(
            {
                "app_user_id": "customer-a",
                "app_tenant_id": "tenant-demo",
                "app_groups": ["refund-customers"],
                "permissions": ["provider_ops:redrive"],
            },
            _mapping(),
        )


@pytest.mark.parametrize(
    "claims",
    [
        {
            "app_user_id": "customer-a",
            "app_tenant_id": "tenant-demo",
            "app_groups": [],
        },
        {
            "app_user_id": "customer-a",
            "app_tenant_id": "tenant-demo",
            "app_groups": ["unknown"],
        },
        {
            "app_user_id": "person-1",
            "app_tenant_id": "tenant-demo",
            "app_groups": ["refund-agents", "refund-supervisors"],
        },
        {
            "app_user_id": "person:1",
            "app_tenant_id": "tenant-demo",
            "app_groups": ["refund-agents"],
        },
    ],
)
def test_invalid_or_ambiguous_identity_claims_fail_closed(claims: dict) -> None:
    with pytest.raises(UnauthenticatedError):
        identity_from_verified_claims(claims, _mapping())
