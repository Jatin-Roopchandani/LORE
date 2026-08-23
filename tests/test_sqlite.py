import sqlite3
from pathlib import Path

from lore.db.sqlite import open_db, schema_version


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_open_db_creates_schema(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    assert {"schema_migrations", "books", "jobs", "quota_ledger"} <= _tables(conn)
    assert schema_version(conn) == 1
    conn.close()


def test_open_db_is_idempotent(tmp_path: Path) -> None:
    open_db(tmp_path / "lore.db").close()
    conn = open_db(tmp_path / "lore.db")  # second open must not re-apply v1
    assert schema_version(conn) == 1
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
