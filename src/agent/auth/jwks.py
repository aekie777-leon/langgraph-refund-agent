"""Fetch and cache a bounded set of trusted asymmetric JWT signing keys."""

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
from jwt import PyJWK
from jwt.exceptions import PyJWKError

from agent.auth.provider import IdentityInfrastructureUnavailableError

_MAX_JWKS_BYTES = 1_048_576
_MAX_SIGNING_KEYS = 32
_MAX_KID_LENGTH = 256


class UnknownSigningKeyError(RuntimeError):
    """Report that a refreshed trusted JWKS does not contain the requested key."""


class AsyncJwksCache:
    """Cache one issuer's JWKS with coalesced refresh and strict bounds."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        jwks_url: str,
        allowed_algorithms: frozenset[str],
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the immutable transport and trust policy."""
        self._client = client
        self._jwks_url = jwks_url
        self._allowed_algorithms = allowed_algorithms
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._keys: dict[str, PyJWK] = {}
        self._expires_at = 0.0
        self._generation = 0
        self._last_refresh_failed = False
        self._refresh_lock = asyncio.Lock()

    async def get_signing_key(self, kid: str) -> PyJWK:
        """Return a trusted key, refreshing at most once for an unknown kid."""
        if not isinstance(kid, str) or not kid.strip() or len(kid) > _MAX_KID_LENGTH:
            raise UnknownSigningKeyError("invalid signing key identifier")
        normalized_kid = kid.strip()
        now = self._clock()
        cached = self._keys.get(normalized_kid)
        if cached is not None and now < self._expires_at:
            return cached

        observed_generation = self._generation
        async with self._refresh_lock:
            now = self._clock()
            cached = self._keys.get(normalized_kid)
            if cached is not None and now < self._expires_at:
                return cached

            if self._generation == observed_generation:
                await self._refresh_locked()
            elif self._last_refresh_failed:
                raise IdentityInfrastructureUnavailableError(
                    "identity infrastructure is unavailable"
                )

            refreshed = self._keys.get(normalized_kid)
            if refreshed is None:
                raise UnknownSigningKeyError("unknown signing key")
            if self._clock() >= self._expires_at:
                raise IdentityInfrastructureUnavailableError(
                    "identity infrastructure is unavailable"
                )
            return refreshed

    async def refresh(self) -> None:
        """Force one bounded refresh for startup readiness checks."""
        async with self._refresh_lock:
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        try:
            payload = await self._fetch_payload()
            keys = _parse_signing_keys(payload, self._allowed_algorithms)
        except IdentityInfrastructureUnavailableError:
            self._generation += 1
            self._last_refresh_failed = True
            raise
        except Exception:
            self._generation += 1
            self._last_refresh_failed = True
            raise IdentityInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            ) from None
        self._keys = keys
        self._expires_at = self._clock() + self._ttl_seconds
        self._generation += 1
        self._last_refresh_failed = False

    async def _fetch_payload(self) -> Any:
        try:
            async with self._client.stream("GET", self._jwks_url) as response:
                response.raise_for_status()
                declared_length = response.headers.get("content-length")
                if declared_length is not None and int(declared_length) > _MAX_JWKS_BYTES:
                    raise IdentityInfrastructureUnavailableError(
                        "identity infrastructure is unavailable"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_JWKS_BYTES:
                        raise IdentityInfrastructureUnavailableError(
                            "identity infrastructure is unavailable"
                        )
        except (httpx.HTTPError, ValueError):
            raise IdentityInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            ) from None
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise IdentityInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            ) from None


def _parse_signing_keys(
    payload: Any,
    allowed_algorithms: frozenset[str],
) -> dict[str, PyJWK]:
    if not isinstance(payload, dict):
        raise IdentityInfrastructureUnavailableError(
            "identity infrastructure is unavailable"
        )
    raw_keys = payload.get("keys")
    if (
        not isinstance(raw_keys, list)
        or not raw_keys
        or len(raw_keys) > _MAX_SIGNING_KEYS
    ):
        raise IdentityInfrastructureUnavailableError(
            "identity infrastructure is unavailable"
        )

    parsed: dict[str, PyJWK] = {}
    for raw_key in raw_keys:
        if not isinstance(raw_key, dict):
            raise IdentityInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            )
        kid = raw_key.get("kid")
        if not isinstance(kid, str) or not kid.strip() or len(kid) > _MAX_KID_LENGTH:
            raise IdentityInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            )
        normalized_kid = kid.strip()
        if normalized_kid in parsed:
            raise IdentityInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            )
        if raw_key.get("use", "sig") != "sig":
            continue
        key_operations = raw_key.get("key_ops")
        if key_operations is not None and (
            not isinstance(key_operations, list) or "verify" not in key_operations
        ):
            continue
        declared_algorithm = raw_key.get("alg")
        if declared_algorithm is not None and declared_algorithm not in allowed_algorithms:
            continue
        try:
            key = PyJWK.from_dict(raw_key)
        except (PyJWKError, ValueError, TypeError):
            raise IdentityInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            ) from None
        if key.algorithm_name not in allowed_algorithms:
            continue
        parsed[normalized_kid] = key

    if not parsed:
        raise IdentityInfrastructureUnavailableError(
            "identity infrastructure is unavailable"
        )
    return parsed
