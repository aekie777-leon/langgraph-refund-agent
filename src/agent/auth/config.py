"""Load and validate the production identity trust contract."""

import json
from collections.abc import Mapping
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from agent.auth.models import Role
from agent.auth.provider import IdentityConfigurationError

ApplicationMode = Literal["development", "test", "production"]
IdentityBackend = Literal["demo", "oidc"]
DirectoryBackend = Literal["none", "scim"]
JwtAlgorithm = Literal[
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
]


class ClaimMappingConfig(BaseModel):
    """Select stable application claims and allowlisted role groups."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    user_id_claim: str = Field(min_length=1)
    tenant_id_claim: str = Field(min_length=1)
    groups_claim: str = Field(min_length=1)
    role_groups: dict[Role, frozenset[str]]

    @field_validator("user_id_claim", "tenant_id_claim", "groups_claim")
    @classmethod
    def validate_claim_name(cls, value: str) -> str:
        """Reject ambiguous or log-hostile claim names."""
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("claim names must not contain whitespace or controls")
        return value

    @model_validator(mode="after")
    def validate_role_groups(self) -> Self:
        """Require deterministic, non-overlapping external group mappings."""
        if not self.role_groups:
            raise ValueError("at least one role group mapping is required")
        seen: set[str] = set()
        for groups in self.role_groups.values():
            if not groups or any(not group.strip() for group in groups):
                raise ValueError("each configured role requires non-empty groups")
            normalized = {group.strip() for group in groups}
            if seen.intersection(normalized):
                raise ValueError("an external group cannot map to multiple roles")
            seen.update(normalized)
        return self


class OidcVerifierConfig(BaseModel):
    """Describe the fixed local-verification policy for one trusted issuer."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    jwks_url: str = Field(min_length=1)
    algorithms: frozenset[JwtAlgorithm]
    claims: ClaimMappingConfig
    jwks_cache_ttl_seconds: int = Field(default=300, ge=1, le=86_400)
    jwks_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    clock_skew_seconds: int = Field(default=60, ge=0, le=300)

    @model_validator(mode="after")
    def validate_trust_policy(self) -> Self:
        """Require an explicit algorithm allowlist and absolute HTTP endpoints."""
        if not self.algorithms:
            raise ValueError("at least one JWT algorithm is required")
        _validate_absolute_http_url(self.issuer, "issuer")
        _validate_absolute_http_url(self.jwks_url, "jwks_url")
        return self


class ScimDirectoryConfig(BaseModel):
    """Describe the minimum read-only SCIM directory contract."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    base_url: str = Field(min_length=1)
    bearer_token: SecretStr
    user_id_attribute: str = Field(min_length=1)
    tenant_id_attribute: str = Field(min_length=1)
    active_attribute: str = Field(min_length=1)
    roles_attribute: str = Field(min_length=1)
    role_mapping: dict[Role, frozenset[str]]
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    @field_validator(
        "user_id_attribute",
        "tenant_id_attribute",
        "active_attribute",
        "roles_attribute",
    )
    @classmethod
    def validate_attribute_path(cls, value: str) -> str:
        """Allow SCIM standard notation without filter control characters."""
        if any(
            not (
                character.isascii()
                and (character.isalnum() or character in {"$", "-", "_", ".", ":"})
            )
            for character in value
        ):
            raise ValueError("SCIM attribute paths contain unsupported characters")
        return value

    @model_validator(mode="after")
    def validate_directory_policy(self) -> Self:
        """Require a usable secret and an absolute SCIM endpoint."""
        if not self.bearer_token.get_secret_value().strip():
            raise ValueError("SCIM bearer token must not be empty")
        _validate_absolute_http_url(self.base_url, "base_url")
        if not self.role_mapping:
            raise ValueError("at least one SCIM role mapping is required")
        seen: set[str] = set()
        for roles in self.role_mapping.values():
            if not roles or any(not role.strip() for role in roles):
                raise ValueError("each SCIM role mapping requires non-empty values")
            normalized = {role.strip() for role in roles}
            if seen.intersection(normalized):
                raise ValueError("a SCIM role cannot map to multiple application roles")
            seen.update(normalized)
        return self


class IdentityRuntimeConfig(BaseModel):
    """Capture one explicit development, test, or production identity mode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: ApplicationMode
    identity_backend: IdentityBackend
    directory_backend: DirectoryBackend
    oidc: OidcVerifierConfig | None = None
    scim: ScimDirectoryConfig | None = None

    @model_validator(mode="after")
    def validate_selected_backends(self) -> Self:
        """Reject incomplete adapters and all production demo shortcuts."""
        if (self.identity_backend == "oidc") != (self.oidc is not None):
            raise ValueError("OIDC configuration must match the selected backend")
        if (self.directory_backend == "scim") != (self.scim is not None):
            raise ValueError("SCIM configuration must match the selected backend")
        if self.mode == "production":
            if self.identity_backend != "oidc":
                raise ValueError("production requires OIDC authentication")
            if self.directory_backend != "scim":
                raise ValueError("production requires a SCIM directory")
            assert self.oidc is not None
            assert self.scim is not None
            _require_https(self.oidc.issuer, "issuer")
            _require_https(self.oidc.jwks_url, "jwks_url")
            _require_https(self.scim.base_url, "SCIM base URL")
        return self

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str]
    ) -> "IdentityRuntimeConfig":
        """Build a typed configuration without including secret values in errors."""
        try:
            mode = _required(environment, "APP_ENV")
            identity_backend = _required(environment, "IDENTITY_PROVIDER")
            directory_backend = _required(environment, "IDENTITY_DIRECTORY")
            oidc = (
                _load_oidc_config(environment) if identity_backend == "oidc" else None
            )
            scim = (
                _load_scim_config(environment)
                if directory_backend == "scim"
                else None
            )
            return cls.model_validate(
                {
                    "mode": mode,
                    "identity_backend": identity_backend,
                    "directory_backend": directory_backend,
                    "oidc": oidc,
                    "scim": scim,
                }
            )
        except (IdentityConfigurationError, ValidationError, ValueError, TypeError):
            raise IdentityConfigurationError(
                "identity configuration is missing or invalid"
            ) from None

    def redacted_summary(self) -> dict[str, Any]:
        """Return only non-secret readiness facts suitable for structured logs."""
        return {
            "mode": self.mode,
            "identity_backend": self.identity_backend,
            "directory_backend": self.directory_backend,
            "oidc_configured": self.oidc is not None,
            "scim_configured": self.scim is not None,
        }


class ProductionSecurityControls(BaseModel):
    """Represent production-only controls owned outside identity adapters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    demo_tokens_configured: bool
    studio_auth_disabled: bool
    provider_allow_insecure_http: bool


def validate_production_security_controls(
    config: IdentityRuntimeConfig,
    controls: ProductionSecurityControls,
) -> None:
    """Reject demo credentials, Studio bypass, and plaintext provider traffic."""
    if config.mode != "production":
        return
    if controls.demo_tokens_configured:
        raise IdentityConfigurationError(
            "production security controls are missing or invalid"
        )
    if not controls.studio_auth_disabled:
        raise IdentityConfigurationError(
            "production security controls are missing or invalid"
        )
    if controls.provider_allow_insecure_http:
        raise IdentityConfigurationError(
            "production security controls are missing or invalid"
        )


def _load_oidc_config(environment: Mapping[str, str]) -> dict[str, Any]:
    role_groups = _load_json(environment, "OIDC_ROLE_GROUPS_JSON")
    return {
        "issuer": _required(environment, "OIDC_ISSUER"),
        "audience": _required(environment, "OIDC_AUDIENCE"),
        "jwks_url": _required(environment, "OIDC_JWKS_URL"),
        "algorithms": _csv(environment, "OIDC_ALLOWED_ALGORITHMS"),
        "claims": {
            "user_id_claim": _required(environment, "OIDC_USER_ID_CLAIM"),
            "tenant_id_claim": _required(environment, "OIDC_TENANT_ID_CLAIM"),
            "groups_claim": _required(environment, "OIDC_GROUPS_CLAIM"),
            "role_groups": role_groups,
        },
        "jwks_cache_ttl_seconds": _integer(
            environment, "OIDC_JWKS_CACHE_TTL_SECONDS", default=300
        ),
        "jwks_timeout_seconds": _floating(
            environment, "OIDC_JWKS_TIMEOUT_SECONDS", default=5.0
        ),
        "clock_skew_seconds": _integer(
            environment, "OIDC_CLOCK_SKEW_SECONDS", default=60
        ),
    }


def _load_scim_config(environment: Mapping[str, str]) -> dict[str, Any]:
    return {
        "base_url": _required(environment, "SCIM_BASE_URL"),
        "bearer_token": _required(environment, "SCIM_BEARER_TOKEN"),
        "user_id_attribute": _required(environment, "SCIM_USER_ID_ATTRIBUTE"),
        "tenant_id_attribute": _required(environment, "SCIM_TENANT_ID_ATTRIBUTE"),
        "active_attribute": _required(environment, "SCIM_ACTIVE_ATTRIBUTE"),
        "roles_attribute": _required(environment, "SCIM_ROLES_ATTRIBUTE"),
        "role_mapping": _load_json(environment, "SCIM_ROLE_MAPPING_JSON"),
        "timeout_seconds": _floating(environment, "SCIM_TIMEOUT_SECONDS", default=5.0),
    }


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise IdentityConfigurationError("required identity setting is missing")
    return value.strip()


def _load_json(environment: Mapping[str, str], name: str) -> Any:
    try:
        return json.loads(_required(environment, name))
    except json.JSONDecodeError:
        raise IdentityConfigurationError("identity JSON setting is invalid") from None


def _csv(environment: Mapping[str, str], name: str) -> list[str]:
    values = [value.strip() for value in _required(environment, name).split(",")]
    if not all(values):
        raise IdentityConfigurationError("identity list setting is invalid")
    return values


def _integer(environment: Mapping[str, str], name: str, *, default: int) -> int:
    try:
        return int(environment.get(name, str(default)))
    except ValueError:
        raise IdentityConfigurationError("identity integer setting is invalid") from None


def _floating(environment: Mapping[str, str], name: str, *, default: float) -> float:
    try:
        return float(environment.get(name, str(default)))
    except ValueError:
        raise IdentityConfigurationError("identity numeric setting is invalid") from None


def _validate_absolute_http_url(value: str, field: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field} must be an absolute HTTP URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError(f"{field} must not contain credentials or fragments")


def _require_https(value: str, field: str) -> None:
    if urlsplit(value).scheme != "https":
        raise ValueError(f"production {field} must use HTTPS")
