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
    (
        2,
        [
            """
            CREATE TABLE chunks (
                id          TEXT PRIMARY KEY,
                book_id     TEXT NOT NULL REFERENCES books(id),
                chapter_seq INTEGER NOT NULL,
                scene_seq   INTEGER NOT NULL,
                seq         INTEGER NOT NULL,
                text        TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            "CREATE INDEX idx_chunks_book ON chunks(book_id)",
        ],
    ),
    (3, ["ALTER TABLE jobs ADD COLUMN error TEXT"]),
    (
        4,
        [
            """
            CREATE TABLE gemini_ledger (
                id    INTEGER PRIMARY KEY,
                day   TEXT NOT NULL,        -- local date, e.g. 2026-09-24
                model TEXT NOT NULL,        -- gemini-3.5-flash-lite
                count INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE UNIQUE INDEX idx_gemini_ledger_day_model"
            " ON gemini_ledger(day, model)",
        ],
    ),
    # Story position (2026-10-28): which series a book belongs to and where.
    # Both optional; two books are ordered only when both are set.
    (
        5,
        [
            "ALTER TABLE books ADD COLUMN series TEXT",
            "ALTER TABLE books ADD COLUMN series_index REAL",
        ],
    ),
    # Resumable processing (2026-10-28): one row per book saying where its
    # processing stands. Times are ISO-8601 UTC strings.
    (
        6,
        [
            """
            CREATE TABLE book_jobs (
                book_id        TEXT PRIMARY KEY REFERENCES books(id),
                stage          TEXT NOT NULL,
                state          TEXT NOT NULL,     -- running, waiting_quota, failed, done, idle
                done_items     INTEGER NOT NULL DEFAULT 0,
                total_items    INTEGER NOT NULL DEFAULT 0,
                stopped_reason TEXT,
                resumable_at   TEXT,              -- set only when waiting_quota
                updated_at     TEXT NOT NULL
            )
            """,
        ],
    ),
    # Disambiguation answers saved but not yet applied by a reload.
    (7, ["ALTER TABLE book_jobs ADD COLUMN needs_reload INTEGER NOT NULL DEFAULT 0"]),
    # Story beats (2026-10-28): quote-anchored moments per chapter part, and
    # the entity names each involves (entity_id is filled in by relink).
    (
        8,
        [
            """
            CREATE TABLE beats (
                id          TEXT PRIMARY KEY,
                book_id     TEXT NOT NULL REFERENCES books(id),
                chapter_seq INTEGER NOT NULL,
                part        INTEGER NOT NULL DEFAULT 0,
                chunk_id    TEXT NOT NULL,
                kind        TEXT NOT NULL,
                text        TEXT NOT NULL,
                quote       TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            "CREATE INDEX idx_beats_part ON beats(book_id, chapter_seq, part)",
            """
            CREATE TABLE beat_entities (
                beat_id   TEXT NOT NULL REFERENCES beats(id) ON DELETE CASCADE,
                position  INTEGER NOT NULL,
                name      TEXT NOT NULL,
                entity_id TEXT,
                PRIMARY KEY (beat_id, position)
            )
            """,
            "CREATE INDEX idx_beat_entities_entity ON beat_entities(entity_id)",
            "CREATE INDEX idx_beat_entities_name ON beat_entities(name)",
        ],
    ),
    # Worlds (2026-10-29): a world is the unit an entity may be shared
    # within. Backfill: one world per distinct series, one per series-less
    # book named after it (suffixed with the book id when the title is
    # already a world's name, or another series-less book has it too).
    # world_id stays nullable in the table (SQLite cannot add NOT NULL
    # without a default); ingest_book always sets it.
    (
        9,
        [
            """
            CREATE TABLE worlds (
                id         INTEGER PRIMARY KEY,
                name       TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            "ALTER TABLE books ADD COLUMN world_id INTEGER REFERENCES worlds(id)",
            "CREATE INDEX idx_books_world ON books(world_id)",
            # Series group by one collation: trimmed and case-folded, the
            # same as worlds.suggest_world. The world takes the spelling of
            # the series' first book.
            "INSERT INTO worlds (name) SELECT name FROM ("
            "SELECT trim(series) AS name, MIN(rowid) AS first_row FROM books"
            " WHERE series IS NOT NULL AND trim(series) <> ''"
            " GROUP BY lower(trim(series))) ORDER BY lower(name)",
            "UPDATE books SET world_id = (SELECT w.id FROM worlds w"
            " WHERE lower(w.name) = lower(trim(books.series)))"
            " WHERE series IS NOT NULL AND trim(series) <> ''",
            "INSERT INTO worlds (name) SELECT CASE WHEN EXISTS"
            " (SELECT 1 FROM worlds w WHERE lower(w.name) = lower(b.title))"
            " OR EXISTS (SELECT 1 FROM books b2 WHERE b2.world_id IS NULL"
            " AND lower(b2.title) = lower(b.title) AND b2.rowid < b.rowid)"
            " THEN b.title || ' (' || substr(b.id, 1, 8) || ')' ELSE b.title END"
            " FROM books b WHERE b.world_id IS NULL ORDER BY b.rowid",
            "UPDATE books SET world_id = COALESCE("
            "(SELECT w.id FROM worlds w WHERE w.name = books.title || ' (' || substr(books.id, 1, 8) || ')'),"
            " (SELECT w.id FROM worlds w WHERE w.name = books.title))"
            " WHERE world_id IS NULL",
        ],
    ),
    # Where an unfinished move is going: Continue (web or CLI) needs the
    # target world to rerun `remove`/`reassign`. Cleared when the job is done.
    (10, ["ALTER TABLE book_jobs ADD COLUMN move_world_id INTEGER"]),
    # One-time reconcile marker: a library graphed before worlds may have an
    # entity fused across books that migration 9 put in different worlds.
    # `worlds.ensure_reconciled` merges those worlds at the first graph open
    # and writes the single row.
    (
        11,
        [
            """
            CREATE TABLE world_reconcile (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                done_at TEXT NOT NULL DEFAULT (datetime('now')),
                merged  INTEGER NOT NULL DEFAULT 0
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
