"""Postgres fixture for migration and schema tests.

Skipped locally when DATABASE_URL is unset. CI always sets it, and a missing
URL there fails the run instead of skipping the suite.
"""

import os
from pathlib import Path

import pytest

from tests.integration.sql_script import execute_sql_file, migration_paths

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
BOOTSTRAP = Path(__file__).resolve().parent / "bootstrap.sql"


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("DATABASE_URL is required in CI so integration tests run against Postgres")
    pytest.skip("DATABASE_URL is not set")


@pytest.fixture(scope="session")
def database_url():
    return _database_url()


@pytest.fixture(scope="session")
def migrated_database(database_url):
    """Apply the Supabase bootstrap and migrations 001..N to an empty database."""
    import psycopg

    with psycopg.connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                """
            )
            existing = cursor.fetchone()[0]
        if existing:
            raise RuntimeError(
                "Refusing to apply migrations: public already has tables. "
                "CI should start from an empty Postgres database."
            )
        execute_sql_file(connection, BOOTSTRAP)
        applied = []
        for path in migration_paths(MIGRATIONS):
            execute_sql_file(connection, path)
            applied.append(path.name)
        # The blank work-type migration is idempotent and already applied in prod.
        blank = MIGRATIONS / "013_work_type_blank.sql"
        if blank.exists():
            execute_sql_file(connection, blank)
    return {"url": database_url, "applied": applied}


@pytest.fixture
def db(migrated_database):
    import psycopg

    with psycopg.connect(migrated_database["url"]) as connection:
        yield connection
        connection.rollback()
