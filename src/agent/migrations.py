"""Discover and apply immutable versioned PostgreSQL migrations."""

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import psycopg

from agent.database import database_uri

MIGRATION_DIRECTORY = Path(__file__).resolve().parent / "sql" / "migrations"
MIGRATION_NAME_PATTERN = re.compile(r"^(?P<version>\d{4})_[a-z0-9_]+\.sql$")
MIGRATION_LOCK_ID = 726_451_903

_BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS case_management;

CREATE TABLE IF NOT EXISTS case_management.schema_migrations (
    version VARCHAR(4) PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    checksum CHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class MigrationError(RuntimeError):
    """Report invalid migration files or incompatible migration history."""


@dataclass(frozen=True, slots=True)
class Migration:
    """Contain one validated migration file and its checksum."""

    version: str
    filename: str
    checksum: str
    sql: str


def discover_migrations(directory: Path = MIGRATION_DIRECTORY) -> tuple[Migration, ...]:
    """Read migration files in version order and reject duplicate versions."""
    migrations: list[Migration] = []
    versions: set[str] = set()

    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME_PATTERN.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"Invalid migration filename: {path.name}")

        version = match.group("version")
        if version in versions:
            raise MigrationError(f"Duplicate migration version: {version}")
        versions.add(version)

        raw_sql = path.read_bytes()
        migrations.append(
            Migration(
                version=version,
                filename=path.name,
                checksum=sha256(raw_sql).hexdigest(),
                sql=raw_sql.decode("utf-8"),
            )
        )

    if not migrations:
        raise MigrationError(f"No migrations found in {directory}")
    return tuple(migrations)


def apply_migrations(conninfo: str | None = None) -> tuple[str, ...]:
    """Apply pending migrations and return the versions applied in this run."""
    migrations = discover_migrations()
    applied_now: list[str] = []

    with psycopg.connect(conninfo or database_uri(), autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        try:
            connection.execute(_BOOTSTRAP_SQL)
            rows = connection.execute(
                """
                SELECT version, filename, checksum
                FROM case_management.schema_migrations
                """
            ).fetchall()
            applied = {
                version: (filename, checksum) for version, filename, checksum in rows
            }

            for migration in migrations:
                previous = applied.get(migration.version)
                if previous is not None:
                    if previous != (migration.filename, migration.checksum):
                        raise MigrationError(
                            "Applied migration does not match the local file: "
                            f"{migration.filename}"
                        )
                    continue

                with connection.transaction():
                    connection.execute(migration.sql)
                    connection.execute(
                        """
                        INSERT INTO case_management.schema_migrations
                            (version, filename, checksum)
                        VALUES (%s, %s, %s)
                        """,
                        (
                            migration.version,
                            migration.filename,
                            migration.checksum,
                        ),
                    )
                applied_now.append(migration.version)
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))

    return tuple(applied_now)
