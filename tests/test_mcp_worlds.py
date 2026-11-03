"""The MCP tools take an optional `world`: lookups stay inside it, and with
several worlds and none given they are told apart, never merged.

Real stores over the synthetic two-world library (tests/worlds_fixtures.py).
"""
from __future__ import annotations

import pytest

from lore.mcp import tools
from tests.worlds_fixtures import ALPHA, BETA, build_library, scripted_models


@pytest.fixture
def config(tmp_path):
    return build_library(tmp_path)


def ids(result):
    return {n["id"] for n in result["nodes"]}


# --- lore_search ------------------------------------------------------------------

async def test_lore_search_in_one_world_returns_only_its_passages(config) -> None:
    models, calls = scripted_models()
    result = await tools.lore_search(config, "Who is Zed Quill?", models=models, world=BETA)
    assert result["passages"] and {p["book_id"] for p in result["passages"]} == {"bb1"}
    assert {s["id"][0] for s in result["seeds"]} == {"b"}
    assert "ALPHAMARK" not in calls["synthesize"][0]


async def test_lore_search_without_a_world_searches_every_world(config) -> None:
    models, calls = scripted_models()
    result = await tools.lore_search(config, "Who is Zed Quill?", models=models)
    assert {"a_zed", "b_zed"} <= {s["id"] for s in result["seeds"]}
    assert "ALPHAMARK" in calls["synthesize"][0] and "BETAMARK" in calls["synthesize"][0]


async def test_lore_search_unknown_world_is_not_found_and_spends_nothing(config) -> None:
    models, calls = scripted_models()
    result = await tools.lore_search(config, "Who is Zed Quill?", models=models, world="Gamma")
    assert result["error"]["code"] == "not_found" and "Gamma" in result["error"]["message"]
    assert not calls["embed"] and not calls["select"] and not calls["synthesize"]


# --- entity / subgraph --------------------------------------------------------------

def test_entity_shared_name_is_ambiguous_across_worlds_and_says_where(config) -> None:
    result = tools.entity(config, "Zed Quill")
    message = result["error"]["message"]
    assert result["error"]["code"] == "not_found"
    assert "a_zed" in message and "b_zed" in message
    assert ALPHA in message and BETA in message and "world" in message


@pytest.mark.parametrize("world, entity_id, books", [
    (ALPHA, "a_zed", {"ba1", "ba2"}), (BETA, "b_zed", {"bb1"}), ("1", "a_zed", {"ba1", "ba2"})])
def test_entity_in_a_world_is_that_worlds(config, world, entity_id, books) -> None:
    card = tools.entity(config, "Zed Quill", world=world)
    assert card["id"] == entity_id
    assert {e["book_id"] for e in card["edges_out"] + card["edges_in"]} <= books
    assert "world" not in card  # only all-worlds mode stamps a world


def test_entity_unknown_world_and_wrong_world(config) -> None:
    assert tools.entity(config, "Zed Quill", world="Gamma")["error"]["code"] == "not_found"
    only_beta = tools.entity_by_id(config, "a_zed", world=BETA)
    assert only_beta["error"]["code"] == "not_found" and BETA in only_beta["error"]["message"]


def test_entity_by_id_in_all_worlds_carries_its_world(config) -> None:
    card = tools.entity_by_id(config, "b_zed")
    assert card["world"] == {"id": 2, "name": BETA}
    assert {e["book_id"] for e in card["edges_out"] + card["edges_in"]} == {"bb1"}


def test_subgraph_stays_in_the_world(config) -> None:
    sub = tools.subgraph(config, "Zed Quill", depth=2, world=ALPHA)
    assert ids(sub) == {"a_zed", "a_yan"}
    assert all(n["books"] and set(n["books"]) <= {"ba1", "ba2"} for n in sub["nodes"])
    assert all("world" not in n for n in sub["nodes"])


def test_subgraph_by_id_in_all_worlds_stamps_nodes_and_never_crosses(tmp_path) -> None:
    config = build_library(tmp_path, cross_edge=True)  # a corrupt edge a_zed -> b_yan
    sub = tools.subgraph_by_id(config, "a_zed", depth=3)
    assert ids(sub) == {"a_zed", "a_yan"}
    assert {n["world"]["name"] for n in sub["nodes"]} == {ALPHA}
    assert {(l["subject_id"][0], l["object_id"][0]) for l in sub["edges"]} == {("a", "a")}


# --- library / vector_search -----------------------------------------------------

def test_library_lists_each_books_world_and_filters(config) -> None:
    every = {b["id"]: b["world"] for b in tools.library(config)["books"]}
    assert every == {"ba1": ALPHA, "ba2": ALPHA, "bb1": BETA}
    only = tools.library(config, world=BETA)["books"]
    assert [b["id"] for b in only] == ["bb1"]
    assert tools.library(config, world="Gamma")["error"]["code"] == "not_found"


async def test_vector_search_stays_in_the_world(config) -> None:
    async def embed(texts):
        from tests.worlds_fixtures import QUESTION_VECTOR
        return [QUESTION_VECTOR for _ in texts]

    hits = await tools.vector_search(config, "the traveller", limit=10, embed=embed,
                                     world=ALPHA)
    assert {h["book_id"] for h in hits["chunks"]} == {"ba1", "ba2"}
    both = await tools.vector_search(config, "the traveller", limit=10, embed=embed)
    assert {h["book_id"] for h in both["chunks"]} == {"ba1", "ba2", "bb1"}
    wrong = await tools.vector_search(config, "the traveller", book_id="bb1", embed=embed,
                                      world=ALPHA)
    assert wrong["error"]["code"] == "not_found"
