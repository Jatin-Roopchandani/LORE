"""The seven query tools, over stdio and Streamable HTTP.

The server class is `MCPServer` (mcp 2.x); `FastMCP` is the 1.x name. SPEC
originally said SSE, the HTTP transport the MCP spec superseded; this serves
Streamable HTTP instead.

In stdio mode stdout is the protocol channel: nothing here may print to it.

Operational note: an engine crash takes the whole process down. Kuzu 0.11.3
segfaults on `CALL read_csv_serial('<path>')`; the cypher guard rejects that
name and allowlists only show_tables, table_info and db_version, so the known
trigger cannot arrive, but a long-lived `--http` server should still run under
a supervisor that restarts it. There is deliberately no per-query isolation.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from lore.config import LoreConfig
from lore.mcp import tools


def build_server(config: LoreConfig) -> MCPServer:
    mcp = MCPServer("lore")

    @mcp.tool()
    async def lore_search(query: str, world: str | None = None,
                          depth: Literal["auto", "deep", "focused"] = "auto") -> dict[str, Any]:
        """Answer a question from the knowledge graph, with quoted citations.

        This is the only tool that spends Gemini budget (the other six never
        do); it returns a budget_exhausted error once the daily cap is spent.
        Use vector_search or entity when you want a free lookup. Needs the
        local Ollama server running to embed the question; if it is not,
        this returns upstream_unavailable.

        `world` (a world's id or name) answers from that world's books only.
        Without it, every world is searched, each on its own, so two
        same-named characters from different worlds stay different people;
        the answer names the book when facts come from more than one.

        `depth` is "auto" (the default: LORE judges whether the question
        needs a full, sectioned answer), "deep" or "focused".
        """
        return await tools.lore_search(config, query, world=world, depth=depth)

    @mcp.tool()
    async def vector_search(query: str, book_id: str | None = None,
                            limit: int = 10, world: str | None = None) -> dict[str, Any]:
        """Find passages nearest a query by meaning, optionally within one book
        or one `world` (an id or name).

        Runs locally and spends no Gemini budget, so it keeps working after
        the daily Gemini cap is reached. `limit` is clamped to 1-50. Needs
        the local Ollama server running to embed the query; if it is not,
        this returns upstream_unavailable.
        """
        return await tools.vector_search(config, query, book_id, limit, world=world)

    @mcp.tool()
    def cypher_query(query: str) -> dict[str, Any]:
        """Run a read-only Cypher query against the graph, capped at 200 rows.

        Any query containing COPY, LOAD, EXPORT, IMPORT, ATTACH, INSTALL,
        CREATE, MERGE, SET, DELETE, DROP or ALTER as a whole word is rejected,
        even inside a string literal. CALL is rejected the same way, except
        for the three allowlisted procedures show_tables, table_info and
        db_version. A question about a book titled "The Call of the Wild"
        must go through entity or subgraph instead.
        """
        return tools.cypher_query(config, query)

    @mcp.tool()
    def entity(name: str, world: str | None = None) -> dict[str, Any]:
        """The canonical card for one entity: aliases, books and edges.

        Resolves an alias to its canonical entity. An unknown name returns a
        not_found error; so does an ambiguous one, whose message lists the
        candidates to choose from. At most 50 edges are returned each way;
        edges_out_total and edges_in_total give the real counts, and
        truncated says the card is partial -- use subgraph for more.

        With `world` (an id or name) only that world's entity and edges are
        used. A name that two worlds share is ambiguous without it, and the
        error lists both with their worlds.
        """
        return tools.entity(config, name, world=world)

    @mcp.tool()
    def subgraph(entity: str, depth: int = 1, world: str | None = None) -> dict[str, Any]:
        """A bounded subgraph around one entity, as nodes and links.

        Each link is one subject->object pair: how many passages back it,
        which predicates, and one quoted citation. Links are ordered
        strongest first and capped at 100, so a large hub keeps its main
        relationships; `depth` is clamped to 1-3. The result says when it
        was truncated. The walk stays inside the entity's world; `world` (an
        id or name) picks the entity's world when its name is shared.
        """
        return tools.subgraph(config, entity, depth, world=world)

    @mcp.tool()
    def library(world: str | None = None) -> dict[str, Any]:
        """Every book in the library, with its world, status and entity
        counts; `world` (an id or name) lists one world's books."""
        return tools.library(config, world=world)

    @mcp.tool()
    def ingest_status(book_id: str) -> dict[str, Any]:
        """Pipeline state for one book: its jobs and any dead letters."""
        return tools.ingest_status(config, book_id)

    return mcp


def run_stdio(config: LoreConfig) -> None:
    build_server(config).run(transport="stdio")


class NonLoopbackHost(ValueError):
    """`--host` named an address beyond loopback."""


def _check_loopback(host: str) -> None:
    """Refuse any bind address that is not loopback.

    The server has no authentication, so binding beyond loopback hands
    `cypher_query` and the user's Gemini budget (through `lore_search`) to
    anyone on the network. The DNS-rebinding check does not stop that: a
    LAN client sets its own Host header. Verified: with host 0.0.0.0, a
    request carrying `Host: 127.0.0.1:8765` was accepted (200). Checked
    here, on the path both run_http and build_http_app take, so no caller
    -- the CLI included -- can skip it.
    """
    if host.lower() == "localhost":
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:  # a hostname, a bracketed IPv6, or ""
        loopback = False
    if not loopback:
        raise NonLoopbackHost(
            f"refusing to bind {host!r}: LORE's MCP server has no"
            " authentication yet, so it only binds loopback (127.0.0.0/8,"
            " localhost, ::1)"
        )


def _security(host: str, port: int) -> TransportSecuritySettings:
    # Loopback bind plus the SDK's DNS-rebinding protection: without an
    # allowed-hosts list a page in the user's browser can reach a localhost port.
    _check_loopback(host)
    # An IPv6 literal is bracketed in a Host header (`[::1]:8765`, RFC 3986);
    # the bare `::1:8765` matched no real request, so every one got 421.
    shown = f"[{host}]" if ":" in host else host
    return TransportSecuritySettings(
        allowed_hosts=[f"{shown}:{port}", f"127.0.0.1:{port}", f"localhost:{port}"],
    )


def build_http_app(config: LoreConfig, host: str = "127.0.0.1", port: int = 8765):
    """The Streamable HTTP ASGI app, without starting it."""
    return build_server(config).streamable_http_app(
        host=host, transport_security=_security(host, port),
    )


def run_http(config: LoreConfig, host: str = "127.0.0.1", port: int = 8765) -> None:
    build_server(config).run(
        transport="streamable-http", host=host, port=port,
        transport_security=_security(host, port),
    )
