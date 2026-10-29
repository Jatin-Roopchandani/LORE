"""The web API with worlds: /api/worlds, and `world` on ask and the graph reads."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lore.web.app import create_app
from tests.worlds_fixtures import ALPHA, BETA, build_library


@pytest.fixture
def config(tmp_path):
    return build_library(tmp_path)


@pytest.fixture
def client(config):
    return TestClient(create_app(config), base_url="http://127.0.0.1:8766")


def test_worlds_lists_each_world_with_its_books(client) -> None:
    assert client.get("/api/worlds").json() == [
        {"id": 1, "name": ALPHA, "books": [
            {"id": "ba1", "title": "Alpha One", "series": ALPHA, "series_index": 1.0},
            {"id": "ba2", "title": "Alpha Two", "series": ALPHA, "series_index": 2.0}]},
        {"id": 2, "name": BETA, "books": [
            {"id": "bb1", "title": "Beta Solo", "series": None, "series_index": None}]}]


def test_overview_of_one_world_has_only_its_entities_and_no_world_field(client) -> None:
    body = client.get("/api/overview", params={"world": ALPHA}).json()
    assert {n["id"] for n in body["nodes"]} == {"a_zed", "a_yan"}
    assert all("world" not in n for n in body["nodes"])
    assert {(l["subject_id"], l["object_id"]) for l in body["edges"]} == {("a_zed", "a_yan")}


def test_overview_of_all_worlds_has_a_cluster_per_world(client) -> None:
    for params in ({}, {"world": "all"}):
        body = client.get("/api/overview", params=params).json()
        by_id = {n["id"]: n for n in body["nodes"]}
        # Same-named entities stay two nodes, each marked with its world.
        assert sorted(by_id) == ["a_yan", "a_zed", "b_yan", "b_zed"]
        assert by_id["a_zed"]["name"] == by_id["b_zed"]["name"] == "Zed Quill"
        assert by_id["a_zed"]["world"] == {"id": 1, "name": ALPHA}
        assert by_id["b_zed"]["world"] == {"id": 2, "name": BETA}
        assert all(l["subject_id"][0] == l["object_id"][0] for l in body["edges"])
        assert set(body) >= {"nodes", "edges", "truncated", "depth", "note"}


def test_overview_limit_is_shared_between_worlds(client) -> None:
    body = client.get("/api/overview", params={"limit": 2}).json()
    assert len(body["nodes"]) == 2 and body["truncated"] is True
    assert {n["world"]["name"] for n in body["nodes"]} == {ALPHA, BETA}


def test_overview_unknown_world_is_a_tool_error(client) -> None:
    r = client.get("/api/overview", params={"world": "Gamma"})
    assert r.status_code == 200 and r.json()["error"]["code"] == "not_found"


def test_overview_never_joins_worlds_even_over_a_stray_edge(tmp_path) -> None:
    client = TestClient(create_app(build_library(tmp_path, cross_edge=True)),
                        base_url="http://127.0.0.1:8766")
    body = client.get("/api/overview").json()
    assert all(l["subject_id"][0] == l["object_id"][0] for l in body["edges"])
    assert body["nodes"] and all(n["world"] for n in body["nodes"])


def test_subgraph_and_entity_take_the_world(client) -> None:
    sub = client.get("/api/subgraph", params={"id": "b_zed", "world": BETA}).json()
    assert {n["id"] for n in sub["nodes"]} == {"b_zed", "b_yan"}
    card = client.get("/api/entity", params={"id": "b_zed", "world": BETA}).json()
    assert card["id"] == "b_zed" and "world" not in card
    wrong = client.get("/api/entity", params={"id": "b_zed", "world": ALPHA}).json()
    assert wrong["error"]["code"] == "not_found"
    allw = client.get("/api/subgraph", params={"id": "b_zed", "depth": 2}).json()
    assert {n["world"]["name"] for n in allw["nodes"]} == {BETA}
    by_name = client.get("/api/entity", params={"name": "Zed Quill", "world": ALPHA}).json()
    assert by_name["id"] == "a_zed"


def test_entities_by_ids_tag_worlds_in_all_mode_and_filter_in_one(client) -> None:
    allw = client.get("/api/entities", params={"ids": "a_zed,b_zed"}).json()["entities"]
    assert {e["id"]: e["world"]["name"] for e in allw} == {"a_zed": ALPHA, "b_zed": BETA}
    one = client.get("/api/entities", params={"ids": "a_zed,b_zed", "world": BETA}).json()
    assert [e["id"] for e in one["entities"]] == ["b_zed"]
    assert "world" not in one["entities"][0]


def test_library_takes_the_world(client) -> None:
    books = client.get("/api/library", params={"world": ALPHA}).json()["books"]
    assert {b["id"] for b in books} == {"ba1", "ba2"}


def test_ask_passes_the_world_to_the_search(client, monkeypatch) -> None:
    seen = {}

    async def fake_search(config, question, on_event=None, world=None):
        seen["world"] = world
        return {"answer": "x"}

    monkeypatch.setattr("lore.mcp.tools.lore_search", fake_search)
    for body, expected in (({"question": "q", "world": ALPHA}, ALPHA),
                           ({"question": "q", "world": 2}, 2),
                           ({"question": "q"}, None)):
        r = client.post("/api/ask", json=body)
        assert r.status_code == 200 and "event: answer" in r.text
        assert seen["world"] == expected
