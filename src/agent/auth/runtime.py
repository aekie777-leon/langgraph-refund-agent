"""Build and own the single identity runtime shared by every HTTP surface."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from agent.auth.config import (
    IdentityRuntimeConfig,
    ProductionSecurityControls,
    validate_production_security_controls,
)
from agent.auth.demo_provider import DemoIdentityProvider
from agent.auth.directory import IdentityDirectory
from agent.auth.jwks import AsyncJwksCache
from agent.auth.oidc_provider import OidcJwtIdentityProvider
from agent.auth.preflight import (
    OIDC_JWKS_COMPONENT,
    SCIM_DIRECTORY_COMPONENT,
    PreflightProbe,
    require_identity_preflight,
)
from agent.auth.provider import IdentityProvider
from agent.auth.scim_directory import ScimIdentityDirectory


@dataclass(frozen=True)
class IdentityRuntime:
    """Own one immutable provider/config pair and its optional HTTP client."""

    config: IdentityRuntimeConfig
    provider: IdentityProvider
    directory: IdentityDirectory | None
    _owned_http_client: httpx.AsyncClient | None = None

    async def preflight(
        self,
        *,
        additional_probes: Mapping[str, PreflightProbe] | None = None,
    ) -> tuple[str, ...]:
        """Verify every production dependency selected by the runtime."""
        probes = dict(additional_probes or {})
        if isinstance(self.provider, OidcJwtIdentityProvider):
            probes[OIDC_JWKS_COMPONENT] = self.provider.preflight
        if isinstance(self.directory, ScimIdentityDirectory):
            probes[SCIM_DIRECTORY_COMPONENT] = self.directory.preflight
        return await require_identity_preflight(self.config, probes)

    async def close(self) -> None:
        """Close only the transport created and owned by this runtime."""
        if self._owned_http_client is not None:
            await self._owned_http_client.aclose()


_runtime: IdentityRuntime | None = None


def create_identity_runtime(
    environment: Mapping[str, str] | None = None,
    *,
    studio_auth_disabled: bool,
    http_client: httpx.AsyncClient | None = None,
) -> IdentityRuntime:
    """Build one runtime from explicit mode and trust configuration."""
    selected_environment = os.environ if environment is None else environment
    config = IdentityRuntimeConfig.from_environment(selected_environment)
    validate_production_security_controls(
        config,
        ProductionSecurityControls(
            demo_tokens_configured=bool(
                selected_environment.get("DEMO_IDENTITY_TOKENS", "").strip()
            ),
            studio_auth_disabled=studio_auth_disabled,
            provider_allow_insecure_http=(
                selected_environment.get("PROVIDER_ALLOW_INSECURE_HTTP", "").lower()
                == "true"
            ),
        ),
    )

    owned_client = None
    if http_client is None and (
        config.identity_backend == "oidc" or config.directory_backend == "scim"
    ):
        timeout = (
            config.oidc.jwks_timeout_seconds
            if config.oidc is not None
            else config.scim.timeout_seconds
            if config.scim is not None
            else 5.0
        )
        owned_client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
        )
        http_client = owned_client

    if config.identity_backend == "demo":
        provider: IdentityProvider = DemoIdentityProvider.from_environment(
            selected_environment
        )
    else:
        assert config.oidc is not None
        assert http_client is not None
        cache = AsyncJwksCache(
            client=http_client,
            jwks_url=config.oidc.jwks_url,
            allowed_algorithms=frozenset(config.oidc.algorithms),
            ttl_seconds=config.oidc.jwks_cache_ttl_seconds,
        )
        provider = OidcJwtIdentityProvider(config.oidc, cache)

    if config.directory_backend == "scim":
        assert config.scim is not None
        assert http_client is not None
        directory: IdentityDirectory | None = ScimIdentityDirectory(
            config.scim, http_client
        )
    elif isinstance(provider, DemoIdentityProvider):
        directory = provider
    else:
        directory = None
    return IdentityRuntime(
        config=config,
        provider=provider,
        directory=directory,
        _owned_http_client=owned_client,
    )


async def initialize_identity_runtime(
    environment: Mapping[str, str] | None = None,
    *,
    studio_auth_disabled: bool,
    http_client: httpx.AsyncClient | None = None,
    additional_probes: Mapping[str, PreflightProbe] | None = None,
) -> IdentityRuntime:
    """Build, preflight, and publish the process-wide identity runtime."""
    runtime = create_identity_runtime(
        environment,
        studio_auth_disabled=studio_auth_disabled,
        http_client=http_client,
    )
    try:
        await runtime.preflight(additional_probes=additional_probes)
        configure_identity_runtime(runtime)
    except BaseException:
        await runtime.close()
        raise
    return runtime


def configure_identity_runtime(runtime: IdentityRuntime) -> None:
    """Publish one runtime after successful startup preflight."""
    global _runtime
    if _runtime is not None:
        raise RuntimeError("Identity runtime has already been configured")
    _runtime = runtime


def get_identity_runtime() -> IdentityRuntime:
    """Return the shared runtime or fail closed before startup completes."""
    if _runtime is None:
        raise RuntimeError(
            "Identity runtime is unavailable because application startup has not completed"
        )
    return _runtime


def get_identity_provider() -> IdentityProvider:
    """Return the provider used by both FastAPI and LangGraph authentication."""
    return get_identity_runtime().provider


async def shutdown_identity_runtime() -> None:
    """Unpublish and close the current identity runtime."""
    global _runtime
    runtime, _runtime = _runtime, None
    if runtime is not None:
        await runtime.close()
