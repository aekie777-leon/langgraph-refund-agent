"""Map verified OIDC claims into the canonical server-owned access scope."""

from collections.abc import Mapping, Sequence
from typing import Any, cast

from agent.auth.config import ClaimMappingConfig
from agent.auth.models import AuthenticatedIdentity, Role
from agent.auth.provider import UnauthenticatedError
from agent.auth.rbac import role_permissions


def identity_from_verified_claims(
    claims: Mapping[str, Any],
    mapping: ClaimMappingConfig,
) -> AuthenticatedIdentity:
    """Build an identity from already verified claims or fail closed."""
    if "permissions" in claims:
        raise UnauthenticatedError("token permissions are not accepted")

    user_id = _required_string_claim(claims, mapping.user_id_claim)
    tenant_id = _required_string_claim(claims, mapping.tenant_id_claim)
    groups = _required_string_sequence_claim(claims, mapping.groups_claim)

    matched_roles = {
        role
        for role, allowed_groups in mapping.role_groups.items()
        if groups.intersection(allowed_groups)
    }
    if len(matched_roles) != 1:
        raise UnauthenticatedError("identity role mapping is invalid")
    role = cast(Role, matched_roles.pop())

    try:
        return AuthenticatedIdentity(
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            permissions=role_permissions(role),
        )
    except ValueError:
        raise UnauthenticatedError("identity claims are invalid") from None


def _required_string_claim(claims: Mapping[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise UnauthenticatedError("required identity claim is invalid")
    return value.strip()


def _required_string_sequence_claim(
    claims: Mapping[str, Any], name: str
) -> frozenset[str]:
    value = claims.get(name)
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
        or any(not isinstance(group, str) or not group.strip() for group in value)
    ):
        raise UnauthenticatedError("required identity groups claim is invalid")
    return frozenset(group.strip() for group in value)
