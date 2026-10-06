"""The MCP server: registration, schemas, in-process round trips, stdio silence."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client

from lore.mcp.server import build_server
from tests.mcp_fixtures import build_store_triple

EXPECTED = {
    "lore_search", "vector_search", "cypher_query",
    "entity", "subgraph", "library", "ingest_status",
}


@pytest.fixture
def served_config(tmp_path: Path):
    return build_store_triple(
        tmp_path,
        books=[("book-1", "A Study in Scarlet")],
        entities=[("e1", "Holmes", "Character", ["book-1"], [])],
    )


def _payload(result) -> dict:
    """The dict a tool returned, however the SDK delivered it."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    return json.loads(result.content[0].text)


async def test_all_seven_tools_are_registered(served_config):
    tools = await build_server(served_config).list_tools()
    assert {t.name for t in tools} == EXPECTED


async def test_every_tool_has_a_description(served_config):
    for tool in await build_server(served_config).list_tools():
        assert tool.description and tool.description.strip(), tool.name


async def test_descriptions_say_what_matters(served_config):
    tools = {t.name: t.description
             for t in await build_server(served_config).list_tools()}
    assert "Gemini" in tools["lore_search"]
    assert "whole word" in tools["cypher_query"].lower()
    assert "200" in tools["cypher_query"]
    assert "Call of the Wild" in tools["cypher_query"]
    assert "1" in tools["subgraph"] and "3" in tools["subgraph"]
    assert "Gemini" in tools["vector_search"]


async def test_no_tool_exposes_internal_parameters(served_config):
    for tool in await build_server(served_config).list_tools():
        props = set((tool.input_schema or {}).get("properties", {}))
        assert not props & {"models", "embed", "config"}, tool.name


async def test_library_round_trips_in_process(served_config):
    async with Client(build_server(served_config)) as client:
        result = await client.call_tool("library", {})
    assert not result.is_error
    assert "A Study in Scarlet" in json.dumps(_payload(result))


async def test_an_error_envelope_reaches_the_client_not_an_exception(served_config):
    async with Client(build_server(served_config)) as client:
        result = await client.call_tool("entity", {"name": "Nobody At All"})
    assert not result.is_error
    assert _payload(result)["error"]["code"] == "not_found"


def test_the_http_app_builds_with_security_settings(served_config):
    from lore.mcp.server import build_http_app
    app = build_http_app(served_config, "127.0.0.1", 8765)
    assert app is not None


def test_stdio_prints_nothing_but_protocol_frames(tmp_path):
    env = {**os.environ, "LORE_DATA_DIR": str(tmp_path / "d")}
    proc = subprocess.run(
        [str(Path(sys.executable).parent / "lore"), "mcp"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=60, env=env,
    )
    for line in proc.stdout.splitlines():
        assert json.loads(line).get("jsonrpc") == "2.0", line
