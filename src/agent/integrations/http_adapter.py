"""Canonical outbound HTTP transport for provider commands."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Final

import httpx
from pydantic import ValidationError

from agent.integrations.models import (
    ProviderCommandEnvelope,
    ProviderCommandResult,
    ProviderConnection,
)
from agent.integrations.provider import ProviderCommandTransport
from agent.integrations.retry import (
    HTTPStatusError,
    ProviderConnectionError,
    ProviderTimeoutError,
)

MAX_PROVIDER_RESPONSE_BYTES: Final = 64 * 1024


class CanonicalHttpProviderTransport(ProviderCommandTransport):
    """Submit canonical command envelopes without leaking provider secrets."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Store an application-owned reusable HTTP client."""
        self._client = client

    async def send_command(
        self,
        *,
        connection: ProviderConnection,
        command: ProviderCommandEnvelope,
    ) -> ProviderCommandResult:
        """Post one envelope and strictly validate the immediate response."""
        headers = _headers_for(connection, command)
        timeout = httpx.Timeout(
            connect=connection.timeout.connect_seconds,
            read=connection.timeout.read_seconds,
            write=connection.timeout.write_seconds,
            pool=connection.timeout.connect_seconds,
        )
        url = str(connection.base_url).rstrip("/") + connection.endpoint
        try:
            async with self._client.stream(
                "POST",
                url,
                headers=headers,
                json=command.model_dump(mode="json"),
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise HTTPStatusError(
                        response.status_code,
                        retry_after_seconds=_retry_after_seconds(response),
                    )
                body = await _read_limited_body(response)
        except HTTPStatusError:
            raise
        except httpx.TimeoutException as error:
            raise ProviderTimeoutError("provider request timed out") from error
        except httpx.TransportError as error:
            raise ProviderConnectionError("provider transport request failed") from error

        try:
            result = ProviderCommandResult.model_validate_json(body)
        except ValidationError as error:
            raise ValueError("provider response failed validation") from error
        if result.command_id != command.command_id:
            raise ValueError("provider response command_id did not match request")
        return result


def _headers_for(
    connection: ProviderConnection,
    command: ProviderCommandEnvelope,
) -> dict[str, str]:
    """Build non-sensitive canonical headers and the selected authentication."""
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": command.idempotency_key,
        "X-Provider-Command-ID": str(command.command_id),
    }
    authentication = connection.authentication
    credential = authentication.credential
    if authentication.scheme == "bearer":
        assert credential is not None
        headers["Authorization"] = f"Bearer {credential.get_secret_value()}"
    elif authentication.scheme == "api_key":
        assert credential is not None and authentication.api_key_header is not None
        if authentication.api_key_header.lower() in {
            "authorization",
            "content-type",
            "idempotency-key",
            "x-provider-command-id",
        }:
            raise ValueError("api_key_header may not override a reserved transport header")
        headers[authentication.api_key_header] = credential.get_secret_value()
    return headers


async def _read_limited_body(response: httpx.Response) -> bytes:
    """Read at most the configured safe response size."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_PROVIDER_RESPONSE_BYTES:
            raise ValueError("provider response exceeded safe size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a safe Retry-After delta or HTTP date when one is supplied."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return None
        seconds = (parsed.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    if not isfinite(seconds):
        return None
    return max(0.0, seconds)
