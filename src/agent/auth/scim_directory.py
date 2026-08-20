"""Read the minimum assignment-eligibility projection from SCIM 2.0."""

import json
from typing import Any, cast

import httpx
from pydantic import ValidationError

from agent.auth.config import ScimDirectoryConfig
from agent.auth.directory import DirectoryInfrastructureUnavailableError, DirectoryUser
from agent.auth.models import Role

_LIST_RESPONSE_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_SERVICE_PROVIDER_SCHEMA = (
    "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
)
_MAX_SCIM_RESPONSE_BYTES = 1_048_576


class ScimIdentityDirectory:
    """Query one read-only SCIM tenant directory with narrow projections."""

    def __init__(
        self,
        config: ScimDirectoryConfig,
        client: httpx.AsyncClient,
    ) -> None:
        """Bind the adapter to immutable endpoint, secret, and mapping policy."""
        self._config = config
        self._client = client
        self._users_url = f"{config.base_url.rstrip('/')}/Users"
        self._preflight_url = f"{config.base_url.rstrip('/')}/ServiceProviderConfig"

    async def find_user(
        self, *, tenant_id: str, user_id: str
    ) -> DirectoryUser | None:
        """Return one exact tenant-scoped user without retaining raw SCIM data."""
        attributes = ",".join(
            dict.fromkeys(
                (
                    self._config.user_id_attribute,
                    self._config.tenant_id_attribute,
                    self._config.active_attribute,
                    self._config.roles_attribute,
                )
            )
        )
        filter_value = (
            f"{self._config.user_id_attribute} eq {_scim_string(user_id)} and "
            f"{self._config.tenant_id_attribute} eq {_scim_string(tenant_id)}"
        )
        payload = await self._get_json(
            self._users_url,
            params={
                "filter": filter_value,
                "attributes": attributes,
                "startIndex": "1",
                "count": "2",
            },
        )
        return self._parse_user_response(
            payload,
            expected_tenant_id=tenant_id,
            expected_user_id=user_id,
        )

    async def preflight(self) -> None:
        """Require a reachable SCIM 2.0 service without mutating directory data."""
        payload = await self._get_json(self._preflight_url)
        schemas = payload.get("schemas") if isinstance(payload, dict) else None
        if not isinstance(schemas, list) or _SERVICE_PROVIDER_SCHEMA not in schemas:
            raise DirectoryInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            )

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        try:
            async with self._client.stream(
                "GET",
                url,
                params=params,
                headers={
                    "Accept": "application/scim+json",
                    "Authorization": (
                        "Bearer " + self._config.bearer_token.get_secret_value()
                    ),
                },
                timeout=self._config.timeout_seconds,
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_SCIM_RESPONSE_BYTES:
                        raise DirectoryInfrastructureUnavailableError(
                            "identity infrastructure is unavailable"
                        )
        except (httpx.HTTPError, ValueError):
            raise DirectoryInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            ) from None
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DirectoryInfrastructureUnavailableError(
                "identity infrastructure is unavailable"
            ) from None

    def _parse_user_response(
        self,
        payload: Any,
        *,
        expected_tenant_id: str,
        expected_user_id: str,
    ) -> DirectoryUser | None:
        if not isinstance(payload, dict):
            raise _directory_unavailable()
        schemas = payload.get("schemas")
        resources = payload.get("Resources")
        total_results = payload.get("totalResults")
        if (
            not isinstance(schemas, list)
            or _LIST_RESPONSE_SCHEMA not in schemas
            or not isinstance(resources, list)
            or not isinstance(total_results, int)
            or isinstance(total_results, bool)
            or total_results < 0
        ):
            raise _directory_unavailable()
        if total_results == 0:
            if resources:
                raise _directory_unavailable()
            return None
        if total_results != 1 or len(resources) != 1:
            raise _directory_unavailable()
        resource = resources[0]
        if not isinstance(resource, dict):
            raise _directory_unavailable()
        resource_schemas = resource.get("schemas")
        if not isinstance(resource_schemas, list) or _USER_SCHEMA not in resource_schemas:
            raise _directory_unavailable()

        user_id = _read_attribute(resource, self._config.user_id_attribute)
        tenant_id = _read_attribute(resource, self._config.tenant_id_attribute)
        active = _read_attribute(resource, self._config.active_attribute)
        raw_roles = _read_attribute(resource, self._config.roles_attribute)
        if (
            not isinstance(user_id, str)
            or not isinstance(tenant_id, str)
            or not isinstance(active, bool)
        ):
            raise _directory_unavailable()
        if user_id != expected_user_id or tenant_id != expected_tenant_id:
            return None
        external_roles = _parse_external_roles(raw_roles)
        roles = {
            role
            for role, mapped_values in self._config.role_mapping.items()
            if external_roles.intersection(mapped_values)
        }
        try:
            return DirectoryUser(
                user_id=user_id,
                tenant_id=tenant_id,
                active=active,
                roles=frozenset(cast(set[Role], roles)),
            )
        except ValidationError:
            raise _directory_unavailable() from None


def _scim_string(value: str) -> str:
    """Encode a SCIM comparison string using its JSON string grammar."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _read_attribute(resource: dict[str, Any], path: str) -> Any:
    if path in resource:
        return resource[path]
    candidates = sorted(
        (
            key
            for key in resource
            if isinstance(key, str) and path.startswith(f"{key}:")
        ),
        key=len,
        reverse=True,
    )
    if candidates:
        current: Any = resource[candidates[0]]
        remaining = path[len(candidates[0]) + 1 :]
    else:
        current = resource
        remaining = path
    for part in remaining.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _parse_external_roles(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        raise _directory_unavailable()
    roles: set[str] = set()
    for item in value:
        if isinstance(item, str) and item.strip():
            roles.add(item.strip())
            continue
        if isinstance(item, dict):
            role_value = item.get("value")
            if isinstance(role_value, str) and role_value.strip():
                roles.add(role_value.strip())
                continue
        raise _directory_unavailable()
    return frozenset(roles)


def _directory_unavailable() -> DirectoryInfrastructureUnavailableError:
    return DirectoryInfrastructureUnavailableError(
        "identity infrastructure is unavailable"
    )
