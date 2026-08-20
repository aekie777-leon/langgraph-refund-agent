"""Tests for the explicit development-only showcase boundary."""

import pytest

from agent.showcase.config import showcase_enabled, validate_showcase_environment


def test_showcase_requires_explicit_true_value() -> None:
    assert showcase_enabled({"SHOWCASE_MODE": "true"}) is True
    assert showcase_enabled({"SHOWCASE_MODE": "TRUE"}) is True
    assert showcase_enabled({"SHOWCASE_MODE": "false"}) is False
    assert showcase_enabled({}) is False


@pytest.mark.parametrize("app_env", ["production", "test", "", "Development "])
def test_showcase_rejects_every_noncanonical_development_mode(app_env: str) -> None:
    environment = {
        "SHOWCASE_MODE": "true",
        "APP_ENV": app_env,
        "IDENTITY_PROVIDER": "demo",
    }
    if app_env == "Development ":
        validate_showcase_environment(environment)
    else:
        with pytest.raises(RuntimeError, match="APP_ENV=development"):
            validate_showcase_environment(environment)


def test_showcase_requires_demo_identity() -> None:
    with pytest.raises(RuntimeError, match="demo identity provider"):
        validate_showcase_environment(
            {
                "SHOWCASE_MODE": "true",
                "APP_ENV": "development",
                "IDENTITY_PROVIDER": "oidc",
            }
        )


def test_disabled_showcase_does_not_constrain_other_modes() -> None:
    validate_showcase_environment(
        {
            "SHOWCASE_MODE": "false",
            "APP_ENV": "production",
            "IDENTITY_PROVIDER": "oidc",
        }
    )

