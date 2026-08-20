"""Authentication and authorization domain for the refund agent."""

from agent.auth.directory import (
    DirectoryInfrastructureUnavailableError,
    DirectoryUser,
    IdentityDirectory,
)
from agent.auth.models import (
    AccessScope,
    AuthenticatedIdentity,
    Permission,
    ProviderOperationsPermission,
    Role,
    decode_identity_key,
    encode_identity_key,
)
from agent.auth.provider import (
    IdentityConfigurationError,
    IdentityInfrastructureUnavailableError,
    IdentityProvider,
    UnauthenticatedError,
)

__all__ = [
    "AccessScope",
    "AuthenticatedIdentity",
    "DirectoryInfrastructureUnavailableError",
    "DirectoryUser",
    "IdentityConfigurationError",
    "IdentityDirectory",
    "IdentityInfrastructureUnavailableError",
    "IdentityProvider",
    "Permission",
    "ProviderOperationsPermission",
    "Role",
    "UnauthenticatedError",
    "decode_identity_key",
    "encode_identity_key",
]
