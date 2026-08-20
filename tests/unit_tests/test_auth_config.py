"""Unit tests for explicit development and production identity configuration."""

import json

import pytest

from agent.auth.config import (
    IdentityRuntimeConfig,
    ProductionSecurityControls,
    validate_production_security_controls,
)
from agent.auth.provider import IdentityConfigurationError

_SCIM_SECRET = "scim-secret-that-must-not-leak"


def _production_environment() -> dict[str, str]:
    return {
        "APP_ENV": "production",
        "IDENTITY_PROVIDER": "oidc",
        "IDENTITY_DIRECTORY": "scim",
        "OIDC_ISSUER": "https://identity.example.com/",
        "OIDC_AUDIENCE": "refund-agent",
        "OIDC_JWKS_URL": "https://identity.example.com/.well-known/jwks.json",
        "OIDC_ALLOWED_ALGORITHMS": "RS256,ES256",
        "OIDC_USER_ID_CLAIM": "app_user_id",
        "OIDC_TENANT_ID_CLAIM": "app_tenant_id",
        "OIDC_GROUPS_CLAIM": "app_groups",
        "OIDC_ROLE_GROUPS_JSON": json.dumps(
            {
                "customer": ["refund-customers"],
                "support_agent": ["refund-agents"],
                "supervisor": ["refund-supervisors"],
            }
        ),
        "SCIM_BASE_URL": "https://directory.example.com/scim/v2",
        "SCIM_BEARER_TOKEN": _SCIM_SECRET,
        "SCIM_USER_ID_ATTRIBUTE": "externalId",
        "SCIM_TENANT_ID_ATTRIBUTE": "urn:example:tenantId",
        "SCIM_ACTIVE_ATTRIBUTE": "active",
        "SCIM_ROLES_ATTRIBUTE": "roles",
        "SCIM_ROLE_MAPPING_JSON": json.dumps(
            {
                "support_agent": ["Refund Agent"],
                "supervisor": ["Refund Supervisor"],
            }
        ),
    }


def test_production_configuration_requires_oidc_scim_and_https() -> None:
    config = IdentityRuntimeConfig.from_environment(_production_environment())

    assert config.mode == "production"
    assert config.identity_backend == "oidc"
    assert config.directory_backend == "scim"
    assert config.oidc is not None
    assert config.oidc.algorithms == frozenset({"RS256", "ES256"})
    assert config.scim is not None
    assert config.redacted_summary() == {
        "mode": "production",
        "identity_backend": "oidc",
        "directory_backend": "scim",
        "oidc_configured": True,
        "scim_configured": True,
    }


def test_secret_is_redacted_from_config_representation_and_summary() -> None:
    config = IdentityRuntimeConfig.from_environment(_production_environment())

    assert _SCIM_SECRET not in repr(config)
    assert _SCIM_SECRET not in str(config.redacted_summary())


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("IDENTITY_PROVIDER", "demo"),
        ("IDENTITY_DIRECTORY", "none"),
        ("OIDC_ISSUER", "http://identity.example.com/"),
        ("OIDC_JWKS_URL", "http://identity.example.com/jwks.json"),
        ("SCIM_BASE_URL", "http://directory.example.com/scim/v2"),
        ("OIDC_ALLOWED_ALGORITHMS", "HS256"),
    ],
)
def test_production_rejects_demo_missing_directory_plaintext_and_symmetric_jwt(
    setting: str,
    value: str,
) -> None:
    environment = _production_environment()
    environment[setting] = value

    with pytest.raises(IdentityConfigurationError) as error:
        IdentityRuntimeConfig.from_environment(environment)

    assert _SCIM_SECRET not in str(error.value)


def test_role_group_mapping_rejects_ambiguous_external_group() -> None:
    environment = _production_environment()
    environment["OIDC_ROLE_GROUPS_JSON"] = json.dumps(
        {
            "support_agent": ["refund-staff"],
            "supervisor": ["refund-staff"],
        }
    )

    with pytest.raises(IdentityConfigurationError):
        IdentityRuntimeConfig.from_environment(environment)


def test_scim_role_mapping_rejects_ambiguous_external_role() -> None:
    environment = _production_environment()
    environment["SCIM_ROLE_MAPPING_JSON"] = json.dumps(
        {
            "support_agent": ["Refund Staff"],
            "supervisor": ["Refund Staff"],
        }
    )

    with pytest.raises(IdentityConfigurationError):
        IdentityRuntimeConfig.from_environment(environment)


@pytest.mark.parametrize(
    ("setting", "mapping"),
    [
        (
            "OIDC_ROLE_GROUPS_JSON",
            {"supervisor": [" refund-supervisors"]},
        ),
        (
            "SCIM_ROLE_MAPPING_JSON",
            {"support_agent": ["Refund Agent "]},
        ),
    ],
)
def test_external_role_mappings_normalize_invisible_whitespace(
    setting: str,
    mapping: dict[str, list[str]],
) -> None:
    environment = _production_environment()
    environment[setting] = json.dumps(mapping)

    config = IdentityRuntimeConfig.from_environment(environment)

    if setting == "OIDC_ROLE_GROUPS_JSON":
        assert config.oidc is not None
        assert config.oidc.claims.role_groups == {
            "supervisor": frozenset({"refund-supervisors"})
        }
    else:
        assert config.scim is not None
        assert config.scim.role_mapping == {
            "support_agent": frozenset({"Refund Agent"})
        }


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("SCIM_USER_ID_ATTRIBUTE", 'externalId or userName pr'),
        ("SCIM_TENANT_ID_ATTRIBUTE", "tenant[id]"),
        ("SCIM_ROLES_ATTRIBUTE", "roles\nvalue"),
    ],
)
def test_scim_attribute_paths_reject_filter_control_syntax(
    setting: str,
    value: str,
) -> None:
    environment = _production_environment()
    environment[setting] = value

    with pytest.raises(IdentityConfigurationError):
        IdentityRuntimeConfig.from_environment(environment)


def test_development_demo_mode_must_be_selected_explicitly() -> None:
    config = IdentityRuntimeConfig.from_environment(
        {
            "APP_ENV": "development",
            "IDENTITY_PROVIDER": "demo",
            "IDENTITY_DIRECTORY": "none",
        }
    )

    assert config.identity_backend == "demo"
    assert config.oidc is None
    assert config.scim is None


def test_missing_explicit_mode_fails_closed() -> None:
    with pytest.raises(IdentityConfigurationError):
        IdentityRuntimeConfig.from_environment({})


@pytest.mark.parametrize(
    "controls",
    [
        ProductionSecurityControls(
            demo_tokens_configured=True,
            studio_auth_disabled=True,
            provider_allow_insecure_http=False,
        ),
        ProductionSecurityControls(
            demo_tokens_configured=False,
            studio_auth_disabled=False,
            provider_allow_insecure_http=False,
        ),
        ProductionSecurityControls(
            demo_tokens_configured=False,
            studio_auth_disabled=True,
            provider_allow_insecure_http=True,
        ),
    ],
)
def test_production_security_controls_reject_every_demo_bypass(
    controls: ProductionSecurityControls,
) -> None:
    config = IdentityRuntimeConfig.from_environment(_production_environment())

    with pytest.raises(IdentityConfigurationError):
        validate_production_security_controls(config, controls)


def test_production_requires_studio_requests_to_use_custom_auth() -> None:
    config = IdentityRuntimeConfig.from_environment(_production_environment())

    validate_production_security_controls(
        config,
        ProductionSecurityControls(
            demo_tokens_configured=False,
            studio_auth_disabled=True,
            provider_allow_insecure_http=False,
        ),
    )
