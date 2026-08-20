"""Unit tests for fail-closed identity startup preflight."""

import json

import pytest

from agent.auth.config import IdentityRuntimeConfig
from agent.auth.preflight import (
    OIDC_JWKS_COMPONENT,
    SCIM_DIRECTORY_COMPONENT,
    require_identity_preflight,
)
from agent.auth.provider import (
    IdentityConfigurationError,
    IdentityInfrastructureUnavailableError,
)

pytestmark = pytest.mark.anyio


def _production_config() -> IdentityRuntimeConfig:
    return IdentityRuntimeConfig.from_environment(
        {
            "APP_ENV": "production",
            "IDENTITY_PROVIDER": "oidc",
            "IDENTITY_DIRECTORY": "scim",
            "OIDC_ISSUER": "https://identity.example.com/",
            "OIDC_AUDIENCE": "refund-agent",
            "OIDC_JWKS_URL": "https://identity.example.com/jwks.json",
            "OIDC_ALLOWED_ALGORITHMS": "RS256",
            "OIDC_USER_ID_CLAIM": "app_user_id",
            "OIDC_TENANT_ID_CLAIM": "app_tenant_id",
            "OIDC_GROUPS_CLAIM": "app_groups",
            "OIDC_ROLE_GROUPS_JSON": json.dumps(
                {"support_agent": ["refund-agents"]}
            ),
            "SCIM_BASE_URL": "https://directory.example.com/scim/v2",
            "SCIM_BEARER_TOKEN": "secret",
            "SCIM_USER_ID_ATTRIBUTE": "externalId",
            "SCIM_TENANT_ID_ATTRIBUTE": "tenantId",
            "SCIM_ACTIVE_ATTRIBUTE": "active",
            "SCIM_ROLES_ATTRIBUTE": "roles",
            "SCIM_ROLE_MAPPING_JSON": json.dumps(
                {"support_agent": ["Refund Agent"]}
            ),
        }
    )


async def test_production_preflight_requires_and_checks_oidc_and_scim() -> None:
    calls: list[str] = []

    async def oidc_probe() -> None:
        calls.append(OIDC_JWKS_COMPONENT)

    async def scim_probe() -> None:
        calls.append(SCIM_DIRECTORY_COMPONENT)

    checked = await require_identity_preflight(
        _production_config(),
        {
            OIDC_JWKS_COMPONENT: oidc_probe,
            SCIM_DIRECTORY_COMPONENT: scim_probe,
        },
    )

    assert checked == (OIDC_JWKS_COMPONENT, SCIM_DIRECTORY_COMPONENT)
    assert calls == [OIDC_JWKS_COMPONENT, SCIM_DIRECTORY_COMPONENT]


async def test_production_preflight_rejects_missing_probe() -> None:
    async def oidc_probe() -> None:
        pass

    with pytest.raises(IdentityConfigurationError, match="incomplete"):
        await require_identity_preflight(
            _production_config(),
            {OIDC_JWKS_COMPONENT: oidc_probe},
        )


async def test_preflight_hides_external_failure_details() -> None:
    async def failing_probe() -> None:
        raise RuntimeError("GET https://idp.invalid/?token=must-not-leak")

    async def scim_probe() -> None:
        pass

    with pytest.raises(IdentityInfrastructureUnavailableError) as error:
        await require_identity_preflight(
            _production_config(),
            {
                OIDC_JWKS_COMPONENT: failing_probe,
                SCIM_DIRECTORY_COMPONENT: scim_probe,
            },
        )

    assert str(error.value) == "identity infrastructure is unavailable"
    assert "must-not-leak" not in str(error.value)


async def test_preflight_redacts_already_classified_outage_details() -> None:
    async def failing_probe() -> None:
        raise IdentityInfrastructureUnavailableError(
            "JWKS body includes must-not-leak"
        )

    async def scim_probe() -> None:
        pass

    with pytest.raises(IdentityInfrastructureUnavailableError) as error:
        await require_identity_preflight(
            _production_config(),
            {
                OIDC_JWKS_COMPONENT: failing_probe,
                SCIM_DIRECTORY_COMPONENT: scim_probe,
            },
        )

    assert str(error.value) == "identity infrastructure is unavailable"
    assert "must-not-leak" not in str(error.value)


async def test_development_demo_mode_has_no_external_preflight_dependency() -> None:
    config = IdentityRuntimeConfig.from_environment(
        {
            "APP_ENV": "development",
            "IDENTITY_PROVIDER": "demo",
            "IDENTITY_DIRECTORY": "none",
        }
    )

    assert await require_identity_preflight(config, {}) == ()
