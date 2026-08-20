"""Define the vendor-neutral personnel-directory boundary."""

from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.auth.models import Role, encode_identity_key
from agent.auth.provider import IdentityInfrastructureUnavailableError


class DirectoryInfrastructureUnavailableError(
    IdentityInfrastructureUnavailableError
):
    """Report that personnel eligibility cannot be checked safely."""


class DirectoryUser(BaseModel):
    """Represent the narrow directory projection needed for assignment policy."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    user_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    active: bool
    roles: frozenset[Role]

    @model_validator(mode="after")
    def validate_identity_parts(self) -> Self:
        """Keep directory identifiers compatible with the canonical actor key."""
        encode_identity_key(tenant_id=self.tenant_id, user_id=self.user_id)
        return self


class IdentityDirectory(Protocol):
    """Look up the minimum trusted attributes needed for assignment."""

    async def find_user(
        self, *, tenant_id: str, user_id: str
    ) -> DirectoryUser | None:
        """Return a tenant-scoped directory user without exposing raw payloads."""
        ...
