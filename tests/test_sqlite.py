import sqlite3
from pathlib import Path

import pytest

from lore.db.sqlite import MIGRATIONS, open_db, schema_version


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_open_db_creates_schema(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    assert {"schema_migrations", "books", "jobs", "quota_ledger", "chunks", "gemini_ledger"} <= _tables(conn)
    assert schema_version(conn) == 7
    conn.close()


def test_open_db_is_idempotent(tmp_path: Path) -> None:
    open_db(tmp_path / "lore.db").close()
    conn = open_db(tmp_path / "lore.db")  # second open must not re-apply v1
    assert schema_version(conn) == 7
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

    # First, create the database successfully at v1 to v6
    conn = open_db(db_path)
    assert schema_version(conn) == 7
    conn.close()

    # Now simulate a failed migration with a statement that succeeds followed by one that fails
    # This tests that partial migrations are rolled back entirely
    broken_migration = (
        8,
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

    # Verify that open_db() works again and schema_version is still 6
    # (the failed migration should have left nothing behind due to atomicity)
    conn = open_db(db_path)
    assert schema_version(conn) == 7
    assert {"schema_migrations", "books", "jobs", "quota_ledger", "chunks", "gemini_ledger"} <= _tables(conn)

    # Most importantly: verify that extra_table does NOT exist
    # This proves the transaction was rolled back, not just the failed statement
    assert "extra_table" not in _tables(conn)
    conn.close()


def test_migration_v5_adds_series_columns_to_books(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(books)")}
    assert cols["series"] == "TEXT" and cols["series_index"] == "REAL"
    conn.close()


def test_migration_v5_keeps_existing_books(tmp_path: Path, monkeypatch) -> None:
    """A library made at v4 gains the columns, null, and loses nothing."""
    path = tmp_path / "lore.db"
    monkeypatch.setattr("lore.db.sqlite.MIGRATIONS", [m for m in MIGRATIONS if m[0] <= 4])
    conn = open_db(path)
    assert schema_version(conn) == 4
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('b1', 'Old Book', '/x.epub')")
    conn.commit()
    conn.close()
    monkeypatch.setattr("lore.db.sqlite.MIGRATIONS", MIGRATIONS)
    conn = open_db(path)
    assert schema_version(conn) == 7
    assert conn.execute("SELECT title, series, series_index FROM books").fetchall() == [
        ("Old Book", None, None)]
    conn.close()


def test_migration_v6_creates_book_jobs(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(book_jobs)")]
    assert cols == ["book_id", "stage", "state", "done_items", "total_items",
                    "stopped_reason", "resumable_at", "updated_at", "needs_reload"]
    conn.close()
