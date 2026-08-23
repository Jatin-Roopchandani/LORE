"""SQLite catalog/job store with a linear, idempotent migration runner."""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """
            CREATE TABLE books (
                id         TEXT PRIMARY KEY,          -- content hash of source file
                title      TEXT NOT NULL,
                author     TEXT,
                source     TEXT NOT NULL DEFAULT 'upload',
                file_path  TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'registered',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE jobs (
                id         INTEGER PRIMARY KEY,
                kind       TEXT NOT NULL,
                book_id    TEXT REFERENCES books(id),
                state      TEXT NOT NULL DEFAULT 'queued',
                detail     TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE quota_ledger (
                id               INTEGER PRIMARY KEY,
                run_ref          TEXT NOT NULL,
                gpu_hours_est    REAL NOT NULL,
                gpu_hours_actual REAL,
                week             TEXT NOT NULL,       -- ISO week, e.g. 2026-W34
                created_at       TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
        ],
    ),
]


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    current = schema_version(conn)
    try:
        for version, statements in MIGRATIONS:
            if version > current:
                try:
                    conn.execute("BEGIN")
                    for stmt in statements:
                        conn.execute(stmt)
                    conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
    except Exception:
        conn.close()
        raise
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0
