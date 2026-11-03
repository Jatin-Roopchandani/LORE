from pathlib import Path

from lore.db.sqlite import open_db, schema_version
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book, list_books

BOOK_ID = "abcdef0123456789"


def _chunks(n: int) -> list[Chunk]:
    return [
        Chunk(
            id=f"{BOOK_ID}:1:{i}",
            book_id=BOOK_ID,
            chapter_seq=1,
            scene_seq=0,
            seq=i,
            text=f"chunk text {i}",
        )
        for i in range(n)
    ]


def test_migration_v2_applies(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    assert schema_version(conn) == 12
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "chunks" in tables
    conn.close()


def test_ingest_book_inserts_and_is_idempotent(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    created = ingest_book(
        conn, book_id=BOOK_ID, title="T", author="A",
        source="upload", file_path="/x/b.epub", chunks=_chunks(3),
    )
    assert created is True
    again = ingest_book(
        conn, book_id=BOOK_ID, title="T", author="A",
        source="upload", file_path="/x/b.epub", chunks=_chunks(3),
    )
    assert again is False
    count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert count == 3
    conn.close()


def test_ingest_book_is_transactional(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    bad = _chunks(3)
    bad[2] = Chunk(id=bad[0].id, book_id=BOOK_ID, chapter_seq=1,
                   scene_seq=0, seq=2, text="dup id")  # PK collision
    try:
        ingest_book(
            conn, book_id=BOOK_ID, title="T", author=None,
            source="upload", file_path="/x/b.epub", chunks=bad,
        )
        raise AssertionError("expected IntegrityError")
    except Exception:
        pass
    # nothing committed: neither the book nor any chunk
    assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    conn.close()


def test_ingest_book_returns_false_for_any_existing_status(tmp_path: Path) -> None:
    # Any existing books row means "already ingested" — not just status ==
    # 'chunked'. Previously, any OTHER pre-existing status fell through to
    # INSERT OR REPLACE, which under FK enforcement deletes the parent row
    # and can explode on chunks that already reference it.
    conn = open_db(tmp_path / "lore.db")
    conn.execute(
        "INSERT INTO books (id, title, author, source, file_path, status)"
        " VALUES (?, ?, ?, ?, ?, 'extracted')",
        (BOOK_ID, "T", "A", "upload", "/x/b.epub"),
    )
    conn.commit()
    created = ingest_book(
        conn, book_id=BOOK_ID, title="T", author="A",
        source="upload", file_path="/x/b.epub", chunks=_chunks(3),
    )
    assert created is False
    count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert count == 0
    conn.close()


def test_list_books_reports_counts(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    ingest_book(
        conn, book_id=BOOK_ID, title="T", author="A",
        source="upload", file_path="/x/b.epub", chunks=_chunks(5),
    )
    books = list_books(conn)
    assert len(books) == 1
    assert books[0]["id"] == BOOK_ID
    assert books[0]["title"] == "T"
    assert books[0]["status"] == "chunked"
    assert books[0]["chunk_count"] == 5
    conn.close()


def test_chunks_for_book_returns_narrative_order(tmp_path: Path) -> None:
    from lore.ingest.registry import chunks_for_book

    conn = open_db(tmp_path / "lore.db")
    out_of_order = [
        Chunk(id=f"{BOOK_ID}:2:0", book_id=BOOK_ID, chapter_seq=2, scene_seq=0, seq=0, text="c"),
        Chunk(id=f"{BOOK_ID}:1:1", book_id=BOOK_ID, chapter_seq=1, scene_seq=0, seq=1, text="b"),
        Chunk(id=f"{BOOK_ID}:1:0", book_id=BOOK_ID, chapter_seq=1, scene_seq=0, seq=0, text="a"),
    ]
    ingest_book(
        conn, book_id=BOOK_ID, title="T", author=None,
        source="upload", file_path="/x/b.epub", chunks=out_of_order,
    )
    assert [c.text for c in chunks_for_book(conn, BOOK_ID)] == ["a", "b", "c"]
    conn.close()


def test_ingest_book_stores_series_and_list_books_reports_it(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    ingest_book(conn, book_id=BOOK_ID, title="T", author="A", source="upload",
                file_path="/x/b.epub", chunks=_chunks(1), series="Saga", series_index=2.0)
    (row,) = list_books(conn)
    assert (row["series"], row["series_index"]) == ("Saga", 2.0)
    conn.close()


def test_ingest_book_without_series_leaves_it_null(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    ingest_book(conn, book_id=BOOK_ID, title="T", author="A", source="upload",
                file_path="/x/b.epub", chunks=_chunks(1))
    (row,) = list_books(conn)
    assert (row["series"], row["series_index"]) == (None, None)
    conn.close()


def test_set_series_sets_clears_and_validates(tmp_path: Path) -> None:
    from lore.ingest.registry import set_series
    conn = open_db(tmp_path / "lore.db")
    ingest_book(conn, book_id=BOOK_ID, title="T", author="A", source="upload",
                file_path="/x/b.epub", chunks=_chunks(1))
    assert set_series(conn, BOOK_ID, "Saga", 3.0) is True
    (row,) = list_books(conn)
    assert (row["series"], row["series_index"]) == ("Saga", 3.0)
    assert set_series(conn, BOOK_ID, None, None) is True
    (row,) = list_books(conn)
    assert (row["series"], row["series_index"]) == (None, None)
    assert set_series(conn, "nope", "Saga", 1.0) is False
    conn.close()


def test_book_rows_survive_a_library_not_yet_migrated(tmp_path: Path) -> None:
    """The read-only query path opens the file as is: a v4 library still answers."""
    import sqlite3
    from lore.ingest.registry import book_rows
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE chunks (id TEXT PRIMARY KEY, book_id TEXT)")
    old.execute("CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT,"
                " source TEXT, file_path TEXT, status TEXT, created_at TEXT)")
    old.execute("INSERT INTO books (id, title) VALUES ('b1', 'Old')")
    old.commit()
    rows = book_rows(old)
    assert rows["b1"].title == "Old" and rows["b1"].series is None
    (listed,) = list_books(old)
    assert listed["title"] == "Old" and listed["series"] is None and listed["chunk_count"] == 0
    old.close()
