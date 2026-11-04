"""Every read endpoint applies the world's stored reading position.

Real stores over tests/position_fixtures.py: in Hollow Saga, "Old Tam" (an alias
stub of "Ren Ashby") is met in chapters 1-2 of the first book; chapter 3 holds
SECRETFACT and Dorn Vale, whom no one meets before it. With the position off,
or `?spoilers=1`, everything is visible.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lore.mcp import tools
from lore.query import explore
from lore.web.app import create_app
from tests.position_fixtures import HOLLOW, OTHER, build_position_library


@pytest.fixture
def config(tmp_path):
    return build_position_library(tmp_path)


@pytest.fixture
def client(config):
    return TestClient(create_app(config), base_url="http://127.0.0.1:8766")


def put(client, world=HOLLOW, *, enabled=True, book="hb1", chapter=2, read=()):
    r = client.put("/api/reading-position", params={"world": world},
                   json={"enabled": enabled, "book_id": book, "chapter": chapter,
                         "read_books": list(read)})
    assert r.status_code == 200, r.text
    return r.json()


def get(client, path, **params):
    params.setdefault("world", HOLLOW)
    r = client.get(path, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def text_of(payload) -> str:
    return json.dumps(payload)


# --- the position endpoint -------------------------------------------------------------

def test_a_world_starts_with_no_position_and_lists_its_books_in_series_order(client):
    got = get(client, "/api/reading-position")
    assert got["enabled"] is False and got["book_id"] is None and got["chapter"] == 0
    assert [b["id"] for b in got["books"]] == ["hb1", "hb2", "hb3"]
    assert [b["chapters"] for b in got["books"]] == [3, 3, 2]


def test_the_position_persists_and_says_which_books_need_a_read_toggle(client):
    put(client, book="hb2", chapter=1, read=["hb3"])
    got = get(client, "/api/reading-position")
    assert (got["enabled"], got["book_id"], got["chapter"], got["read_books"]) == (True, "hb2", 1, ["hb3"])
    assert {b["id"]: b["order"] for b in got["books"]} == {
        "hb1": "before", "hb2": "current", "hb3": "unknown"}
    # another world has its own, untouched
    other = get(client, "/api/reading-position", world=OTHER)
    assert other["enabled"] is False


def test_the_position_endpoint_refuses_what_makes_no_sense(client):
    base = {"enabled": True, "book_id": "hb1", "chapter": 1, "read_books": []}
    for body, why in [({**base, "book_id": "ob1"}, "no book"),
                      ({**base, "chapter": 9}, "no chapter 9"),
                      ({**base, "book_id": None}, "choose the book"),
                      ({**base, "read_books": ["nope"]}, "no book")]:
        r = client.put("/api/reading-position", params={"world": HOLLOW}, json=body)
        assert r.status_code == 400 and why in r.json()["error"]["message"]
    assert client.get("/api/reading-position").status_code == 400  # a world is needed
    assert client.get("/api/reading-position", params={"world": "nowhere"}).status_code == 404
    off = put(client, enabled=False, book=None, chapter=0)
    assert off["enabled"] is False


def test_the_position_can_be_looked_up_by_world_id(client):
    wid = get(client, "/api/reading-position")["world"]["id"]
    put(client, world=str(wid), chapter=1)
    assert get(client, "/api/reading-position", world=str(wid))["enabled"] is True


# --- the graph -----------------------------------------------------------------------------

def test_the_overview_shows_only_what_has_been_met(client):
    full = get(client, "/api/overview")
    assert {n["id"] for n in full["nodes"]} >= {"h_dorn", "w_pip"}
    put(client, chapter=2)
    got = get(client, "/api/overview")
    ids = {n["id"] for n in got["nodes"]}
    assert ids == {"h_ren", "h_mira"}  # not Dorn (ch 3), not the unread book
    assert [n["name"] for n in got["nodes"] if n["id"] == "h_ren"] == ["Old Tam"]
    assert len(got["edges"]) == 1 and got["edges"][0]["count"] == 1  # only the chapter 1 edge
    assert {e["subject"] for e in got["edges"]} == {"Old Tam"}
    assert "Ren Ashby" not in text_of(got) and "SECRETFACT" not in text_of(got)
    assert "BETRAYED" not in text_of(got) and "Dorn" not in text_of(got)


def test_moving_the_position_forward_reveals_in_story_order(client):
    put(client, chapter=3)
    got = get(client, "/api/overview")
    assert {n["id"] for n in got["nodes"]} == {"h_ren", "h_mira", "h_dorn"}
    link = next(e for e in got["edges"] if e["object_id"] == "h_mira")
    assert link["count"] == 2 and set(link["predicates"]) == {"MET", "BETRAYED"}
    put(client, chapter=0)
    assert get(client, "/api/overview")["nodes"] == []


def test_the_subgraph_expands_only_through_visible_edges_under_the_earliest_alias(client):
    put(client, chapter=2)
    got = get(client, "/api/subgraph", id="h_ren", depth=2)
    assert {n["id"] for n in got["nodes"]} == {"h_ren", "h_mira"}
    assert all("Ren Ashby" not in text_of(e) for e in got["edges"])
    assert "Old Tam" in text_of(got["nodes"])
    # asked for by the alias stub's id: the same entity, the same view
    assert get(client, "/api/subgraph", id="h_tam", depth=2) == got
    full = get(client, "/api/subgraph", id="h_ren", spoilers=1)
    assert {n["id"] for n in full["nodes"]} == {"h_ren", "h_mira", "h_dorn"}
    assert "Ren Ashby" in text_of(full["nodes"])


def test_an_entity_not_met_yet_is_not_found(client):
    put(client, chapter=2)
    for path in ("/api/entity", "/api/subgraph"):
        got = get(client, path, id="h_dorn")
        assert got["error"]["code"] == "not_found"
    assert get(client, "/api/entity/h_dorn/profile")["error"]["code"] == "not_found"
    assert get(client, "/api/entity", id="h_dorn", spoilers=1)["id"] == "h_dorn"
    # and an entity of an unread book
    assert get(client, "/api/entity", id="w_pip")["error"]["code"] == "not_found"


def test_looking_up_by_a_name_not_yet_revealed_finds_nothing(client):
    put(client, chapter=2)
    assert get(client, "/api/entity", name="Ren Ashby")["error"]["code"] == "not_found"
    assert get(client, "/api/entity", name="Old Tam")["name"] == "Old Tam"
    assert get(client, "/api/subgraph", name="Ren Ashby")["error"]["code"] == "not_found"
    put(client, chapter=3)
    assert get(client, "/api/entity", name="Ren Ashby")["id"] == "h_ren"


def test_the_card_names_the_entity_by_the_earliest_alias_and_lists_visible_aliases(client):
    put(client, chapter=2)
    card = get(client, "/api/entity", id="h_ren")
    assert card["name"] == "Old Tam" and card["aliases"] == []
    assert card["edges_out_total"] == 1 and card["edges_in_total"] == 0
    assert "Ren Ashby" not in text_of(card)
    put(client, chapter=3)
    card = get(client, "/api/entity", id="h_ren")
    assert card["name"] == "Old Tam" and card["aliases"] == ["Ren Ashby"]
    assert card["edges_out_total"] == 3


def test_entities_by_ids_drops_the_unmet_and_renames(client):
    put(client, chapter=2)
    got = get(client, "/api/entities", ids="h_ren,h_dorn,w_pip,h_mira")["entities"]
    assert {e["id"]: e["name"] for e in got} == {"h_ren": "Old Tam", "h_mira": "Mira Cole"}


def test_path_search_and_passage_respect_the_position(client):
    put(client, chapter=2)
    assert get(client, "/api/path", a="h_ren", b="h_mira")["found"] is True
    assert get(client, "/api/path", a="h_ren", b="h_dorn")["error"]["code"] == "not_found"
    full = get(client, "/api/path", a="h_ren", b="h_mira")
    assert [n["name"] for n in full["nodes"]] == ["Old Tam", "Mira Cole"]
    assert "BETRAYED" not in text_of(full) and "SECRETFACT" not in text_of(full)
    # search finds the name met, never the merged canonical name, never the unmet
    assert [r["name"] for r in get(client, "/api/search", q="tam")["results"]] == ["Old Tam"]
    assert get(client, "/api/search", q="ashby")["results"] == []
    assert get(client, "/api/search", q="dorn")["results"] == []
    assert get(client, "/api/search", q="pip")["results"] == []
    assert get(client, "/api/search", q="ashby", spoilers=1)["results"][0]["id"] == "h_ren"
    # passages
    assert get(client, "/api/passage/hb1:2:0")["chunk_id"] == "hb1:2:0"
    assert get(client, "/api/passage/hb1:3:0")["error"]["code"] == "not_found"
    assert get(client, "/api/passage/hb2:1:0")["error"]["code"] == "not_found"
    assert "SECRETFACT" in get(client, "/api/passage/hb1:3:0", spoilers=1)["text"]


def test_a_search_matches_every_visible_alias(client):
    put(client, chapter=3)
    assert get(client, "/api/search", q="ashby")["results"][0]["id"] == "h_ren"
    assert get(client, "/api/search", q="ashby")["results"][0]["name"] == "Old Tam"
    assert get(client, "/api/search", q="ashby")["results"][0]["matched_alias"] == "Ren Ashby"


# --- the entity panel ------------------------------------------------------------------------------

def test_the_profile_hides_later_statements_and_the_whole_story_summary(client):
    full = get(client, "/api/entity/h_ren/profile")
    assert full["summary"] == "WHOLESTORYSUMMARY" and "summary_hidden" not in full
    put(client, chapter=2)
    got = get(client, "/api/entity/h_ren/profile")
    assert got["summary"] is None and got["summary_hidden"] is True
    assert got["name"] == "Old Tam"
    statements = [s["text"] for a in got["aspects"] for s in a["statements"]]
    assert statements == ["EARLYSTATE"]
    assert got["counts"] == {"beats": 1, "relations": 1, "chapters": 1}
    assert "WHOLESTORYSUMMARY" not in text_of(got) and "Ren Ashby" not in text_of(got)
    put(client, book="hb2", chapter=1)
    got = get(client, "/api/entity/h_ren/profile")
    assert sorted(s["text"] for a in got["aspects"] for s in a["statements"]) == [
        "EARLYSTATE", "LATESTATE", "SEQUELSTATE"]
    assert got["counts"]["beats"] == 3


def test_the_timeline_and_relations_hide_what_comes_later(client):
    put(client, chapter=2)
    beats = get(client, "/api/entity/h_ren/timeline")["beats"]
    assert [b["text"] for b in beats] == ["EARLYBEAT"]
    rel = get(client, "/api/entity/h_ren/relations")["relations"]
    assert [r["other"]["id"] for r in rel] == ["h_mira"]
    assert [p["predicate"] for p in rel[0]["predicates"]] == ["MET"]
    assert len(rel[0]["evidence"]) == 1 and "BETRAYED" not in text_of(rel)
    assert [b["text"] for b in get(client, "/api/entity/h_ren/timeline", spoilers=1)["beats"]] == [
        "EARLYBEAT", "LATEBEAT", "SEQUELBEAT"]
    put(client, book="hb2", chapter=1)
    assert [b["text"] for b in get(client, "/api/entity/h_ren/timeline")["beats"]] == [
        "EARLYBEAT", "LATEBEAT", "SEQUELBEAT"]


def test_edge_evidence_is_only_what_is_visible(client):
    put(client, chapter=2)
    edge = get(client, "/api/edge", a="h_ren", b="h_mira")
    assert [r["predicate"] for r in edge["relationships"]] == ["MET"]
    assert edge["a"]["name"] == "Old Tam" and "Ren Ashby" not in text_of(edge)
    assert "SECRETFACT" not in text_of(edge) and "BETRAYED" not in text_of(edge)
    full = get(client, "/api/edge", a="h_ren", b="h_mira", spoilers=1)
    assert sorted(r["predicate"] for r in full["relationships"]) == [
        "BETRAYED", "MET", "TRUSTS"]
    put(client, chapter=0)
    assert get(client, "/api/edge", a="h_ren", b="h_mira")["error"]["code"] == "not_found"


def test_the_library_lists_only_books_with_something_read(client):
    assert len(get(client, "/api/library")["books"]) == 3
    put(client, chapter=1)
    assert [b["id"] for b in get(client, "/api/library")["books"]] == ["hb1"]
    put(client, book="hb2", chapter=1)
    assert [b["id"] for b in get(client, "/api/library")["books"]] == ["hb1", "hb2"]
    assert len(get(client, "/api/library", spoilers=1)["books"]) == 3


# --- unknown order and the read toggle -----------------------------------------------------------

def test_a_book_of_unknown_order_is_visible_only_when_marked_read(client):
    put(client, book="hb2", chapter=1)
    assert get(client, "/api/entity", id="w_pip")["error"]["code"] == "not_found"
    put(client, book="hb2", chapter=1, read=["hb3"])
    assert get(client, "/api/entity", id="w_pip")["name"] == "Pip Marsh"
    # but the later book of the series is never visible from the first, marked or not
    put(client, book="hb1", chapter=3, read=["hb2", "hb3"])
    assert "h_mira" in {n["id"] for n in get(client, "/api/overview")["nodes"]}
    assert not any(e["book_id"] == "hb2" for e in get(client, "/api/entity", id="h_mira")["edges_out"])


def test_a_disabled_position_changes_nothing(client):
    before = {p: get(client, p) for p in ("/api/overview", "/api/library", "/api/entity/h_ren/profile")}
    put(client, enabled=False, chapter=1)
    assert {p: get(client, p) for p in before} == before


def test_worlds_do_not_share_a_position(client):
    put(client, chapter=1)
    other = get(client, "/api/overview", world=OTHER)
    assert {n["id"] for n in other["nodes"]} == {"o_zed", "o_yan"}
    both = get(client, "/api/overview", world="all")
    ids = {n["id"] for n in both["nodes"]}
    assert "o_zed" in ids and "h_dorn" not in ids and "w_pip" not in ids


# --- the plain tool functions ---------------------------------------------------------------------------

def test_the_tool_functions_take_spoilers(config, client):
    put(client, chapter=2)
    assert tools.entity_by_id(config, "h_dorn", world=HOLLOW)["error"]["code"] == "not_found"
    assert tools.entity_by_id(config, "h_dorn", world=HOLLOW, spoilers=True)["id"] == "h_dorn"
    assert explore.search(config, "dorn", world=HOLLOW)["results"] == []
    assert explore.search(config, "dorn", world=HOLLOW, spoilers=True)["results"]
    assert {n["id"] for n in tools.overview(config, world=HOLLOW, spoilers=True)["nodes"]} >= {"h_dorn"}


# --- /api/ask --------------------------------------------------------------------------------------

def test_ask_applies_the_position_unless_spoilers_is_asked_for(client, monkeypatch):
    seen: list[dict] = []

    async def fake_search(config, question, on_event=None, **kw):
        seen.append(kw)
        return {"answer": "ok"}

    monkeypatch.setattr(tools, "lore_search", fake_search)
    body = {"question": "Who is Old Tam?", "world": HOLLOW}
    assert client.post("/api/ask", json=body).status_code == 200
    assert client.post("/api/ask?spoilers=1", json=body).status_code == 200
    assert seen == [{"world": HOLLOW}, {"world": HOLLOW, "spoilers": True}]
