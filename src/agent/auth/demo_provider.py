"""Provide a deterministic demo identity provider backed by the environment."""

import json
import os
from typing import cast

from agent.auth.models import AuthenticatedIdentity, Role
from agent.auth.provider import UnauthenticatedError
from agent.auth.rbac import role_permissions

_VALID_ROLES = frozenset({"customer", "support_agent", "supervisor"})


class DemoIdentityProvider:
    """Resolve bearer tokens against a JSON map from the environment.

    Tokens exist only in the environment or local configuration, never in
    source code or persisted data.
    """

    def __init__(self, tokens: dict[str, dict[str, str]]) -> None:
        """Build the token-to-identity map from validated configuration."""
        self._identities: dict[str, AuthenticatedIdentity] = {}
        for token, entry in tokens.items():
            if not isinstance(token, str) or not token.strip():
                raise ValueError("demo identity token must be a non-empty string")
            self._identities[token] = self._build_identity(entry)

    @classmethod
    def from_env(cls, variable: str = "DEMO_IDENTITY_TOKENS") -> "DemoIdentityProvider":
        """Load demo identities from a JSON environment variable."""
        raw = os.getenv(variable)
        if not raw or not raw.strip():
            return cls({})
        try:
            tokens = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"{variable} is not valid JSON") from error
        if not isinstance(tokens, dict):
            raise RuntimeError(f"{variable} must be a JSON object")
        return cls(tokens)

    def resolve(self, *, authorization_header: str | None) -> AuthenticatedIdentity:
        """Resolve a bearer token into a trusted identity or reject it."""
        token = self._extract_bearer(authorization_header)
        identity = self._identities.get(token) if token is not None else None
        if identity is None:
            raise UnauthenticatedError("Unknown or missing demo token")
        return identity

    @staticmethod
    def _extract_bearer(authorization_header: str | None) -> str | None:
        scheme, _, token = (authorization_header or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return token.strip()

    @staticmethod
    def _build_identity(entry: dict[str, str]) -> AuthenticatedIdentity:
        if not isinstance(entry, dict):
            raise ValueError("demo identity entry must be a JSON object")
        user_id = entry.get("user_id")
        tenant_id = entry.get("tenant_id")
        role = entry.get("role")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("demo identity entry requires a non-empty user_id")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("demo identity entry requires a non-empty tenant_id")
        if not isinstance(role, str) or role not in _VALID_ROLES:
            raise ValueError(f"demo identity entry has an unsupported role: {role!r}")
        return AuthenticatedIdentity(
            user_id=user_id,
            tenant_id=tenant_id,
            role=cast(Role, role),
            permissions=role_permissions(cast(Role, role)),
        )
