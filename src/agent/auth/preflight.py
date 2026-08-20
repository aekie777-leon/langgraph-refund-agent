"""Evaluate identity readiness without exposing external failure details."""

from collections.abc import Awaitable, Callable, Mapping

from agent.auth.config import IdentityRuntimeConfig
from agent.auth.provider import (
    IdentityConfigurationError,
    IdentityInfrastructureUnavailableError,
)

PreflightProbe = Callable[[], Awaitable[None]]

OIDC_JWKS_COMPONENT = "oidc_jwks"
SCIM_DIRECTORY_COMPONENT = "scim_directory"


async def require_identity_preflight(
    config: IdentityRuntimeConfig,
    probes: Mapping[str, PreflightProbe],
) -> tuple[str, ...]:
    """Require all selected production identity dependencies to be available."""
    required = _required_components(config)
    if not required.issubset(probes):
        raise IdentityConfigurationError("identity preflight is incomplete")

    checked: list[str] = []
    for component in sorted(required):
        try:
            await probes[component]()
        except IdentityInfrastructureUnavailableError:
            raise IdentityInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            ) from None
        except Exception:
            raise IdentityInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            ) from None
        checked.append(component)
    return tuple(checked)


def _required_components(config: IdentityRuntimeConfig) -> set[str]:
    if config.mode != "production":
        return set()
    return {OIDC_JWKS_COMPONENT, SCIM_DIRECTORY_COMPONENT}
