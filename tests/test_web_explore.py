"""The graph explorer's read endpoints on a real (tmp) Kuzu, SQLite and Lance.

Invented names. Alpha Saga (books ba1, ba2) gets a small cast on top of the
shared worlds fixture:

    Zed Quill --INTERACTS_WITH/ALLIED_WITH--> Yan Vale --LEADS_TO--> Wick Orrin
    Wick Orrin --LEADS_TO--> Pell Haven;  Lone Tarn has no edges.

Beta Realm has its own Zed Quill and Yan Vale, which must never leak in.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lore.beats.store import Beat, save_beats
from lore.codex.store import CodexEntry, Statement, save_entry
from lore.db.kuzu_store import KuzuStore
from lore.db.sqlite import open_db
from lore.web.app import create_app
from tests.worlds_fixtures import ALPHA, BETA, build_library

ZED_QUOTE = "ALPHAMARK a_zed met a_yan"


def _add_entities(config, rows) -> None:
    graph = KuzuStore(config.kuzu_dir / "graph")
    for eid, name, kind, books, aliases in rows:
        graph.execute("CREATE (:Entity {id: $id, name: $n, kind: $k, books: $b,"
                      " aliases: $a})", {"id": eid, "n": name, "k": kind,
                                         "b": books, "a": aliases})
    graph.close()


def _add_edges(config, rows) -> None:
    """(subject, object, predicate, book, chunk, chapter, evidence)"""
    graph = KuzuStore(config.kuzu_dir / "graph")
    for s, o, pred, book, chunk, chapter, ev in rows:
        graph.execute(
            "MATCH (s:Entity {id: $s}), (o:Entity {id: $o}) CREATE (s)-[:REL {predicate:"
            " $p, predicate_raw: $p, evidence: $ev, confidence: 0.9, chunk_id: $c,"
            " book_id: $b, chapter_seq: $ch, run_id: 'r'}]->(o)",
            {"s": s, "o": o, "p": pred, "ev": ev, "c": chunk, "b": book, "ch": chapter})
    graph.close()


def _sql(config, *statements) -> None:
    conn = open_db(config.sqlite_path)
    for sql, params in statements:
        conn.execute(sql, params)
    conn.commit()
    conn.close()


@pytest.fixture
def config(tmp_path):
    config = build_library(tmp_path)
    _add_entities(config, [
        ("a_wick", "Wick Orrin", "Character", ["ba1", "ba2"], ["The Ferryman"]),
        ("a_pell", "Pell Haven", "Place", ["ba2"], []),
        ("a_lone", "Lone Tarn", "Place", ["ba1"], []),
        ("a_zeda", "Zed Aab", "Character", ["ba1"], []),
        ("a_zoyan", "Zoyan Ride", "Character", ["ba1"], []),
    ])
    _add_edges(config, [
        ("a_yan", "a_wick", "LEADS_TO", "ba1", "ba1:2:0", 2, "Yan led the way to Wick"),
        ("a_wick", "a_pell", "LEADS_TO", "ba2", "ba2:2:0", 2, "Wick poled to Pell Haven"),
        ("a_yan", "a_zed", "TRUSTS", "ba2", "ba2:2:0", 2, "Yan trusted Zed"),
        ("a_zed", "a_yan", "ALLIED_WITH", "ba2", "ba2:1:0", 1, "Zed allied with Yan"),
    ])
    # The Alpha edge duplicates (a_zed, a_yan, ba1) from the fixture: that one
    # is "ALPHAMARK a_zed met a_yan".
    conn = open_db(config.sqlite_path)
    entry = CodexEntry(
        entity_id="a_zed", world_id=1, name="Zed Quill", kind="Character",
        summary="A traveller who walks the hills with Yan Vale.",
        statements=(
            Statement("nature", "Zed is a traveller.", "the traveller Zed Quill", "ba1:1:0"),
            Statement("history", "Zed walked on with Yan.", "walked on with Yan Vale",
                      "ba2:1:0"),
            Statement("history", "Zed met Yan.", "Zed Quill walked", "ba1:2:0")))
    save_entry(conn, entry)
    conn.close()
    return config


@pytest.fixture
def client(config):
    return TestClient(create_app(config), base_url="http://127.0.0.1:8766")


def _get(client, url, **params):
    return client.get(url, params=params).json()


def _not_found(body) -> bool:
    return body.get("error", {}).get("code") == "not_found"


# --- profile ------------------------------------------------------------------------

def test_profile_shape_and_aspects_grouped_in_saved_order(client) -> None:
    body = _get(client, "/api/entity/a_zed/profile", world=ALPHA)
    assert set(body) == {"id", "name", "kind", "books", "summary", "aspects", "counts"}
    assert (body["id"], body["name"], body["kind"]) == ("a_zed", "Zed Quill", "Character")
    assert body["books"] == ["ba1", "ba2"]
    assert body["summary"] == "A traveller who walks the hills with Yan Vale."
    assert [a["aspect"] for a in body["aspects"]] == ["nature", "history"]
    history = body["aspects"][1]["statements"]
    assert history[0] == {"text": "Zed walked on with Yan.",
                          "quote": "walked on with Yan Vale", "chunk_id": "ba2:1:0",
                          "book_id": "ba2", "chapter": 1}
    assert history[1]["book_id"] == "ba1" and history[1]["chapter"] == 2
    # chapters: distinct (book, chapter) over the edges and beats: ba1/1, ba2/1, ba2/2.
    assert body["counts"] == {"beats": 1, "relations": 1, "chapters": 3}


def test_profile_without_a_codex_entry_has_a_null_summary_and_no_aspects(client) -> None:
    body = _get(client, "/api/entity/a_yan/profile", world=ALPHA)
    assert body["summary"] is None
    assert body["aspects"] == []
    assert body["name"] == "Yan Vale"
    assert body["counts"]["relations"] == 2  # Zed and Wick


def test_profile_follows_an_alias_stub_to_its_canonical(config, client) -> None:
    _add_entities(config, [("a_zstub", "Zed", "Character", ["ba1"], [])])
    graph = KuzuStore(config.kuzu_dir / "graph")
    graph.execute("MATCH (a:Entity {id:'a_zstub'}), (c:Entity {id:'a_zed'})"
                  " CREATE (a)-[:ALIAS_OF]->(c)")
    graph.close()
    assert _get(client, "/api/entity/a_zstub/profile", world=ALPHA)["id"] == "a_zed"


# --- world isolation ----------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "/api/entity/b_zed/profile", "/api/entity/b_zed/timeline",
    "/api/entity/b_zed/relations", "/api/entity/nobody/profile"])
def test_entity_endpoints_say_not_found_outside_the_world(client, url) -> None:
    assert _not_found(_get(client, url, world=ALPHA))


def test_each_world_reads_its_own_same_named_entity(client) -> None:
    alpha = _get(client, "/api/entity/a_zed/relations", world=ALPHA)
    beta = _get(client, "/api/entity/b_zed/relations", world=BETA)
    assert [r["other"]["id"] for r in alpha["relations"]] == ["a_yan"]
    assert [r["other"]["id"] for r in beta["relations"]] == ["b_yan"]
    timeline = _get(client, "/api/entity/b_zed/timeline", world=BETA)
    assert [b["text"] for b in timeline["beats"]] == ["BETABEAT"]


def test_a_cross_world_edge_is_not_shown_in_either_world(tmp_path) -> None:
    config = build_library(tmp_path, cross_edge=True)
    client = TestClient(create_app(config), base_url="http://127.0.0.1:8766")
    rel = _get(client, "/api/entity/a_zed/relations", world=ALPHA)["relations"]
    assert "b_yan" not in {r["other"]["id"] for r in rel}


def test_edge_path_search_and_passage_are_scoped_to_the_world(client) -> None:
    assert _not_found(_get(client, "/api/edge", a="a_zed", b="b_yan", world=ALPHA))
    assert _not_found(_get(client, "/api/edge", a="b_zed", b="b_yan", world=ALPHA))
    assert _not_found(_get(client, "/api/path", a="a_zed", b="b_yan", world=ALPHA))
    ids = {r["id"] for r in _get(client, "/api/search", q="zed", world=ALPHA)["results"]}
    assert ids == {"a_zed", "a_zeda"}
    assert _not_found(_get(client, "/api/passage/bb1:1:0", world=ALPHA))
    assert _get(client, "/api/passage/bb1:1:0", world=BETA)["book_id"] == "bb1"


def test_an_unknown_world_is_not_found(client) -> None:
    for url, params in [("/api/entity/a_zed/profile", {}), ("/api/search", {"q": "z"}),
                        ("/api/passage/ba1:1:0", {}), ("/api/edge", {"a": "a_zed", "b": "a_yan"})]:
        assert _not_found(_get(client, url, world="Nowhere", **params))


# --- timeline -----------------------------------------------------------------------

def test_timeline_shape(client) -> None:
    body = _get(client, "/api/entity/a_zed/timeline", world=ALPHA)
    assert set(body) == {"beats", "order_note"}
    assert body["order_note"] is None
    assert body["beats"] == [{
        "kind": "action", "text": "ALPHABEAT", "quote": "walked on",
        "chunk_id": "ba1:1:0", "book_id": "ba1", "book_title": "Alpha One",
        "chapter": 1, "position_label": "Alpha One, book 1 of Alpha Saga, chapter 1",
        "others": []}]


def _more_beats(config) -> None:
    conn = open_db(config.sqlite_path)
    for beat_id, book, chapter, others in [
            ("beat-ba2", "ba2", 1, ("Zed Quill", "Yan Vale")),
            ("beat-ba2b", "ba2", 2, ("Zed Quill",))]:
        save_beats(conn, book, chapter, 0, [Beat(
            id=beat_id, book_id=book, chapter_seq=chapter,
            chunk_id=f"{book}:{chapter}:0", entities=others, kind="decision",
            text=f"T-{beat_id}", quote="walked on")])
        conn.execute("UPDATE beat_entities SET entity_id = ? WHERE beat_id = ? AND name = ?",
                     ("a_zed", beat_id, "Zed Quill"))
        conn.execute("UPDATE beat_entities SET entity_id = ? WHERE beat_id = ? AND name = ?",
                     ("a_yan", beat_id, "Yan Vale"))
    conn.commit()
    conn.close()


def test_timeline_is_in_story_order_not_id_order(config, client) -> None:
    _more_beats(config)
    # Make Alpha Two the first book of the series: story order flips id order.
    _sql(config, ("UPDATE books SET series_index = 2 WHERE id = 'ba1'", ()),
         ("UPDATE books SET series_index = 1 WHERE id = 'ba2'", ()))
    body = _get(client, "/api/entity/a_zed/timeline", world=ALPHA)
    assert [(b["book_id"], b["chapter"]) for b in body["beats"]] == [
        ("ba2", 1), ("ba2", 2), ("ba1", 1)]
    assert body["beats"][0]["position_label"] == "Alpha Two, book 1 of Alpha Saga, chapter 1"
    assert body["order_note"] is None
    # The other entity in a shared beat, as {id, name}; Zed himself is left out.
    assert body["beats"][0]["others"] == [{"id": "a_yan", "name": "Yan Vale"}]


def test_timeline_says_when_the_books_order_is_unknown(config, client) -> None:
    _more_beats(config)
    _sql(config, ("UPDATE books SET series = NULL, series_index = NULL"
                  " WHERE id IN ('ba1', 'ba2')", ()))
    body = _get(client, "/api/entity/a_zed/timeline", world=ALPHA)
    assert "unknown" in body["order_note"]
    assert "Alpha One" in body["order_note"] and "Alpha Two" in body["order_note"]
    # Chapter order inside one book still holds.
    ba2 = [b["chapter"] for b in body["beats"] if b["book_id"] == "ba2"]
    assert ba2 == sorted(ba2)
    assert all("book 1 of" not in b["position_label"] for b in body["beats"])


def test_timeline_of_an_entity_with_no_beats_is_empty(client) -> None:
    assert _get(client, "/api/entity/a_pell/timeline", world=ALPHA)["beats"] == []


# --- relations ----------------------------------------------------------------------

def test_relations_group_by_other_entity_most_mentioned_first(client) -> None:
    body = _get(client, "/api/entity/a_yan/relations", world=ALPHA)
    assert set(body) == {"relations", "order_note"}
    others = [r["other"] for r in body["relations"]]
    # Zed: INTERACTS x2, ALLIED_WITH, TRUSTS = 4 edges; Wick: 1.
    assert others == [{"id": "a_zed", "name": "Zed Quill", "kind": "Character"},
                      {"id": "a_wick", "name": "Wick Orrin", "kind": "Character"}]
    zed = body["relations"][0]
    assert zed["predicates"][0] == {"predicate": "INTERACTS_WITH", "count": 2,
                                    "direction": "in"}
    assert {(p["predicate"], p["direction"]) for p in zed["predicates"]} == {
        ("INTERACTS_WITH", "in"), ("ALLIED_WITH", "in"), ("TRUSTS", "out")}


def test_relations_evidence_is_capped_and_in_story_order(client) -> None:
    zed = _get(client, "/api/entity/a_yan/relations", world=ALPHA)["relations"][0]
    assert len(zed["evidence"]) == 3
    assert zed["evidence"][0] == {"quote": ZED_QUOTE, "chunk_id": "ba1:1:0",
                                  "book_id": "ba1", "chapter": 1}
    chapters = [(e["book_id"], e["chapter"]) for e in zed["evidence"]]
    assert chapters == [("ba1", 1), ("ba2", 1), ("ba2", 1)]
    assert zed["first_chapter"] == {"book_id": "ba1", "book_title": "Alpha One", "chapter": 1}
    assert zed["last_chapter"] == {"book_id": "ba2", "book_title": "Alpha Two", "chapter": 2}


def test_relations_of_an_entity_with_none_is_empty(client) -> None:
    assert _get(client, "/api/entity/a_lone/relations", world=ALPHA)["relations"] == []


# --- edge ---------------------------------------------------------------------------

def test_edge_lists_both_directions_in_story_order(client) -> None:
    body = _get(client, "/api/edge", a="a_zed", b="a_yan", world=ALPHA)
    assert set(body) == {"a", "b", "relationships", "predicates", "order_note"}
    assert body["a"] == {"id": "a_zed", "name": "Zed Quill", "kind": "Character"}
    rels = body["relationships"]
    assert [(r["book_id"], r["chapter"], r["predicate"]) for r in rels] == [
        ("ba1", 1, "INTERACTS_WITH"), ("ba2", 1, "ALLIED_WITH"),
        ("ba2", 1, "INTERACTS_WITH"), ("ba2", 2, "TRUSTS")]
    first = rels[0]
    assert first["subject"]["id"] == "a_zed" and first["object"]["id"] == "a_yan"
    assert first["quote"] == ZED_QUOTE and first["chunk_id"] == "ba1:1:0"
    assert first["book_title"] == "Alpha One"
    assert first["position_label"] == "Alpha One, book 1 of Alpha Saga, chapter 1"
    trusts = next(r for r in rels if r["predicate"] == "TRUSTS")
    assert trusts["subject"]["id"] == "a_yan" and trusts["object"]["id"] == "a_zed"
    assert {p["predicate"] for p in body["predicates"]} == {
        "INTERACTS_WITH", "ALLIED_WITH", "TRUSTS"}


def test_edge_is_symmetric_in_what_it_finds(client) -> None:
    ab = _get(client, "/api/edge", a="a_zed", b="a_yan", world=ALPHA)["relationships"]
    ba = _get(client, "/api/edge", a="a_yan", b="a_zed", world=ALPHA)["relationships"]
    assert ab == ba


def test_edge_between_unconnected_entities_is_empty(client) -> None:
    body = _get(client, "/api/edge", a="a_zed", b="a_lone", world=ALPHA)
    assert body["relationships"] == [] and body["predicates"] == []


# --- search -------------------------------------------------------------------------

def _ids(client, q, **params):
    return [r["id"] for r in _get(client, "/api/search", q=q, world=ALPHA, **params)["results"]]


def test_search_shape(client) -> None:
    results = _get(client, "/api/search", q="Wick", world=ALPHA)["results"]
    assert results == [{"id": "a_wick", "name": "Wick Orrin", "kind": "Character",
                        "books": ["ba1", "ba2"], "matched_alias": None}]


def test_search_prefix_beats_substring_and_degree_beats_name_order(client) -> None:
    # "Zed Aab" sorts before "Zed Quill" but has no edges.
    assert _ids(client, "zed") == ["a_zed", "a_zeda"]
    # "yan": a prefix hit first, then the substring hit inside "Zoyan Ride".
    assert _ids(client, "yan") == ["a_yan", "a_zoyan"]


def test_search_exact_name_ranks_first(client) -> None:
    assert _ids(client, "zed aab")[0] == "a_zeda"


def test_search_by_alias_reports_the_alias(client) -> None:
    results = _get(client, "/api/search", q="the ferryman", world=ALPHA)["results"]
    assert [(r["id"], r["matched_alias"]) for r in results] == [("a_wick", "The Ferryman")]
    assert _ids(client, "ferry") == ["a_wick"]  # a word of the alias


def test_search_is_case_insensitive_and_ignores_stubs(config, client) -> None:
    _add_entities(config, [("a_wstub", "Wick Orrin", "Character", ["ba1"], [])])
    graph = KuzuStore(config.kuzu_dir / "graph")
    graph.execute("MATCH (a:Entity {id:'a_wstub'}), (c:Entity {id:'a_wick'})"
                  " CREATE (a)-[:ALIAS_OF]->(c)")
    graph.close()
    assert _ids(client, "WICK") == ["a_wick"]


def test_search_fuzzy_tolerates_a_misspelling(client) -> None:
    assert _ids(client, "yan vael")[0] == "a_yan"
    assert _ids(client, "pel haven")[0] == "a_pell"
    assert _ids(client, "xqzjw") == []


def test_search_limit_and_blank_query(client) -> None:
    assert len(_ids(client, "zed", limit=1)) == 1
    assert _get(client, "/api/search", q="  ", world=ALPHA) == {"results": []}
    assert _get(client, "/api/search", world=ALPHA) == {"results": []}


def test_search_without_a_world_covers_every_world_and_tags_each(client) -> None:
    results = _get(client, "/api/search", q="zed quill")["results"]
    by_id = {r["id"]: r for r in results}
    assert {"a_zed", "b_zed"} <= set(by_id)
    assert by_id["a_zed"]["world"] == {"id": 1, "name": ALPHA}
    assert by_id["b_zed"]["world"] == {"id": 2, "name": BETA}


# --- path ---------------------------------------------------------------------------

def test_path_found_through_rel_edges_either_way_round(client) -> None:
    body = _get(client, "/api/path", a="a_zed", b="a_pell", world=ALPHA)
    assert body["found"] is True and body["timed_out"] is False
    assert [n["id"] for n in body["nodes"]] == ["a_zed", "a_yan", "a_wick", "a_pell"]
    assert body["nodes"][0] == {"id": "a_zed", "name": "Zed Quill", "kind": "Character"}
    assert [(h["from"], h["to"]) for h in body["hops"]] == [
        ("a_zed", "a_yan"), ("a_yan", "a_wick"), ("a_wick", "a_pell")]
    hop = body["hops"][1]
    assert hop["predicates"] == [{"predicate": "LEADS_TO", "count": 1, "direction": "out"}]
    assert hop["evidence"] == [{"quote": "Yan led the way to Wick", "chunk_id": "ba1:2:0",
                                "book_id": "ba1", "chapter": 2}]
    # Walking against an edge's direction is still a path.
    back = _get(client, "/api/path", a="a_pell", b="a_zed", world=ALPHA)
    assert [n["id"] for n in back["nodes"]] == ["a_pell", "a_wick", "a_yan", "a_zed"]
    assert back["hops"][0]["predicates"][0]["direction"] == "in"


def test_path_is_capped_at_max_hops(client) -> None:
    short = _get(client, "/api/path", a="a_zed", b="a_pell", world=ALPHA, max=2)
    assert short == {"nodes": [], "hops": [], "found": False, "timed_out": False}
    assert _get(client, "/api/path", a="a_zed", b="a_pell", world=ALPHA, max=3)["found"]
    # More than 4 is clamped to 4, never an unbounded search.
    assert _get(client, "/api/path", a="a_zed", b="a_pell", world=ALPHA, max=99)["found"]


def test_path_not_found_is_an_empty_list(client) -> None:
    body = _get(client, "/api/path", a="a_zed", b="a_lone", world=ALPHA)
    assert body == {"nodes": [], "hops": [], "found": False, "timed_out": False}


def test_path_to_itself_is_one_node_and_no_hops(client) -> None:
    body = _get(client, "/api/path", a="a_zed", b="a_zed", world=ALPHA)
    assert body["found"] and [n["id"] for n in body["nodes"]] == ["a_zed"]
    assert body["hops"] == []


def test_path_time_cap_gives_up_and_says_so(client, monkeypatch) -> None:
    from lore.query import explore
    monkeypatch.setattr(explore, "PATH_TIME_CAP_S", -1.0)
    body = _get(client, "/api/path", a="a_zed", b="a_pell", world=ALPHA)
    assert body["found"] is False and body["timed_out"] is True and body["hops"] == []


# --- passage ------------------------------------------------------------------------

def test_passage_returns_the_text_with_its_place(client) -> None:
    body = _get(client, "/api/passage/ba2:1:0", world=ALPHA)
    assert set(body) == {"chunk_id", "book_id", "book_title", "chapter", "text",
                         "position_label"}
    assert body["chunk_id"] == "ba2:1:0" and body["chapter"] == 1
    assert body["book_title"] == "Alpha Two"
    assert body["text"].startswith("ALPHAMARK ba2 chapter 1")
    assert body["position_label"] == "Alpha Two, book 2 of Alpha Saga, chapter 1"
    assert _not_found(_get(client, "/api/passage/nope:1:0"))


def test_passage_without_a_world_reads_any_book(client) -> None:
    assert _get(client, "/api/passage/bb1:1:0")["book_id"] == "bb1"


# --- no Gemini ----------------------------------------------------------------------

def test_browsing_never_builds_a_model(client, monkeypatch) -> None:
    from lore.mcp import tools
    def boom(*a, **k):
        raise AssertionError("browsing must not reach a model")
    monkeypatch.setattr(tools, "_build_models", boom)
    for url, params in [("/api/entity/a_zed/profile", {}), ("/api/entity/a_zed/timeline", {}),
                        ("/api/entity/a_zed/relations", {}),
                        ("/api/edge", {"a": "a_zed", "b": "a_yan"}),
                        ("/api/search", {"q": "zed"}),
                        ("/api/path", {"a": "a_zed", "b": "a_pell"}),
                        ("/api/passage/ba1:1:0", {})]:
        r = client.get(url, params={**params, "world": ALPHA})
        assert r.status_code == 200 and "error" not in r.json()
