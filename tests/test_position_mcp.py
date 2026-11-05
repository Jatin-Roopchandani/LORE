"""MCP: the reading position applies by default; `ignore_reading_position` bypasses it.

Over tests/position_fixtures.py (see test_position_explore.py for the cast).
`lore_search` is covered with scripted models in test_position_answers.py; here
the graph tools through the server, and `vector_search` with a fake embedder.
"""
from __future__ import annotations

import json

import pytest
from mcp import Client

from lore.db.sqlite import open_db
from lore.mcp import tools
from lore.mcp.server import build_server
from lore.reading_position import ReadingPosition, set_position
from tests.position_fixtures import HOLLOW, build_position_library
from tests.worlds_fixtures import QUESTION_VECTOR


@pytest.fixture
def config(tmp_path):
    config = build_position_library(tmp_path)
    conn = open_db(config.sqlite_path)
    wid = conn.execute("SELECT id FROM worlds WHERE name = ?", (HOLLOW,)).fetchone()[0]
    set_position(conn, ReadingPosition(wid, "hb1", 2, (), True))
    conn.close()
    return config


def payload(result) -> dict:
    return result.structured_content or json.loads(result.content[0].text)


async def call(config, tool, **args):
    async with Client(build_server(config)) as client:
        result = await client.call_tool(tool, {"world": HOLLOW, **args})
    assert not result.is_error
    return payload(result)


async def test_the_tools_advertise_the_bypass(config):
    tools_ = {t.name: t for t in await build_server(config).list_tools()}
    for name in ("lore_search", "vector_search", "entity", "subgraph", "library"):
        assert "ignore_reading_position" in tools_[name].input_schema["properties"], name
        assert "reading position" in tools_[name].description.lower()
    assert "ignore_reading_position" not in tools_["cypher_query"].input_schema["properties"]
    assert "does not apply a reading position" in tools_["cypher_query"].description.lower()


async def test_entity_applies_the_position_by_default(config):
    card = await call(config, "entity", name="Old Tam")
    assert card["name"] == "Old Tam" and card["edges_out_total"] == 1
    assert "Ren Ashby" not in json.dumps(card)
    assert (await call(config, "entity", name="Dorn Vale"))["error"]["code"] == "not_found"
    assert (await call(config, "entity", name="Ren Ashby"))["error"]["code"] == "not_found"


async def test_entity_with_the_bypass_sees_everything(config):
    card = await call(config, "entity", name="Ren Ashby", ignore_reading_position=True)
    assert card["name"] == "Ren Ashby" and card["edges_out_total"] == 3
    assert (await call(config, "entity", name="Dorn Vale", ignore_reading_position=True))["id"] == "h_dorn"


async def test_subgraph_applies_the_position_and_the_bypass_lifts_it(config):
    got = await call(config, "subgraph", entity="Old Tam", depth=2)
    assert {n["id"] for n in got["nodes"]} == {"h_ren", "h_mira"}
    assert "BETRAYED" not in json.dumps(got) and "SECRETFACT" not in json.dumps(got)
    full = await call(config, "subgraph", entity="Ren Ashby", depth=2, ignore_reading_position=True)
    assert {n["id"] for n in full["nodes"]} == {"h_ren", "h_mira", "h_dorn"}


async def test_library_lists_what_the_position_reaches(config):
    assert [b["id"] for b in (await call(config, "library"))["books"]] == ["hb1"]
    full = await call(config, "library", ignore_reading_position=True)
    assert len(full["books"]) == 3


async def test_vector_search_stops_at_the_position(config):
    async def embed(texts):
        return [QUESTION_VECTOR for _ in texts]

    got = await tools.vector_search(config, "the road", limit=50, embed=embed, world=HOLLOW)
    assert {c["chunk_id"] for c in got["chunks"]} == {"hb1:1:0", "hb1:2:0"}
    one = await tools.vector_search(config, "the road", book_id="hb1", limit=50, embed=embed,
                                    world=HOLLOW)
    assert {c["chunk_id"] for c in one["chunks"]} == {"hb1:1:0", "hb1:2:0"}
    unread = await tools.vector_search(config, "the road", book_id="hb2", embed=embed, world=HOLLOW)
    assert unread["error"]["code"] == "not_found"
    free = await tools.vector_search(config, "the road", limit=50, embed=embed, world=HOLLOW,
                                     spoilers=True)
    assert len(free["chunks"]) == 8
