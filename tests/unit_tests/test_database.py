"""Unit tests for PostgreSQL configuration and migration discovery."""

from hashlib import sha256
from pathlib import Path

import pytest

from agent.database import create_async_connection_pool, database_uri
from agent.migrations import MIGRATION_DIRECTORY, MigrationError, discover_migrations


def test_database_uri_prefers_complete_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_URI", "postgresql://user:secret@db/cases")
    monkeypatch.delenv("POSTGRES_USER", raising=False)

    assert database_uri() == "postgresql://user:secret@db/cases"


def test_database_uri_builds_from_components(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    monkeypatch.setenv("POSTGRES_USER", "case_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "safe-example")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "case_test")

    uri = database_uri()

    assert "user=case_user" in uri
    assert "password=safe-example" in uri
    assert "host=localhost" in uri
    assert "port=5433" in uri
    assert "dbname=case_test" in uri


def test_database_uri_reports_all_missing_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "POSTGRES_URI",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_DB",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="POSTGRES_USER.*POSTGRES_DB"):
        database_uri()


def test_async_pool_is_created_closed_for_explicit_lifecycle() -> None:
    pool = create_async_connection_pool(
        "postgresql://user:secret@localhost/cases",
        min_size=0,
        max_size=1,
    )

    assert pool.closed is True


@pytest.mark.parametrize(
    ("min_size", "max_size"),
    [(-1, 1), (2, 1), (0, 0)],
)
def test_async_pool_rejects_invalid_sizes(min_size: int, max_size: int) -> None:
    with pytest.raises(ValueError):
        create_async_connection_pool(
            "postgresql://user:secret@localhost/cases",
            min_size=min_size,
            max_size=max_size,
        )


def test_discover_migrations_orders_versions_and_hashes_bytes(tmp_path: Path) -> None:
    second = tmp_path / "0002_second.sql"
    first = tmp_path / "0001_first.sql"
    second.write_text("SELECT 2;\n", encoding="utf-8")
    first.write_text("SELECT 1;\n", encoding="utf-8")

    migrations = discover_migrations(tmp_path)

    assert [migration.version for migration in migrations] == ["0001", "0002"]
    assert migrations[0].checksum == sha256(first.read_bytes()).hexdigest()


def test_project_migrations_include_support_case_api_indexes() -> None:
    migrations = discover_migrations(MIGRATION_DIRECTORY)

    assert [migration.filename for migration in migrations] == [
        "0001_refund_requests.sql",
        "0002_support_cases.sql",
        "0003_support_case_api_indexes.sql",
    ]


def test_discover_migrations_rejects_invalid_filename(tmp_path: Path) -> None:
    (tmp_path / "migration.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(MigrationError, match="Invalid migration filename"):
        discover_migrations(tmp_path)


def test_discover_migrations_rejects_duplicate_version(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0001_duplicate.sql").write_text("SELECT 2;", encoding="utf-8")

    with pytest.raises(MigrationError, match="Duplicate migration version"):
        discover_migrations(tmp_path)
