"""Build PostgreSQL connection settings shared by application adapters."""

import ipaddress
import os
from collections.abc import Mapping

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool


def database_uri(environment: Mapping[str, str] | None = None) -> str:
    """Return a PostgreSQL URI from one URI or validated component variables."""
    selected_environment = os.environ if environment is None else environment
    if uri := selected_environment.get("POSTGRES_URI"):
        if selected_environment.get("APP_ENV") == "production":
            _require_external_tls_postgres(uri)
        return uri

    if selected_environment.get("APP_ENV") == "production":
        raise RuntimeError(
            "Production requires POSTGRES_URI for external TLS PostgreSQL"
        )

    components = {
        "POSTGRES_USER": selected_environment.get("POSTGRES_USER"),
        "POSTGRES_PASSWORD": selected_environment.get("POSTGRES_PASSWORD"),
        "POSTGRES_HOST": selected_environment.get("POSTGRES_HOST"),
        "POSTGRES_PORT": selected_environment.get("POSTGRES_PORT", "5432"),
        "POSTGRES_DB": selected_environment.get("POSTGRES_DB"),
    }
    missing = [name for name, value in components.items() if not value]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Missing PostgreSQL configuration: {names}")

    return make_conninfo(
        user=components["POSTGRES_USER"],
        password=components["POSTGRES_PASSWORD"],
        host=components["POSTGRES_HOST"],
        port=components["POSTGRES_PORT"],
        dbname=components["POSTGRES_DB"],
    )


def _require_external_tls_postgres(conninfo: str) -> None:
    """Reject local or plaintext PostgreSQL from the production profile."""
    try:
        values = conninfo_to_dict(conninfo)
    except ProgrammingError:
        raise RuntimeError("Production PostgreSQL configuration is invalid") from None

    if values.get("sslmode", "") not in {"require", "verify-ca", "verify-full"}:
        raise RuntimeError("Production PostgreSQL must require TLS")

    host_value = values.get("host")
    host_text = host_value if isinstance(host_value, str) else ""
    hosts = [host.strip() for host in host_text.split(",")]
    hostaddr_value = values.get("hostaddr")
    hostaddr_text = hostaddr_value if isinstance(hostaddr_value, str) else ""
    host_addresses = [
        host.strip() for host in hostaddr_text.split(",") if host.strip()
    ]
    if not hosts or any(not host or _is_local_postgres_host(host) for host in hosts):
        raise RuntimeError("Production PostgreSQL must be an external service")
    if any(_is_loopback_address(host) for host in host_addresses):
        raise RuntimeError("Production PostgreSQL must be an external service")


def _is_local_postgres_host(host: str) -> bool:
    normalized = host.strip("[]").rstrip(".").lower()
    if normalized in {"localhost", "postgres"} or normalized.endswith(".localhost"):
        return True
    if normalized.startswith("/"):
        return True
    return _is_loopback_address(normalized)


def _is_loopback_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.strip("[]")).is_loopback
    except ValueError:
        return False


def create_async_connection_pool(
    conninfo: str | None = None,
    *,
    min_size: int = 1,
    max_size: int = 10,
) -> AsyncConnectionPool:
    """Create a closed async pool for explicit application lifecycle control."""
    if min_size < 0:
        raise ValueError("min_size must not be negative")
    if max_size < 1 or max_size < min_size:
        raise ValueError("max_size must be positive and at least min_size")

    return AsyncConnectionPool(
        conninfo=conninfo or database_uri(),
        min_size=min_size,
        max_size=max_size,
        open=False,
    )
