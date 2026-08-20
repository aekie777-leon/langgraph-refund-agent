"""Verify one issuer's signed OIDC access tokens locally."""

from typing import Any

import jwt
from jwt.exceptions import PyJWTError

from agent.auth.claim_policy import identity_from_verified_claims
from agent.auth.config import OidcVerifierConfig
from agent.auth.jwks import AsyncJwksCache, UnknownSigningKeyError
from agent.auth.models import AuthenticatedIdentity
from agent.auth.provider import UnauthenticatedError

_MAX_ACCESS_TOKEN_LENGTH = 16_384
_MAX_KID_LENGTH = 256


class OidcJwtIdentityProvider:
    """Resolve Bearer access tokens using a fixed issuer and local JWKS."""

    def __init__(self, config: OidcVerifierConfig, jwks: AsyncJwksCache) -> None:
        """Bind the verifier to immutable trust and cache policies."""
        self._config = config
        self._jwks = jwks

    async def resolve(
        self, *, authorization_header: str | None
    ) -> AuthenticatedIdentity:
        """Validate credentials and map only trusted application claims."""
        token = _extract_bearer_token(authorization_header)
        try:
            header = jwt.get_unverified_header(token)
        except PyJWTError:
            raise UnauthenticatedError("invalid bearer credentials") from None
        kid = header.get("kid")
        algorithm = header.get("alg")
        if (
            not isinstance(kid, str)
            or not kid.strip()
            or len(kid) > _MAX_KID_LENGTH
            or not isinstance(algorithm, str)
            or algorithm not in self._config.algorithms
        ):
            raise UnauthenticatedError("invalid bearer credentials")

        try:
            signing_key = await self._jwks.get_signing_key(kid)
        except UnknownSigningKeyError:
            raise UnauthenticatedError("invalid bearer credentials") from None

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=signing_key,
                algorithms=sorted(self._config.algorithms),
                audience=self._config.audience,
                issuer=self._config.issuer,
                leeway=self._config.clock_skew_seconds,
                options={
                    "require": ["iss", "aud", "exp", "nbf", "iat"],
                    "strict_aud": True,
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except PyJWTError:
            raise UnauthenticatedError("invalid bearer credentials") from None
        return identity_from_verified_claims(claims, self._config.claims)

    async def preflight(self) -> None:
        """Require the configured JWKS endpoint to provide usable signing keys."""
        await self._jwks.refresh()


def _extract_bearer_token(authorization_header: str | None) -> str:
    scheme, separator, token = (authorization_header or "").partition(" ")
    normalized = token.strip()
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or not normalized
        or len(normalized) > _MAX_ACCESS_TOKEN_LENGTH
        or any(character.isspace() for character in normalized)
    ):
        raise UnauthenticatedError("invalid bearer credentials")
    return normalized
