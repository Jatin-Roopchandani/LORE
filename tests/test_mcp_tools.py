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
from lore.db.lance_store import EMBED_DIM, LanceStore
from lore.mcp.tools import (
    cypher_query, entity, ingest_status, library, overview, subgraph,
    vector_search,
)
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


def test_ingest_status_without_a_library_returns_no_graph(empty_config):
    assert ingest_status(empty_config, "x")["error"]["code"] == "no_graph"


def test_stores_unavailable_is_no_graph_with_the_honest_message(
    library_config, monkeypatch
):
    def boom(config, **scope):
        raise StoresUnavailable("the graph exists but could not be opened")
    monkeypatch.setattr(tools_module, "open_stores", boom)
    result = entity(library_config, "Holmes")
    assert result["error"]["code"] == "no_graph"
    assert "could not be opened" in result["error"]["message"]
    assert "lore ingest" not in result["error"]["message"]


# --- tools that do not need the graph ---------------------------------------------
#
# ingest_status exists to watch the pipeline, so it must work exactly while the
# graph is absent (between ingest and `lore graph load`) or locked (during one).

def _drop_graph(config: LoreConfig) -> None:
    import shutil
    path = config.kuzu_dir / "graph"
    path.unlink() if path.is_file() else shutil.rmtree(path)


def test_ingest_status_works_without_a_graph(library_config, a_book_id):
    _drop_graph(library_config)
    result = ingest_status(library_config, a_book_id)
    assert "error" not in result, result
    assert len(result["jobs"]) == 2


def test_library_without_a_graph_lists_books_with_null_entity_counts(library_config):
    _drop_graph(library_config)
    result = library(library_config)
    assert "error" not in result, result
    assert {b["id"] for b in result["books"]} == {BOOK_ID, EMPTY_BOOK_ID}
    assert all(b["entity_count"] is None for b in result["books"])
    assert "graph" in result["note"] and "unavailable" in result["note"]
    json.dumps(result, allow_nan=False)


def test_library_with_a_graph_has_full_counts_and_no_note(library_config):
    result = library(library_config)
    assert result["note"] is None
    assert {b["id"]: b["entity_count"] for b in result["books"]} == {
        BOOK_ID: 2, EMPTY_BOOK_ID: 0}


def _hold_the_write_lock(config: LoreConfig):
    """A separate process holding the graph's write lock, as `lore graph
    load` does; returns the Popen once the lock is really held."""
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; from pathlib import Path;"
         "from lore.db.kuzu_store import KuzuStore;"
         "s = KuzuStore(Path(sys.argv[1])); print('ready', flush=True);"
         "sys.stdin.readline(); s.close()", str(config.kuzu_dir / "graph")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert holder.stdout.readline().strip() == "ready"
    return holder


def _release(holder) -> None:
    holder.stdin.write("\n")
    holder.stdin.flush()
    holder.wait(timeout=60)


async def test_the_graph_free_tools_work_during_a_rebuild(library_config, a_book_id):
    LanceStore(library_config.lancedb_dir).add_chunks([
        {"vector": _vec(0.0), "chunk_id": "c1", "book_id": BOOK_ID,
         "chapter_seq": 1, "text": "alpha", "entity_ids": []}])
    holder = _hold_the_write_lock(library_config)
    try:
        status = ingest_status(library_config, a_book_id)
        books = library(library_config)
        hits = await vector_search(library_config, "q", embed=_fake_embed)
        card = entity(library_config, "Holmes")
    finally:
        _release(holder)
    assert "error" not in status, status
    assert "error" not in hits, hits
    assert [h["chunk_id"] for h in hits["chunks"]] == ["c1"]
    assert all(b["entity_count"] is None for b in books["books"])
    assert "rebuild" in books["note"]
    # The graph tools still need the graph, and say so.
    assert card["error"]["code"] == "no_graph"


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
#   far                 --self-->    far                       (a self-loop)
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
            edge("far", "far", "self", "c7", 3),
        ],
    )


def test_entity_returns_a_card(graph_config):
    result = entity(graph_config, HOLMES)
    assert result["id"] == "h" and result["name"] == HOLMES
    assert result["kind"] == "Character" and result["books"] == [BOOK_ID]
    assert {"aliases", "edges_out", "edges_in"} <= set(result)
    assert [e["object"] for e in result["edges_out"]] == ["Dr. Watson"]
    assert [(e["subject_id"], e["object_id"]) for e in result["edges_out"]] == [
        ("h", "w")]
    assert [e["subject"] for e in result["edges_in"]] == ["Miss Mary Morstan"]


def test_entity_card_edges_are_ordered_by_book_chapter_chunk(graph_config):
    result = entity(graph_config, "Dr. Watson")
    # in: m1 (ch 1, c1) before m2 (ch 3, c6)
    assert [e["chunk_id"] for e in result["edges_in"]] == ["c2", "c6"]


# The card used to return every edge. On the live graph Mr. Sherlock Holmes
# has 172 out and 111 in -- a 103 KB result that Claude Code refused as one
# tool response. Each direction is capped, the totals say how many exist,
# and `truncated` says the card is partial.

def test_the_card_reports_edge_totals_when_nothing_is_cut(graph_config):
    result = entity(graph_config, "Dr. Watson")
    assert result["edges_in_total"] == 2 and len(result["edges_in"]) == 2
    assert result["edges_out_total"] == len(result["edges_out"])
    assert result["truncated"] is False


def test_the_card_caps_each_direction_and_keeps_the_first_edges(
        graph_config, monkeypatch):
    monkeypatch.setattr(tools_module, "CARD_EDGE_CAP", 1)
    result = entity(graph_config, "Dr. Watson")
    # The same total order as the uncapped card: c2 (ch 1) survives, c6 goes.
    assert [e["chunk_id"] for e in result["edges_in"]] == ["c2"]
    assert result["edges_in_total"] == 2
    assert result["truncated"] is True


def test_exactly_cap_card_edges_is_not_truncated(graph_config, monkeypatch):
    monkeypatch.setattr(tools_module, "CARD_EDGE_CAP", 2)
    result = entity(graph_config, "Dr. Watson")
    assert len(result["edges_in"]) == 2 and result["truncated"] is False


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
    assert {(e["subject_id"], e["object_id"]) for e in result["edges"]} == {
        ("h", "w"), ("m1", "h"), ("w", "m1")}
    assert all(e["subject_id"] in ids and e["object_id"] in ids
               for e in result["edges"])
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
    assert all(e["subject_id"] in ids and e["object_id"] in ids
               for e in result["edges"])


def test_edges_name_their_ends_and_ids_join_to_nodes(graph_config):
    result = subgraph(graph_config, HOLMES, depth=3)
    names = {n["id"]: n["name"] for n in result["nodes"]}
    assert result["edges"]
    for e in result["edges"]:
        assert e["subject_id"] in names and e["object_id"] in names
        assert e["subject"] == names[e["subject_id"]]
        assert e["object"] == names[e["object_id"]]


# Parallel edges. On the live graph Jefferson Hope's depth-1 subgraph had 31
# nodes but 205 edges -- the same pair repeated once per passage -- an 82 KB
# result Claude Code refused. The subgraph returns one link per
# subject->object pair: how many passages back it, which predicates, and one
# representative citation; links are ordered strongest first, so a capped
# hub keeps its real relationships and drops the one-off mentions.

@pytest.fixture
def parallel_config(tmp_path: Path) -> LoreConfig:
    def entity_row(i, name):
        return (i, name, "Character", [BOOK_ID], [])

    def edge(s, o, p, chunk, seq):
        return (s, o, p, chunk, BOOK_ID, seq)

    return build_store_triple(
        tmp_path,
        books=[(BOOK_ID, "A Study")],
        entities=[entity_row("h", HOLMES), entity_row("w", "Dr. Watson"),
                  entity_row("j", "Jabez Wilson")],
        edges=[
            edge("h", "w", "knows", "c8", 4),
            edge("h", "w", "knows", "c2", 1),
            edge("h", "w", "admires", "c9", 4),
            edge("w", "h", "knows", "c3", 2),
            edge("h", "j", "knows", "c4", 2),
        ],
    )


def test_parallel_edges_become_one_link_per_direction(parallel_config):
    result = subgraph(parallel_config, HOLMES, depth=1)
    by_pair = {(e["subject_id"], e["object_id"]): e for e in result["edges"]}
    assert set(by_pair) == {("h", "w"), ("w", "h"), ("h", "j")}
    hw = by_pair[("h", "w")]
    assert hw["count"] == 3
    assert hw["predicates"] == {"knows": 2, "admires": 1}
    # One citation, the first edge in book/chapter/chunk order: c2 (ch 1).
    assert [c["chunk_id"] for c in hw["citations"]] == ["c2"]
    assert hw["citations"][0]["predicate"] == "knows"
    assert by_pair[("w", "h")]["count"] == 1


def test_links_are_ordered_strongest_first(parallel_config):
    result = subgraph(parallel_config, HOLMES, depth=1)
    # h->w (3) first; the count-1 links tie and fall back to (subject, object).
    assert [(e["subject_id"], e["object_id"]) for e in result["edges"]] == [
        ("h", "w"), ("h", "j"), ("w", "h")]


def test_a_capped_subgraph_keeps_the_strongest_links(parallel_config, monkeypatch):
    monkeypatch.setattr(tools_module, "SUBGRAPH_EDGE_CAP", 1)
    result = subgraph(parallel_config, HOLMES, depth=1)
    assert [(e["subject_id"], e["object_id"]) for e in result["edges"]] == [("h", "w")]
    assert result["truncated"] is True


def test_a_capped_subgraph_drops_nodes_only_its_cut_links_reached(
        parallel_config, monkeypatch):
    # Live graph: Holmes at depth 3 kept 100 links but still listed 286 nodes
    # (75 KB) -- most reachable only through links the cap had dropped.
    monkeypatch.setattr(tools_module, "SUBGRAPH_EDGE_CAP", 1)
    result = subgraph(parallel_config, HOLMES, depth=1)
    assert [n["id"] for n in result["nodes"]] == ["h", "w"]   # j's link was cut


def test_the_root_stays_even_with_no_links(graph_config):
    result = subgraph(graph_config, "Lonely Hall", depth=2)
    assert [n["id"] for n in result["nodes"]] == ["lonely"]


def test_grouped_subgraph_is_plain_json(parallel_config):
    json.dumps(subgraph(parallel_config, HOLMES, depth=1), allow_nan=False)


def test_a_self_loop_terminates_and_appears_once(graph_config):
    result = subgraph(graph_config, HOLMES, depth=3)
    ids = [n["id"] for n in result["nodes"]]
    assert ids.count("far") == 1
    loops = [e for e in result["edges"] if e["subject_id"] == e["object_id"]]
    assert [(e["subject_id"], e["predicates"]) for e in loops] == [
        ("far", {"self": 1})]
    # And a walk rooted ON the loop node terminates too.
    rooted = subgraph(graph_config, "Far Away Place", depth=3)
    assert [n["id"] for n in rooted["nodes"]].count("far") == 1


def test_the_edge_cap_truncates_and_keeps_the_first_edges(graph_config, monkeypatch):
    monkeypatch.setattr(tools_module, "SUBGRAPH_EDGE_CAP", 2)
    result = subgraph(graph_config, HOLMES, depth=1)
    assert result["truncated"] is True
    # Total order (s.id, o.id, ...): the survivors are the first two.
    assert [(e["subject_id"], e["object_id"]) for e in result["edges"]] == [
        ("h", "w"), ("m1", "h")]
    ids = {n["id"] for n in result["nodes"]}
    assert all(e["subject_id"] in ids and e["object_id"] in ids
               for e in result["edges"])


def test_exactly_cap_edges_is_not_truncated(graph_config, monkeypatch):
    monkeypatch.setattr(tools_module, "SUBGRAPH_EDGE_CAP", 3)
    result = subgraph(graph_config, HOLMES, depth=1)
    assert len(result["edges"]) == 3 and result["truncated"] is False


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
        "from lore.mcp import tools;"
        "from lore.mcp.tools import entity, subgraph;"
        "tools.SUBGRAPH_NODE_CAP = int(sys.argv[2]);"
        "tools.SUBGRAPH_EDGE_CAP = int(sys.argv[3]);"
        "c = LoreConfig(data_dir=Path(sys.argv[1]));"
        "print(json.dumps([subgraph(c, 'Sherlock Holmes', depth=3),"
        " entity(c, 'Dr. Watson'), entity(c, 'Mary Morstan')]))"
    )
    # Uncapped, and with caps small enough to fire: truncation is where a
    # set-order bug would change which nodes and edges survive.
    for node_cap, edge_cap in ((300, 500), (4, 500), (300, 3), (4, 3)):
        outputs = set()
        for seed in ("0", "1", "2", "12345"):
            done = subprocess.run(
                [sys.executable, "-c", code, str(graph_config.data_dir),
                 str(node_cap), str(edge_cap)],
                capture_output=True, text=True, timeout=120, check=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
            )
            outputs.add(done.stdout)
        assert len(outputs) == 1, (node_cap, edge_cap)
        if (node_cap, edge_cap) != (300, 500):
            assert '"truncated": true' in done.stdout


def test_graph_tools_emit_strict_json(graph_config):
    for result in (entity(graph_config, HOLMES),
                   subgraph(graph_config, HOLMES, depth=3),
                   entity(graph_config, AMBIGUOUS_NAME)):
        json.dumps(result, allow_nan=False)


def test_graph_tools_without_a_graph_return_no_graph(empty_config):
    assert entity(empty_config, HOLMES)["error"]["code"] == "no_graph"
    assert subgraph(empty_config, HOLMES)["error"]["code"] == "no_graph"


# --- vector_search and cypher_query --------------------------------------------

BOOK_A, BOOK_B = "book-a", "book-b"


def _vec(x: float) -> list[float]:
    v = [0.0] * EMBED_DIM
    v[0] = x
    return v


async def _fake_embed(texts):
    return [_vec(0.0) for _ in texts]


@pytest.fixture
def search_config(tmp_path: Path) -> LoreConfig:
    """Five chunks of book A sit nearest the query; book B's are farther.
    A post-filter of the global top 3 would give book B nothing."""
    config = build_store_triple(
        tmp_path,
        books=[(BOOK_A, "Alpha"), (BOOK_B, "Beta")],
        entities=[("e1", "Holmes", "Character", [BOOK_A], []),
                  ("e2", "Watson", "Character", [BOOK_A], [])],
    )
    rows = [
        {"vector": _vec(0.1 * i), "chunk_id": f"a{i}", "book_id": BOOK_A,
         "chapter_seq": i, "text": f"alpha {i}", "entity_ids": []}
        for i in range(5)
    ] + [
        {"vector": _vec(10.0 + i), "chunk_id": f"b{i}", "book_id": BOOK_B,
         "chapter_seq": i, "text": f"beta {i}", "entity_ids": []}
        for i in range(3)
    ]
    LanceStore(config.lancedb_dir).add_chunks(rows)
    return config


async def test_vector_search_returns_chunks(search_config):
    result = await vector_search(search_config, "a murder", limit=3,
                                 embed=_fake_embed)
    assert [h["chunk_id"] for h in result["chunks"]] == ["a0", "a1", "a2"]
    hit = result["chunks"][0]
    assert set(hit) == {"chunk_id", "book_id", "book_title", "chapter_seq",
                        "text", "distance"}
    assert hit["book_title"] == "Alpha"
    assert hit["text"] == "alpha 0"


async def test_vector_search_filters_inside_the_search(search_config):
    # Book B is not in the global top 3, so only a prefilter finds it.
    result = await vector_search(search_config, "murder", book_id=BOOK_B,
                                 limit=3, embed=_fake_embed)
    assert [h["chunk_id"] for h in result["chunks"]] == ["b0", "b1", "b2"]
    assert all(h["book_id"] == BOOK_B for h in result["chunks"])


async def test_vector_search_with_an_unknown_book_id(search_config):
    result = await vector_search(search_config, "murder", book_id="nope",
                                 embed=_fake_embed)
    assert result["error"]["code"] == "not_found"


async def test_vector_search_applies_the_query_prefix_once(search_config):
    seen = []

    async def spy(texts):
        seen.extend(texts)
        return [_vec(0.0) for _ in texts]

    await vector_search(search_config, "hello", embed=spy)
    from lore.query.seed import QUERY_PREFIX
    assert seen == [QUERY_PREFIX + "hello"]


@pytest.mark.parametrize("limit,expected", [(0, 1), (-5, 1), (1000, 8), (2, 2)])
async def test_vector_search_clamps_limit(search_config, limit, expected):
    result = await vector_search(search_config, "q", limit=limit,
                                 embed=_fake_embed)
    assert len(result["chunks"]) == expected


async def test_vector_search_limit_ceiling_is_documented_and_applied(
    search_config, monkeypatch
):
    from lore.mcp import tools
    asked = []
    real = LanceStore.search_chunks

    def spy(self, vector, limit=5):
        asked.append(limit)
        return real(self, vector, limit)

    monkeypatch.setattr(LanceStore, "search_chunks", spy)
    result = await vector_search(search_config, "q", limit=10**6, embed=_fake_embed)
    # Read a little past the ceiling (boilerplate is filtered out), return no more.
    assert asked and asked[0] >= tools.VECTOR_LIMIT_MAX
    assert len(result["chunks"]) <= tools.VECTOR_LIMIT_MAX


async def test_vector_search_distance_is_strict_json(search_config, monkeypatch):
    for bad in (float("inf"), float("nan"), None):
        def fake(self, vector, limit=5, bad=bad):
            row = {"chunk_id": "a0", "book_id": BOOK_A, "chapter_seq": 1,
                   "text": "t", "vector": vector, "entity_ids": []}
            if bad is not None:
                row["_distance"] = bad
            return [row]

        monkeypatch.setattr(LanceStore, "search_chunks", fake)
        result = await vector_search(search_config, "q", embed=_fake_embed)
        assert result["chunks"][0]["distance"] is None
        json.dumps(result, allow_nan=False)
    real = await _real_distance(search_config, monkeypatch)
    assert isinstance(real, float)


async def _real_distance(config, monkeypatch):
    monkeypatch.undo()
    result = await vector_search(config, "q", embed=_fake_embed)
    json.dumps(result, allow_nan=False)
    return result["chunks"][0]["distance"]


async def test_vector_search_without_a_library(empty_config):
    result = await vector_search(empty_config, "q", embed=_fake_embed)
    assert result["error"]["code"] == "no_graph"


async def test_vector_search_works_with_the_graph_missing(search_config):
    _drop_graph(search_config)
    result = await vector_search(search_config, "q", limit=2, embed=_fake_embed)
    assert [h["chunk_id"] for h in result["chunks"]] == ["a0", "a1"]


async def test_vector_search_holds_no_store_while_it_embeds(search_config, monkeypatch):
    # The embedder can take seconds (or hang on a stopped Ollama); no store
    # handle -- above all no Kuzu lock -- may be held across it.
    from contextlib import contextmanager
    real = tools_module.open_stores
    opened: list[dict] = []
    active = []

    @contextmanager
    def spy(config, **scope):
        opened.append(scope)
        active.append(1)
        try:
            with real(config, **scope) as stores:
                yield stores
        finally:
            active.pop()

    async def embed(texts):
        assert not active, "a store is open while embedding"
        return [_vec(0.0) for _ in texts]

    monkeypatch.setattr(tools_module, "open_stores", spy)
    result = await vector_search(search_config, "q", book_id=BOOK_A, embed=embed)
    assert result["chunks"]
    assert opened and all(scope.get("kuzu") is False for scope in opened)


# A book chunked by `lore ingest` but never `lore graph load`ed has no Lance
# rows: the live "Pride and Prejudice" (731 chunks, 0 vectors). An empty list
# there would read as "nothing matched" when nothing was ever searchable.
UNEMBEDDED = "book-pp"


async def test_vector_search_in_an_unembedded_book_says_so(tmp_path):
    config = build_store_triple(
        tmp_path, books=[(BOOK_A, "Alpha"), (UNEMBEDDED, "Pride and Prejudice")],
        chunks=[("p1", UNEMBEDDED, 1)],
    )
    LanceStore(config.lancedb_dir).add_chunks([
        {"vector": _vec(0.0), "chunk_id": "a0", "book_id": BOOK_A,
         "chapter_seq": 1, "text": "alpha", "entity_ids": []}])
    result = await vector_search(config, "q", book_id=UNEMBEDDED,
                                 embed=_fake_embed)
    assert result["error"]["code"] == "not_found"
    assert "Pride and Prejudice" in result["error"]["message"]
    assert "lore graph load" in result["error"]["message"]
    # A book that does have rows still returns hits.
    hits = await vector_search(config, "q", book_id=BOOK_A, embed=_fake_embed)
    assert [h["chunk_id"] for h in hits["chunks"]] == ["a0"]


async def test_an_embedded_book_with_no_match_is_still_an_empty_list(
    search_config, monkeypatch
):
    monkeypatch.setattr(LanceStore, "search_chunks_in_book",
                        lambda self, vector, book_id, limit=5: [])
    result = await vector_search(search_config, "q", book_id=BOOK_A,
                                 embed=_fake_embed)
    assert result == {"chunks": []}


def test_cypher_query_returns_rows(library_config):
    result = cypher_query(library_config,
                          "MATCH (e:Entity) RETURN e.name ORDER BY e.name LIMIT 3")
    assert result["columns"] == ["e.name"]
    assert result["rows"] == [["Holmes"], ["Watson"]]
    assert result["truncated"] is False


def test_cypher_query_rejects_a_write(library_config):
    result = cypher_query(library_config, "CREATE (n:Entity {id: 'x'})")
    assert result["error"]["code"] == "cypher_rejected"
    assert "CREATE" in result["error"]["message"]


def test_cypher_query_rejects_copy_to_a_file(library_config, tmp_path):
    target = tmp_path / "leak.csv"
    result = cypher_query(
        library_config, f"COPY (MATCH (e:Entity) RETURN e.id) TO '{target}'"
    )
    assert result["error"]["code"] == "cypher_rejected"
    assert not target.exists(), "the guard must run BEFORE Kuzu sees the query"


def test_cypher_query_rejects_load_from_a_file(library_config):
    result = cypher_query(
        library_config, "LOAD FROM '/etc/hostname' (file_format='csv') RETURN *"
    )
    assert result["error"]["code"] == "cypher_rejected"


def test_cypher_query_caps_rows(tmp_path):
    config = build_store_triple(
        tmp_path,
        books=[(BOOK_A, "Alpha")],
        entities=[(f"e{i}", f"n{i}", "Character", [BOOK_A], [])
                  for i in range(20)],
    )
    result = cypher_query(config, "MATCH (a:Entity), (b:Entity) RETURN a.id, b.id")
    assert len(result["rows"]) == 200
    assert result["truncated"] is True


def test_cypher_query_returns_plain_json_for_node_values(library_config):
    result = cypher_query(library_config,
                          "MATCH (e:Entity) RETURN e ORDER BY e.id LIMIT 1")
    json.dumps(result, allow_nan=False)
    node = result["rows"][0][0]
    assert isinstance(node, dict)
    assert node["id"] == "e1" and node["name"] == "Holmes"


def test_cypher_query_returns_plain_json_for_relationships(graph_config):
    result = cypher_query(graph_config,
                          "MATCH (:Entity)-[r:REL]->(:Entity) RETURN r LIMIT 1")
    json.dumps(result, allow_nan=False)
    assert isinstance(result["rows"][0][0], dict)


def test_cypher_query_returns_dates_as_iso_strings(library_config):
    result = cypher_query(
        library_config,
        "RETURN date('2026-01-01'), timestamp('2026-01-02 03:04:05'),"
        " interval('1 day'), 1.5, [1, 2]",
    )
    json.dumps(result, allow_nan=False)
    d, ts, interval, f, lst = result["rows"][0]
    assert d == "2026-01-01"
    assert ts == "2026-01-02T03:04:05"
    assert isinstance(interval, str)
    assert f == 1.5 and lst == [1, 2]


def test_cypher_query_nonfinite_floats_become_null(library_config):
    result = cypher_query(library_config, "RETURN 1.0 / 0.0, 0.0 / 0.0, 2.5")
    json.dumps(result, allow_nan=False)
    assert result["rows"][0][2] == 2.5


def test_sanitize_recurses_and_stringifies():
    import datetime
    from lore.mcp.tools import _plain
    out = _plain({"a": [float("inf"), {"b": float("nan")}, b"x"],
                  "d": datetime.date(2026, 1, 1),
                  "t": datetime.datetime(2026, 1, 1, 2, 3, 4),
                  "n": None, "ok": True, "i": 3})
    assert out == {"a": [None, {"b": None}, "b'x'"], "d": "2026-01-01",
                   "t": "2026-01-01T02:03:04", "n": None, "ok": True, "i": 3}
    json.dumps(out, allow_nan=False)


def test_cypher_query_timeout_is_mapped_from_a_real_kuzu_timeout(
    tmp_path, monkeypatch
):
    """A real Kuzu timeout, not a mock: an ORDER BY over a cross product must
    materialise and sort before its first row, so a 1 ms limit always fires.
    Kuzu raises RuntimeError('Interrupted.')."""
    from lore.mcp import stores
    config = build_store_triple(
        tmp_path,
        books=[(BOOK_A, "Alpha")],
        entities=[(f"e{i:03d}", f"n{i}", "Character", [BOOK_A], [])
                  for i in range(60)],
    )
    monkeypatch.setattr(stores, "TIMEOUT_MS", 1)
    result = cypher_query(
        config,
        "MATCH (a:Entity),(b:Entity),(c:Entity) RETURN a.id, b.id, c.id"
        " ORDER BY a.id, b.id, c.id",
    )
    assert result["error"]["code"] == "timeout"


def test_a_cypher_typo_is_rejected_with_kuzus_own_message(library_config):
    # Not re-raised: mcp 2.3.0 turns an escaped exception into the bare text
    # "Error executing tool cypher_query" and drops Kuzu's message, so an LLM
    # that typoed its Cypher could not see what to fix.
    result = cypher_query(library_config, "MATCH (e:Entity) RETRN e")
    assert result["error"]["code"] == "cypher_rejected"
    assert "Parser exception" in result["error"]["message"]
    assert "RETRN" in result["error"]["message"]


def test_a_wrong_property_is_rejected_with_the_binder_message(library_config):
    result = cypher_query(library_config, "MATCH (e:Entity) RETURN e.nme")
    assert result["error"]["code"] == "cypher_rejected"
    assert "Binder exception" in result["error"]["message"]
    assert "nme" in result["error"]["message"]


async def test_a_cypher_typo_reaches_an_mcp_client_with_its_message(library_config):
    from mcp import Client
    from lore.mcp.server import build_server
    async with Client(build_server(library_config)) as client:
        result = await client.call_tool("cypher_query",
                                        {"query": "MATCH (e:Entity) RETRN e"})
    assert not result.is_error
    assert result.structured_content["error"]["code"] == "cypher_rejected"
    assert "Parser exception" in result.structured_content["error"]["message"]


def test_cypher_tools_without_a_graph(empty_config):
    assert cypher_query(empty_config, "RETURN 1")["error"]["code"] == "no_graph"


async def test_vector_search_needs_no_gemini_key(search_config, monkeypatch):
    """The default path (embed=None) must not build the Gemini-gated query
    stack: selection and synthesis are on Gemini here with no key reachable,
    and a local-only tool still has to answer. Every other test injects
    `embed`, which is how this path went untested."""
    from lore.query import agent as agent_module
    from lore.extract.embed import QUERY_PREFIX

    config = search_config.model_copy(deep=True)
    config.selection.provider = "gemini"
    config.synthesis.provider = "gemini"
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "LORE_GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # No real ~/.config/lore/gemini.env: HOME points at an empty dir.
    monkeypatch.setenv("HOME", str(search_config.data_dir / "no-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(search_config.data_dir / "no-xdg"))

    seen: list[list[str]] = []
    closed: list[bool] = []

    class FakeOllama:
        def __init__(self, *args, **kwargs):
            pass

        async def embed(self, texts, model=None):
            seen.append(list(texts))
            return [_vec(0.0) for _ in texts]

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(agent_module, "OllamaProvider", FakeOllama)

    result = await vector_search(config, "a murder", embed=None)

    assert "error" not in result
    assert result["chunks"]
    assert seen == [[QUERY_PREFIX + "a murder"]]
    assert closed == [True]


# --- concurrent calls ---------------------------------------------------------------

def test_concurrent_tool_calls_never_report_a_false_no_graph(graph_config):
    """Sync tools run on anyio worker threads (up to 40 at once), and each
    call opens its own read-only Kuzu Database. Kuzu reserves `max_db_size`
    of virtual address space per Database -- 8 TiB by default -- so above
    ~14 simultaneous opens the 128 TiB user address space ran out:
    "Mmap for size 8796093022208 failed", reported as no_graph ("may be
    corrupt"). Measured: 16 threads x 200 mixed calls gave 41-73 spurious
    no_graph before kuzu_store bounded read-only opens."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    threads = 32
    barrier = threading.Barrier(threads)
    calls = [
        lambda: entity(graph_config, HOLMES),
        lambda: subgraph(graph_config, HOLMES, depth=2),
        lambda: library(graph_config),
        lambda: cypher_query(graph_config,
                             "MATCH (e:Entity) RETURN e.id ORDER BY e.id"),
    ]

    def worker(n: int) -> list[dict]:
        barrier.wait()
        return [calls[(n + i) % len(calls)]() for i in range(4)]

    with ThreadPoolExecutor(max_workers=threads) as pool:
        results = [r for batch in pool.map(worker, range(threads)) for r in batch]
    errors = [r["error"] for r in results if "error" in r]
    assert errors == []
    assert len(results) == threads * 4


# --- the local embedder down ------------------------------------------------------

async def test_vector_search_with_ollama_down_is_upstream_unavailable(search_config):
    import httpx

    async def down(texts):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    result = await vector_search(search_config, "q", embed=down)
    assert result["error"]["code"] == "upstream_unavailable"
    message = result["error"]["message"]
    assert "Ollama" in message and "Connection refused" in message
    assert "CUDA_VISIBLE_DEVICES=-1" in message


async def test_vector_search_closes_the_embedder_when_ollama_errors(
    search_config, monkeypatch
):
    import httpx
    from lore.query import agent as agent_module
    closed = []

    class DownOllama:
        def __init__(self, *args, **kwargs):
            pass

        async def embed(self, texts, model=None):
            request = httpx.Request("POST", "http://localhost:11434/api/embed")
            raise httpx.HTTPStatusError(
                "404 model not found", request=request,
                response=httpx.Response(404, request=request))

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(agent_module, "OllamaProvider", DownOllama)
    result = await vector_search(search_config, "q", embed=None)
    assert result["error"]["code"] == "upstream_unavailable"
    assert "Ollama" in result["error"]["message"]
    assert closed == [True]


def test_search_result_edges_carry_ids():
    from lore.mcp.shapes import to_search_result
    from lore.query.models import Answer, EdgeRow

    edge = EdgeRow("Holmes", "INTERACTS_WITH", "met", "Watson", "ev", 0.9,
                   "b1", 1, "b1:1:0", subject_id="e1", object_id="e2")
    answer = Answer(text="x", passages=(), mode="graph", template="neighbours",
                    note=None, edges=(edge,))
    out = to_search_result(answer, {}).model_dump()
    assert (out["edges"][0]["subject_id"], out["edges"][0]["object_id"]) == ("e1", "e2")


def test_entity_by_id_opens_that_entity_when_names_collide(tmp_path):
    from lore.mcp.tools import entity, entity_by_id, subgraph_by_id
    config = build_store_triple(
        tmp_path,
        books=[("b1", "A Study")],
        chunks=[("c1", "b1", 1)],
        entities=[("e1", "Stangerson", "Character", ["b1"], []),
                  ("e2", "Stangerson", "Character", ["b1"], []),
                  ("e3", "Hope", "Character", ["b1"], [])],
        # Both Stangersons need an edge: an edgeless namesake is dropped as
        # a candidate, which would make the name unambiguous.
        edges=[("e3", "e2", "KILLS", "c1", "b1", 1),
               ("e3", "e1", "MENTIONS", "c1", "b1", 1)],
    )
    assert entity(config, "Stangerson")["error"]["code"] == "not_found"  # ambiguous by name
    card = entity_by_id(config, "e2")
    assert card["id"] == "e2" and card["edges_in"][0]["subject_id"] == "e3"
    assert entity_by_id(config, "nope")["error"]["code"] == "not_found"
    sub = subgraph_by_id(config, "e2")
    assert {n["id"] for n in sub["nodes"]} == {"e2", "e3"}


def test_entities_by_ids_returns_known_ids_and_skips_unknown(graph_config):
    from lore.mcp.tools import entities_by_ids
    result = entities_by_ids(graph_config, ["w", "nope", "h", "w"])
    assert result == {"entities": [
        {"id": "h", "name": HOLMES, "kind": "Character", "books": [BOOK_ID]},
        {"id": "w", "name": "Dr. Watson", "kind": "Character", "books": [BOOK_ID]},
    ]}
    assert entities_by_ids(graph_config, []) == {"entities": []}


def test_entities_by_ids_caps_the_lookup(graph_config, monkeypatch):
    from lore.mcp import tools as t
    monkeypatch.setattr(t, "ENTITIES_BY_IDS_CAP", 1)
    assert [e["id"] for e in t.entities_by_ids(graph_config, ["w", "h"])["entities"]] == ["w"]


def test_entities_by_ids_without_a_graph_is_no_graph(empty_config):
    from lore.mcp.tools import entities_by_ids
    assert entities_by_ids(empty_config, ["h"])["error"]["code"] == "no_graph"


# --- overview ------------------------------------------------------------------
# The web UI draws this before any question: the most-connected canonical
# entities and the links among them only.

@pytest.fixture
def overview_config(tmp_path: Path) -> LoreConfig:
    def row(i, name, kind="Character"):
        return (i, name, kind, [BOOK_ID], [])

    def edge(s, o, chunk, p="knows"):
        return (s, o, p, chunk, BOOK_ID, 1)

    return build_store_triple(
        tmp_path,
        books=[(BOOK_ID, "A Study")],
        entities=[row("a", "Alpha"), row("b", "Beta"), row("c", "Gamma"),
                  row("d", "Delta Place", "Place"), row("e", "Epsilon"),
                  row("stub", "Mr. Alpha"), row("lonely", "Lonely")],
        aliases=[("stub", "a")],
        edges=[
            # degrees: a 5, b 4, c 3, d 3, e 2; stub 3 (but an alias stub)
            edge("a", "b", "c1"), edge("a", "b", "c2"), edge("b", "a", "c3"),
            edge("a", "c", "c4"), edge("d", "a", "c5"),
            edge("c", "d", "c6"), edge("e", "b", "c7"),
            edge("stub", "e", "c8"), edge("stub", "c", "c9"),
            edge("stub", "d", "c10"),
        ],
    )


def test_overview_ranks_canonical_entities_by_degree(overview_config):
    result = overview(overview_config)
    assert [n["id"] for n in result["nodes"]] == ["a", "b", "c", "d", "e"]
    assert result["depth"] == 0 and result["note"] is None
    assert result["nodes"][3]["kind"] == "Place"


def test_overview_skips_alias_stubs_and_edgeless_entities(overview_config):
    ids = {n["id"] for n in overview(overview_config)["nodes"]}
    assert "stub" not in ids and "lonely" not in ids


def test_overview_links_only_among_the_top_entities(overview_config):
    result = overview(overview_config, limit=3)
    assert [n["id"] for n in result["nodes"]] == ["a", "b", "c"]
    pairs = [(l["subject_id"], l["object_id"]) for l in result["edges"]]
    # Strongest first: a->b backs two passages.
    assert pairs == [("a", "b"), ("a", "c"), ("b", "a")]
    assert result["edges"][0]["count"] == 2
    assert result["edges"][0]["predicates"] == {"knows": 2}
    assert result["truncated"] is True


def test_overview_clamps_the_limit(overview_config):
    assert [n["id"] for n in overview(overview_config, limit=0)["nodes"]] == ["a"]
    assert len(overview(overview_config, limit=1000)["nodes"]) == 5
    assert overview(overview_config, limit=1000)["truncated"] is False


def test_overview_caps_links(overview_config, monkeypatch):
    assert tools_module.OVERVIEW_LINK_CAP == 60
    monkeypatch.setattr(tools_module, "OVERVIEW_LINK_CAP", 2)
    result = overview(overview_config)
    assert len(result["edges"]) == 2 and result["truncated"] is True


def test_overview_without_a_graph_returns_no_graph(empty_config):
    assert overview(empty_config)["error"]["code"] == "no_graph"


def test_search_result_carries_prose():
    from lore.mcp.shapes import to_search_result
    from lore.query.models import Answer, Claim, ProseSentence
    answer = Answer(text="A. [1]", passages=(), mode="graph", template=None, note=None,
                    claims=(Claim(text="A", passage=1, quote="a b c d"),),
                    prose=(ProseSentence("A.", (1,), (1,)),), answer_style="prose")
    out = to_search_result(answer, {}).model_dump()
    assert out["answer_style"] == "prose"
    assert out["prose"] == [{"text": "A.", "claims": [1], "passages": [1], "new_paragraph": False,
                                "kind": "fact"}]


def test_search_result_maps_new_paragraph():
    from lore.mcp.shapes import to_search_result
    from lore.query.models import Answer, Claim, ProseSentence
    answer = Answer(text="A. [1]\n\nB. [1]", passages=(), mode="graph", template=None, note=None,
                    claims=(Claim(text="A", passage=1, quote="a b c d"),),
                    prose=(ProseSentence("A.", (1,), (1,)), ProseSentence("B.", (1,), (1,), True)),
                    answer_style="prose")
    out = to_search_result(answer, {}).model_dump()
    assert [p["new_paragraph"] for p in out["prose"]] == [False, True]


def test_search_result_without_prose_is_claims_style():
    from lore.mcp.shapes import to_search_result
    from lore.query.models import Answer
    out = to_search_result(Answer(text="x", passages=(), mode="graph", template=None, note=None),
                           {}).model_dump()
    assert out["prose"] == [] and out["answer_style"] == "claims"


def test_search_result_carries_unanswered():
    from lore.mcp.shapes import to_search_result
    from lore.query.models import Answer
    base = dict(text="x", passages=(), mode="graph", template=None, note=None)
    assert to_search_result(Answer(**base), {}).model_dump()["unanswered"] == []
    out = to_search_result(Answer(**base, unanswered=("How?",)), {}).model_dump()
    assert out["unanswered"] == ["How?"]


def test_search_result_carries_gaps():
    from lore.mcp.shapes import to_search_result
    from lore.query.models import Answer, Claim, ProseSentence
    answer = Answer(text="A. [1]", passages=(), mode="graph", template=None, note=None,
                    claims=(Claim(text="A", passage=1, quote="a b c d"),),
                    prose=(ProseSentence("A.", (1,), (1,)),), answer_style="prose",
                    gaps=("whether it was used later",))
    out = to_search_result(answer, {}).model_dump()
    assert out["gaps"] == ["whether it was used later"]
    plain = to_search_result(Answer(text="x", passages=(), mode="graph", template=None, note=None),
                             {}).model_dump()
    assert plain["gaps"] == []
