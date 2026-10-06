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
    for proc in ("show_tables", "table_info", "db_version"):
        assert proc in tools["cypher_query"]
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


async def test_structured_content_matches_the_payload(served_config):
    async with Client(build_server(served_config)) as client:
        ok = await client.call_tool("library", {})
        err = await client.call_tool("entity", {"name": "Nobody At All"})
    assert ok.structured_content == json.loads(ok.content[0].text)
    assert "books" in ok.structured_content
    assert err.structured_content == json.loads(err.content[0].text)
    assert err.structured_content["error"]["code"] == "not_found"


async def test_subgraph_takes_entity_as_its_parameter(served_config):
    tools = {t.name: t for t in await build_server(served_config).list_tools()}
    assert "entity" in tools["subgraph"].input_schema["properties"]
    async with Client(build_server(served_config)) as client:
        result = await client.call_tool("subgraph", {"entity": "Holmes"})
    assert not result.is_error
    assert "error" not in result.structured_content


def test_stdio_serves_tools_and_prints_only_protocol_frames(tmp_path):
    env = {**os.environ, "LORE_DATA_DIR": str(tmp_path / "d")}
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = "".join(json.dumps(f) + "\n" for f in frames)
    proc = subprocess.Popen(
        [str(Path(sys.executable).parent / "lore"), "mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    try:
        out, _ = proc.communicate(stdin, timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    messages = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert messages and all(m.get("jsonrpc") == "2.0" for m in messages)
    listing = next(m for m in messages if m.get("id") == 2)
    assert {t["name"] for t in listing["result"]["tools"]} == EXPECTED
