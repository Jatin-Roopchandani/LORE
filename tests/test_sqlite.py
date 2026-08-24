import sqlite3
from pathlib import Path

import pytest

from lore.db.sqlite import MIGRATIONS, open_db, schema_version


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_open_db_creates_schema(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    assert {"schema_migrations", "books", "jobs", "quota_ledger", "chunks"} <= _tables(conn)
    assert schema_version(conn) == 2
    conn.close()


def test_open_db_is_idempotent(tmp_path: Path) -> None:
    open_db(tmp_path / "lore.db").close()
    conn = open_db(tmp_path / "lore.db")  # second open must not re-apply v1
    assert schema_version(conn) == 2
    conn.close()


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    try:
        conn.execute(
            "INSERT INTO jobs (kind, book_id, state) VALUES ('ingest', 'nope', 'queued')"
        )
        assert False, "expected FK violation"
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()


def test_migration_atomicity(tmp_path: Path, monkeypatch) -> None:
    """Verify that a failed migration leaves the database in a clean state.

    The broken migration succeeds on the first statement (CREATE TABLE extra_table)
    but fails on the second (invalid SQL). This tests that the entire transaction
    is rolled back, not just the failed statement.
    """
    db_path = tmp_path / "lore.db"

    # First, create the database successfully at v1 and v2
    conn = open_db(db_path)
    assert schema_version(conn) == 2
    conn.close()

    # Now simulate a failed migration with a statement that succeeds followed by one that fails
    # This tests that partial migrations are rolled back entirely
    broken_migration = (
        3,
        [
            "CREATE TABLE extra_table (x INTEGER)",
            "THIS IS NOT VALID SQL",
        ],
    )
    monkeypatch.setattr("lore.db.sqlite.MIGRATIONS", MIGRATIONS + [broken_migration])

    # Attempt to open the database with the broken migration
    with pytest.raises(sqlite3.OperationalError):
        open_db(db_path)

    # Remove the broken migration to restore normal behavior
    monkeypatch.setattr("lore.db.sqlite.MIGRATIONS", MIGRATIONS)

    # Verify that open_db() works again and schema_version is still 2
    # (the failed migration should have left nothing behind due to atomicity)
    conn = open_db(db_path)
    assert schema_version(conn) == 2
    assert {"schema_migrations", "books", "jobs", "quota_ledger", "chunks"} <= _tables(conn)

    # Most importantly: verify that extra_table does NOT exist
    # This proves the transaction was rolled back, not just the failed statement
    assert "extra_table" not in _tables(conn)
    conn.close()
