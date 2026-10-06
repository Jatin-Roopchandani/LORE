"""The seven read-only tools, over stdio and Streamable HTTP.

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

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from lore.config import LoreConfig
from lore.mcp import tools


def build_server(config: LoreConfig) -> MCPServer:
    mcp = MCPServer("lore")

    @mcp.tool()
    async def lore_search(query: str) -> dict:
        """Answer a question from the knowledge graph, with quoted citations.

        This is the only tool that spends Gemini budget (the other six never
        do); it returns a budget_exhausted error once the daily cap is spent.
        Use vector_search or entity when you want a free lookup.
        """
        return await tools.lore_search(config, query)

    @mcp.tool()
    async def vector_search(query: str, book_id: str | None = None,
                            limit: int = 10) -> dict:
        """Find passages nearest a query by meaning, optionally within one book.

        Runs locally and spends no Gemini budget, so it keeps working after
        the daily Gemini cap is reached. `limit` is clamped to 1-50.
        """
        return await tools.vector_search(config, query, book_id, limit)

    @mcp.tool()
    def cypher_query(query: str) -> dict:
        """Run a read-only Cypher query against the graph, capped at 200 rows.

        Any query containing COPY, LOAD, EXPORT, IMPORT, ATTACH, INSTALL,
        CREATE, MERGE, SET, DELETE, DROP, ALTER or CALL as a whole word is
        rejected, even inside a string literal. A question about a book titled
        "The Call of the Wild" must go through entity or subgraph instead.
        """
        return tools.cypher_query(config, query)

    @mcp.tool()
    def entity(name: str) -> dict:
        """The canonical card for one entity: aliases, books and edges.

        Resolves an alias to its canonical entity; an ambiguous name returns
        the candidates, and an unknown name returns a not_found error.
        """
        return tools.entity(config, name)

    @mcp.tool()
    def subgraph(entity_name: str, depth: int = 1) -> dict:
        """A bounded subgraph around one entity, as nodes and edges.

        `depth` is clamped to 1-3, and node and edge counts are capped; the
        result says when it was truncated.
        """
        return tools.subgraph(config, entity_name, depth)

    @mcp.tool()
    def library() -> dict:
        """Every book in the library, with its status and entity counts."""
        return tools.library(config)

    @mcp.tool()
    def ingest_status(book_id: str) -> dict:
        """Pipeline state for one book: its jobs and any dead letters."""
        return tools.ingest_status(config, book_id)

    return mcp


def run_stdio(config: LoreConfig) -> None:
    build_server(config).run(transport="stdio")


def _security(host: str, port: int) -> TransportSecuritySettings:
    # Loopback bind plus the SDK's DNS-rebinding protection: without an
    # allowed-hosts list a page in the user's browser can reach a localhost port.
    return TransportSecuritySettings(
        allowed_hosts=[f"{host}:{port}", f"127.0.0.1:{port}", f"localhost:{port}"],
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
