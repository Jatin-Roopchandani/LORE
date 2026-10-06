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
    for name in ("vector_search", "lore_search"):
        assert "Ollama" in tools[name] and "upstream_unavailable" in tools[name]


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


def _is_protocol_frame(line: str) -> bool:
    try:
        return json.loads(line).get("jsonrpc") == "2.0"
    except (ValueError, AttributeError):
        return False


def test_stdio_serves_tools_and_prints_only_protocol_frames(tmp_path):
    """stdin stays open until the tools/list reply has arrived.

    The old version wrote all frames with communicate(), which closes stdin
    at once: the server read EOF and began shutting down, and when shutdown
    beat the tools/list handler no reply was written (StopIteration, ~2 runs
    in 15). That was the test racing its own teardown, not a server bug, so
    the fix is ordering -- never a sleep or a retry, which would only hide it.
    """
    import queue
    import threading
    import time

    env = {**os.environ, "LORE_DATA_DIR": str(tmp_path / "d")}
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    # stderr to a file, not a pipe: the server logs there, and an undrained
    # pipe would fill and block it.
    stderr_path = tmp_path / "stderr.txt"
    lines: queue.Queue[str | None] = queue.Queue()
    with open(stderr_path, "w") as stderr:
        proc = subprocess.Popen(
            [str(Path(sys.executable).parent / "lore"), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            text=True, env=env,
        )

    def read_stdout() -> None:
        # A thread, so the main loop can wait on the queue with a deadline;
        # a bare readline() on the pipe could block forever.
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)  # EOF

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    received: list[str] = []
    listing = None
    try:
        for frame in frames:
            proc.stdin.write(json.dumps(frame) + "\n")
            proc.stdin.flush()
        deadline = time.monotonic() + 60
        while listing is None:
            remaining = deadline - time.monotonic()
            assert remaining > 0, (
                f"no tools/list reply within 60s; stdout so far: {received};"
                f" stderr: {stderr_path.read_text()}")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            assert line is not None, (
                f"server exited before replying; stdout: {received};"
                f" stderr: {stderr_path.read_text()}")
            if not line.strip():
                continue
            received.append(line)
            assert _is_protocol_frame(line), f"non-protocol stdout: {line!r}"
            message = json.loads(line)
            if message.get("id") == 2:
                listing = message
        # Only now let the server see EOF, and make sure it exits.
        proc.stdin.close()
        proc.wait(timeout=30)
        reader.join(timeout=30)
        # Anything written after the reply must be protocol too: a stray
        # print during shutdown would corrupt a real client's stream.
        while True:
            line = lines.get_nowait() if not lines.empty() else None
            if line is None:
                break
            if line.strip():
                received.append(line)
                assert _is_protocol_frame(line), f"non-protocol stdout: {line!r}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert received and all(_is_protocol_frame(line) for line in received)
    assert {t["name"] for t in listing["result"]["tools"]} == EXPECTED


# --- loopback only ------------------------------------------------------------------
#
# The HTTP server has no authentication, so it must never bind beyond loopback.
# Verified before the fix: with --host 0.0.0.0 a LAN request carrying
# `Host: 127.0.0.1:8765` was accepted (200), exposing cypher_query and the
# user's Gemini budget.

_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "t", "version": "0"}}}
_HEADERS = {"Accept": "application/json, text/event-stream",
            "Content-Type": "application/json"}


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com",
                                  "", "[::1]"])
def test_a_non_loopback_host_is_refused(served_config, host):
    from lore.mcp.server import NonLoopbackHost, build_http_app, run_http
    with pytest.raises(NonLoopbackHost, match="loopback"):
        build_http_app(served_config, host, 8765)
    with pytest.raises(NonLoopbackHost, match="no authentication"):
        run_http(served_config, host, 8765)  # refused before it binds


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost",
                                  "LocalHost", "::1"])
def test_loopback_hosts_are_accepted(served_config, host):
    from lore.mcp.server import build_http_app
    assert build_http_app(served_config, host, 8765) is not None


def test_the_ipv6_allowed_host_is_bracketed():
    # The real Host header for ::1 is "[::1]:8765"; "::1:8765" matched nothing,
    # so every request got 421.
    from lore.mcp.server import _security
    assert "[::1]:8765" in _security("::1", 8765).allowed_hosts
    assert "::1:8765" not in _security("::1", 8765).allowed_hosts


@pytest.mark.parametrize("host,good", [("127.0.0.1", "127.0.0.1:8765"),
                                       ("::1", "[::1]:8765")])
def test_a_foreign_host_header_gets_421_and_loopback_does_not(
    served_config, host, good
):
    from starlette.testclient import TestClient
    from lore.mcp.server import build_http_app
    app = build_http_app(served_config, host, 8765)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        foreign = client.post("/mcp", json=_INIT,
                              headers={**_HEADERS, "Host": "evil.example:8765"})
        local = client.post("/mcp", json=_INIT, headers={**_HEADERS, "Host": good})
    assert foreign.status_code == 421
    assert local.status_code != 421
    assert local.status_code == 200


def test_the_cli_refuses_a_non_loopback_host(tmp_path, monkeypatch):
    from unittest.mock import patch
    from typer.testing import CliRunner
    from lore.cli import app
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    with patch("lore.mcp.server.MCPServer.run") as run:
        result = CliRunner().invoke(app, ["mcp", "--http", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "loopback" in result.output
    run.assert_not_called()
