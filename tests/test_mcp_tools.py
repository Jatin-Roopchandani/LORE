import json
import sqlite3
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.mcp import tools as tools_module
from lore.mcp.stores import StoresUnavailable
from lore.mcp.tools import ingest_status, library
from tests.mcp_fixtures import build_store_triple
from tests.mcp_fixtures import empty_config as _empty_config

BOOK_ID = "book-1"
EMPTY_BOOK_ID = "book-2"


@pytest.fixture
def library_config(tmp_path: Path) -> LoreConfig:
    """Two books: one with entities and jobs (one of them failed), one with
    neither. Named for what it holds, not `library_config`, which
    test_mcp_stores.py once used for a different store."""
    return build_store_triple(
        tmp_path,
        books=[(BOOK_ID, "A Study"), (EMPTY_BOOK_ID, "Blank")],
        chunks=[("c1", BOOK_ID, 1)],
        entities=[
            ("e1", "Holmes", "Character", [BOOK_ID], []),
            ("e2", "Watson", "Character", [BOOK_ID], []),
        ],
        jobs=[
            ("extract", BOOK_ID, "completed", None, "2026-01-01 00:00:01"),
            ("extract", BOOK_ID, "failed", "boom: schema mismatch",
             "2026-01-01 00:00:02"),
        ],
    )


@pytest.fixture
def empty_config(tmp_path: Path) -> LoreConfig:
    return _empty_config(tmp_path)


@pytest.fixture
def a_book_id(library_config) -> str:
    conn = sqlite3.connect(library_config.sqlite_path)
    try:
        return conn.execute("SELECT book_id FROM jobs").fetchone()[0]
    finally:
        conn.close()


def test_library_lists_books_with_counts(library_config):
    result = library(library_config)
    assert result["books"]
    book = result["books"][0]
    assert set(book) >= {"id", "title", "author", "status",
                         "chunk_count", "entity_count"}


def test_library_counts_entities_and_keeps_zero_entity_books(library_config):
    by_id = {b["id"]: b for b in library(library_config)["books"]}
    assert by_id[BOOK_ID]["entity_count"] == 2
    assert by_id[EMPTY_BOOK_ID]["entity_count"] == 0


def test_library_without_a_graph_returns_no_graph(empty_config):
    assert library(empty_config)["error"]["code"] == "no_graph"


def test_ingest_status_reports_jobs(library_config, a_book_id):
    result = ingest_status(library_config, a_book_id)
    assert result["book_id"] == a_book_id
    assert "jobs" in result
    assert "dead_letters" in result


def test_failed_jobs_are_dead_letters_with_their_error(library_config, a_book_id):
    result = ingest_status(library_config, a_book_id)
    assert len(result["jobs"]) == 2
    assert [j["state"] for j in result["dead_letters"]] == ["failed"]
    assert result["dead_letters"][0]["error"] == "boom: schema mismatch"


def test_ingest_status_for_an_unknown_book(library_config):
    result = ingest_status(library_config, "no-such-book")
    assert result["error"]["code"] == "not_found"
    assert "no-such-book" in result["error"]["message"]


def test_ingest_status_without_a_graph_returns_no_graph(empty_config):
    assert ingest_status(empty_config, "x")["error"]["code"] == "no_graph"


def test_stores_unavailable_is_no_graph_with_the_honest_message(
    library_config, monkeypatch
):
    def boom(config):
        raise StoresUnavailable("the graph exists but could not be opened")
    monkeypatch.setattr(tools_module, "open_stores", boom)
    result = library(library_config)
    assert result["error"]["code"] == "no_graph"
    assert "could not be opened" in result["error"]["message"]
    assert "lore ingest" not in result["error"]["message"]


def test_both_tools_emit_strict_json(library_config, a_book_id):
    json.dumps(library(library_config), allow_nan=False)
    json.dumps(ingest_status(library_config, a_book_id), allow_nan=False)
