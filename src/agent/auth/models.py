"""Trusted identity and authorization domain models."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["customer", "support_agent", "supervisor"]

ProviderOperationsPermission = Literal[
    "provider_ops:read",
    "provider_ops:redrive",
]

Permission = Literal[
    "orders:read:own",
    "orders:operate:own",
    "cases:read:own",
    "cases:read:assigned",
    "cases:read:all",
    "cases:update:assigned",
    "cases:update:all",
    "cases:assign",
    "provider_ops:read",
    "provider_ops:redrive",
]

_IDENTITY_SEPARATOR = ":"
_RESERVED_TENANT_IDS = frozenset({"legacy"})


def encode_identity_key(*, tenant_id: str, user_id: str) -> str:
    """Build a composite identity key from tenant and user components."""
    _validate_identity_part(tenant_id, "tenant_id")
    _validate_identity_part(user_id, "user_id")
    return f"{tenant_id}{_IDENTITY_SEPARATOR}{user_id}"


def decode_identity_key(key: str) -> tuple[str, str]:
    """Split a composite identity key back into tenant and user components."""
    if not isinstance(key, str) or key.count(_IDENTITY_SEPARATOR) != 1:
        raise ValueError("identity key must contain exactly one separator")
    tenant_id, user_id = key.split(_IDENTITY_SEPARATOR)
    _validate_identity_part(tenant_id, "tenant_id")
    _validate_identity_part(user_id, "user_id")
    return tenant_id, user_id


def _validate_identity_part(value: str, field: str) -> None:
    """Reject empty components and the separator reserved for the composite key."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if _IDENTITY_SEPARATOR in value:
        raise ValueError(f"{field} must not contain the separator ':'")
    if field == "tenant_id" and value.strip() in _RESERVED_TENANT_IDS:
        raise ValueError(f"{field} is reserved: {value.strip()}")


class AccessScope(BaseModel):
    """Represent the immutable access scope enforced by repositories."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    role: Role
    permissions: frozenset[Permission]

    @model_validator(mode="after")
    def validate_identity_parts(self) -> Self:
        """Reject scope components that cannot round-trip through the key."""
        _validate_identity_part(self.tenant_id, "tenant_id")
        _validate_identity_part(self.user_id, "user_id")
        return self

    @property
    def identity(self) -> str:
        """Return the composite owner key used for created_by stamps."""
        return encode_identity_key(tenant_id=self.tenant_id, user_id=self.user_id)

    @property
    def customer_id(self) -> str | None:
        """Return the customer identifier when the scope is a customer."""
        return self.user_id if self.role == "customer" else None


class AuthenticatedIdentity(BaseModel):
    """Represent a trusted identity produced by the authentication layer."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    user_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    role: Role
    permissions: frozenset[Permission]
    is_authenticated: Literal[True] = True

    @model_validator(mode="after")
    def validate_identity_parts(self) -> Self:
        """Reject identity components that cannot round-trip through the key."""
        _validate_identity_part(self.tenant_id, "tenant_id")
        _validate_identity_part(self.user_id, "user_id")
        return self

    @property
    def identity_key(self) -> str:
        """Return the composite key used as the LangGraph identity."""
        return encode_identity_key(tenant_id=self.tenant_id, user_id=self.user_id)

    @property
    def customer_id(self) -> str | None:
        """Return the customer identifier when the identity is a customer."""
        return self.user_id if self.role == "customer" else None

    def scope(self) -> AccessScope:
        """Derive the immutable access scope for this identity."""
        return AccessScope(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            role=self.role,
            permissions=self.permissions,
        )
