import json
import sqlite3
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.db.sqlite import open_db
from lore.mcp import tools as tools_module
from lore.mcp.stores import StoresUnavailable
from lore.mcp.tools import ingest_status, library

BOOK_ID = "book-1"
EMPTY_BOOK_ID = "book-2"


@pytest.fixture
def live_config(tmp_path: Path) -> LoreConfig:
    """A real store triple holding two books: one with entities and jobs
    (one of them failed), one with neither. Built the way the CLI builds
    them, as in test_mcp_stores.py, but with books and jobs added because
    these tools read them."""
    config = LoreConfig(data_dir=tmp_path / "data")
    graph = KuzuStore(config.kuzu_dir / "graph")
    for entity_id, name, books in (
        ("e1", "Holmes", [BOOK_ID]),
        ("e2", "Watson", [BOOK_ID]),
    ):
        graph.execute(
            "CREATE (:Entity {id: $id, name: $name, kind: 'Character',"
            " books: $books})",
            {"id": entity_id, "name": name, "books": books},
        )
    graph.close()
    LanceStore(config.lancedb_dir)
    conn = open_db(config.sqlite_path)
    for book_id, title in ((BOOK_ID, "A Study"), (EMPTY_BOOK_ID, "Blank")):
        conn.execute(
            "INSERT INTO books (id, title, file_path) VALUES (?, ?, 'x.txt')",
            (book_id, title),
        )
    conn.execute(
        "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
        " VALUES ('c1', ?, 1, 1, 1, 'text')", (BOOK_ID,),
    )
    for kind, state, error, ts in (
        ("extract", "completed", None, "2026-01-01 00:00:01"),
        ("extract", "failed", "boom: schema mismatch", "2026-01-01 00:00:02"),
    ):
        conn.execute(
            "INSERT INTO jobs (kind, book_id, state, error, updated_at)"
            " VALUES (?, ?, ?, ?, ?)", (kind, BOOK_ID, state, error, ts),
        )
    conn.commit()
    conn.close()
    return config


@pytest.fixture
def empty_config(tmp_path: Path) -> LoreConfig:
    return LoreConfig(data_dir=tmp_path / "nothing")


@pytest.fixture
def a_book_id(live_config) -> str:
    conn = sqlite3.connect(live_config.sqlite_path)
    try:
        return conn.execute("SELECT book_id FROM jobs").fetchone()[0]
    finally:
        conn.close()


def test_library_lists_books_with_counts(live_config):
    result = library(live_config)
    assert result["books"]
    book = result["books"][0]
    assert set(book) >= {"id", "title", "author", "status",
                         "chunk_count", "entity_count"}


def test_library_counts_entities_and_keeps_zero_entity_books(live_config):
    by_id = {b["id"]: b for b in library(live_config)["books"]}
    assert by_id[BOOK_ID]["entity_count"] == 2
    assert by_id[EMPTY_BOOK_ID]["entity_count"] == 0


def test_library_without_a_graph_returns_no_graph(empty_config):
    assert library(empty_config)["error"]["code"] == "no_graph"


def test_ingest_status_reports_jobs(live_config, a_book_id):
    result = ingest_status(live_config, a_book_id)
    assert result["book_id"] == a_book_id
    assert "jobs" in result
    assert "dead_letters" in result


def test_failed_jobs_are_dead_letters_with_their_error(live_config, a_book_id):
    result = ingest_status(live_config, a_book_id)
    assert len(result["jobs"]) == 2
    assert [j["state"] for j in result["dead_letters"]] == ["failed"]
    assert result["dead_letters"][0]["error"] == "boom: schema mismatch"


def test_ingest_status_for_an_unknown_book(live_config):
    result = ingest_status(live_config, "no-such-book")
    assert result["error"]["code"] == "not_found"
    assert "no-such-book" in result["error"]["message"]


def test_ingest_status_without_a_graph_returns_no_graph(empty_config):
    assert ingest_status(empty_config, "x")["error"]["code"] == "no_graph"


def test_stores_unavailable_is_no_graph_with_the_honest_message(
    live_config, monkeypatch
):
    def boom(config):
        raise StoresUnavailable("the graph exists but could not be opened")
    monkeypatch.setattr(tools_module, "open_stores", boom)
    result = library(live_config)
    assert result["error"]["code"] == "no_graph"
    assert "could not be opened" in result["error"]["message"]
    assert "lore ingest" not in result["error"]["message"]


def test_both_tools_emit_strict_json(live_config, a_book_id):
    json.dumps(library(live_config), allow_nan=False)
    json.dumps(ingest_status(live_config, a_book_id), allow_nan=False)
