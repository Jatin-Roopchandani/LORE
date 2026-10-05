import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.mcp import tools as tools_module
from lore.mcp.stores import StoresUnavailable
from lore.mcp.tools import entity, ingest_status, library, subgraph
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


# --- the graph tools -----------------------------------------------------------
#
#   h  Sherlock Holmes  --knows-->   w  Dr. Watson
#   w  Dr. Watson       --knows-->   m1 Miss Mary Morstan
#   m1 Miss Mary Morstan --knows-->  h                       (a cycle back to the root)
#   m1                  --employs--> j  Jabez Wilson          (2 hops from h)
#   j                   --lives_at-> far Far Away Place       (3 hops from h)
#   m2 Miss Morstan     --knows-->   w                        (2 hops; a twin of m1)
#   ah "Mr. Sherlock Holmes" is an alias stub: ALIAS_OF -> h, no REL edges
#   lonely Lonely Hall  has no edges at all
HOLMES = "Sherlock Holmes"
ALIAS_NAME = "Mr. Sherlock Holmes"
# m1 and m2 share the alias "Mary Morstan", so that one query name matches two
# distinct canonical entities -- the live graph's "Miss Mary Morstan" /
# "Miss Morstan" case, which no tool may resolve silently.
AMBIGUOUS_NAME = "Mary Morstan"


@pytest.fixture
def graph_config(tmp_path: Path) -> LoreConfig:
    def entity_row(i, name, aliases=()):
        return (i, name, "Character", [BOOK_ID], list(aliases))

    def edge(s, o, p, chunk, seq):
        return (s, o, p, chunk, BOOK_ID, seq)

    return build_store_triple(
        tmp_path,
        books=[(BOOK_ID, "A Study")],
        entities=[
            entity_row("h", HOLMES), entity_row("ah", ALIAS_NAME),
            entity_row("w", "Dr. Watson"),
            entity_row("m1", "Miss Mary Morstan", ["Mary Morstan"]),
            entity_row("m2", "Miss Morstan", ["Mary Morstan"]),
            entity_row("j", "Jabez Wilson"),
            entity_row("far", "Far Away Place"),
            entity_row("lonely", "Lonely Hall"),
        ],
        aliases=[("ah", "h")],
        edges=[
            edge("h", "w", "knows", "c2", 1),
            edge("w", "m1", "knows", "c1", 1),
            edge("m1", "h", "knows", "c3", 2),
            edge("m1", "j", "employs", "c4", 2),
            edge("j", "far", "lives_at", "c5", 3),
            edge("m2", "w", "knows", "c6", 3),
        ],
    )


def test_entity_returns_a_card(graph_config):
    result = entity(graph_config, HOLMES)
    assert result["id"] == "h" and result["name"] == HOLMES
    assert result["kind"] == "Character" and result["books"] == [BOOK_ID]
    assert {"aliases", "edges_out", "edges_in"} <= set(result)
    assert [e["object"] for e in result["edges_out"]] == ["Dr. Watson"]
    assert [e["subject"] for e in result["edges_in"]] == ["Miss Mary Morstan"]


def test_entity_card_edges_are_ordered_by_book_chapter_chunk(graph_config):
    result = entity(graph_config, "Dr. Watson")
    # in: m1 (ch 1, c1) before m2 (ch 3, c6)
    assert [e["chunk_id"] for e in result["edges_in"]] == ["c2", "c6"]


def test_entity_resolves_an_alias_to_its_canonical(graph_config):
    assert entity(graph_config, ALIAS_NAME)["name"] == HOLMES
    assert entity(graph_config, ALIAS_NAME) == entity(graph_config, HOLMES)


def test_entity_not_found(graph_config):
    result = entity(graph_config, "Nobody At All")
    assert result["error"]["code"] == "not_found"


def test_an_ambiguous_name_lists_the_candidates(graph_config):
    result = entity(graph_config, AMBIGUOUS_NAME)
    assert result["error"]["code"] == "not_found"
    message = result["error"]["message"]
    assert "ambiguous" in message.lower()
    # Both candidates, by name and id -- never one of them picked silently.
    assert "Miss Mary Morstan (m1)" in message
    assert "Miss Morstan (m2)" in message


def test_subgraph_refuses_an_ambiguous_name_too(graph_config):
    result = subgraph(graph_config, AMBIGUOUS_NAME)
    assert result["error"]["code"] == "not_found"
    assert "ambiguous" in result["error"]["message"].lower()
    assert "(m1)" in result["error"]["message"] and "(m2)" in result["error"]["message"]


def test_an_edgeless_entity_is_found_with_an_empty_card(graph_config):
    # It exists, so "no entity named" would be a lie.
    result = entity(graph_config, "Lonely Hall")
    assert result["id"] == "lonely"
    assert result["edges_out"] == [] and result["edges_in"] == []


def test_subgraph_of_an_edgeless_entity_is_just_the_root(graph_config):
    result = subgraph(graph_config, "Lonely Hall")
    assert [n["id"] for n in result["nodes"]] == ["lonely"]
    assert result["edges"] == [] and result["truncated"] is False


def test_subgraph_depth_one(graph_config):
    result = subgraph(graph_config, HOLMES, depth=1)
    assert [n["id"] for n in result["nodes"]] == ["h", "m1", "w"]
    ids = {n["id"] for n in result["nodes"]}
    # Edges join node ids, including the h<->m1 cycle edge and the w->m1 edge
    # between two non-root nodes.
    assert {(e["subject"], e["object"]) for e in result["edges"]} == {
        ("h", "w"), ("m1", "h"), ("w", "m1")}
    assert all(e["subject"] in ids and e["object"] in ids for e in result["edges"])
    assert result["depth"] == 1 and result["note"] is None
    assert result["truncated"] is False


def test_subgraph_walks_deeper_and_terminates_on_a_cycle(graph_config):
    two = subgraph(graph_config, HOLMES, depth=2)
    assert {n["id"] for n in two["nodes"]} == {"h", "m1", "w", "j", "m2"}
    three = subgraph(graph_config, HOLMES, depth=3)
    assert {n["id"] for n in three["nodes"]} == {
        "h", "m1", "w", "j", "m2", "far"}
    assert len(three["nodes"]) == len({n["id"] for n in three["nodes"]})


def test_subgraph_resolves_an_alias_to_its_canonical(graph_config):
    assert subgraph(graph_config, ALIAS_NAME) == subgraph(graph_config, HOLMES)


def test_subgraph_not_found(graph_config):
    assert subgraph(graph_config, "Nobody At All")["error"]["code"] == "not_found"


def test_depth_is_capped_and_the_cap_is_reported(graph_config):
    result = subgraph(graph_config, HOLMES, depth=99)
    assert result["depth"] == 3          # MAX_DEPTH, not 99
    assert "capped" in (result.get("note") or "").lower()
    assert "99" in result["note"]


def test_depth_below_one_is_clamped_to_one_and_says_so(graph_config):
    for bad in (0, -5):
        result = subgraph(graph_config, HOLMES, depth=bad)
        assert result["depth"] == 1
        assert "capped" in result["note"].lower()
        assert str(bad) in result["note"]


def test_subgraph_node_cap_truncates_deterministically(graph_config, monkeypatch):
    monkeypatch.setattr(tools_module, "SUBGRAPH_NODE_CAP", 3)
    result = subgraph(graph_config, HOLMES, depth=3)
    assert result["truncated"] is True
    # The root, then the lowest-id neighbours first -- not an arbitrary subset.
    assert [n["id"] for n in result["nodes"]] == ["h", "m1", "w"]
    ids = {n["id"] for n in result["nodes"]}
    assert all(e["subject"] in ids and e["object"] in ids for e in result["edges"])


def test_a_cap_that_fits_exactly_is_not_truncated(graph_config, monkeypatch):
    monkeypatch.setattr(tools_module, "SUBGRAPH_NODE_CAP", 3)
    assert subgraph(graph_config, HOLMES, depth=1)["truncated"] is False


# Phase 3 Task 13 made the query agent return the same evidence every run
# by adding a total order before every LIMIT. These tools cap results too,
# so they inherit the same hazard: an unordered query plus a cap returns a
# DIFFERENT subset run to run, and a UI would redraw a different graph each
# time with nothing to blame.
def test_the_same_entity_card_comes_back_every_time(graph_config):
    first = entity(graph_config, "Dr. Watson")
    assert all(entity(graph_config, "Dr. Watson") == first for _ in range(3))


def test_the_same_subgraph_comes_back_every_time(graph_config):
    first = subgraph(graph_config, HOLMES, depth=2)
    assert all(subgraph(graph_config, HOLMES, depth=2) == first
               for _ in range(3))


# The two tests above share one process, one hash seed: they cannot see a set
# iterated into the output. Fresh interpreters with different seeds can.
def test_output_does_not_depend_on_the_hash_seed(graph_config):
    code = (
        "import json, sys;"
        "from pathlib import Path;"
        "from lore.config import LoreConfig;"
        "from lore.mcp.tools import entity, subgraph;"
        "c = LoreConfig(data_dir=Path(sys.argv[1]));"
        "print(json.dumps([subgraph(c, 'Sherlock Holmes', depth=3),"
        " entity(c, 'Dr. Watson'), entity(c, 'Mary Morstan')]))"
    )
    outputs = set()
    for seed in ("0", "1", "2", "12345"):
        done = subprocess.run(
            [sys.executable, "-c", code, str(graph_config.data_dir)],
            capture_output=True, text=True, timeout=120, check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        outputs.add(done.stdout)
    assert len(outputs) == 1


def test_graph_tools_emit_strict_json(graph_config):
    for result in (entity(graph_config, HOLMES),
                   subgraph(graph_config, HOLMES, depth=3),
                   entity(graph_config, AMBIGUOUS_NAME)):
        json.dumps(result, allow_nan=False)


def test_graph_tools_without_a_graph_return_no_graph(empty_config):
    assert entity(empty_config, HOLMES)["error"]["code"] == "no_graph"
    assert subgraph(empty_config, HOLMES)["error"]["code"] == "no_graph"
