"""Build PostgreSQL connection settings shared by application adapters."""

import os

from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool


def database_uri() -> str:
    """Return a PostgreSQL URI from one URI or validated component variables."""
    if uri := os.getenv("POSTGRES_URI"):
        return uri

    environment = {
        "POSTGRES_USER": os.getenv("POSTGRES_USER"),
        "POSTGRES_PASSWORD": os.getenv("POSTGRES_PASSWORD"),
        "POSTGRES_HOST": os.getenv("POSTGRES_HOST"),
        "POSTGRES_PORT": os.getenv("POSTGRES_PORT", "5432"),
        "POSTGRES_DB": os.getenv("POSTGRES_DB"),
    }
    missing = [name for name, value in environment.items() if not value]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Missing PostgreSQL configuration: {names}")

    return make_conninfo(
        user=environment["POSTGRES_USER"],
        password=environment["POSTGRES_PASSWORD"],
        host=environment["POSTGRES_HOST"],
        port=environment["POSTGRES_PORT"],
        dbname=environment["POSTGRES_DB"],
    )


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
