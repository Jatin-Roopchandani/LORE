"""One request, one set of store handles.

Do NOT cache these handles across requests. Measured, not assumed: a Kuzu
handle held open makes `lore graph load` (the writer) fail outright with
"IO exception: Could not set lock on file", while a second READER opens
fine. Opening read-only costs 21 ms median (93 ms max over 10 runs) and
LanceDB connect 5 ms, against a lore_search that spends seconds in Gemini,
so per-request is free -- and it means rebuilding the graph while the
server runs keeps working. A cached handle would look like a harmless
optimisation and silently break that.
`test_a_writer_can_open_the_graph_between_requests` guards this.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.mcp.guards import TIMEOUT_MS


class StoresMissing(RuntimeError):
    """The library has not been built yet."""


@dataclass(frozen=True)
class Stores:
    conn: sqlite3.Connection
    kuzu: KuzuStore
    lance: LanceStore


@contextmanager
def open_stores(config: LoreConfig) -> Iterator[Stores]:
    """Open the three stores read-only, yield them, and close them.

    The same existence check `lore ask` makes: a question must not create,
    migrate, or touch the library.

    SQLite uses plain `sqlite3.connect`, not `open_db`: `open_db` runs
    migrations, and a read-only tool must not migrate the user's library as
    a side effect of a query. The Gemini ledger gets its migration through
    its own recorder path.
    """
    graph_path = config.kuzu_dir / "graph"
    if not (graph_path.exists()
            and config.sqlite_path.is_file()
            and config.lancedb_dir.is_dir()):
        raise StoresMissing(
            "no graph yet -- run `lore ingest`, `lore process`, then"
            " `lore graph load` before querying"
        )

    # Nested try/finally, opened in order: a failure partway through opening
    # (Kuzu fine, LanceDB raises) or inside the caller's body still releases
    # every handle that was acquired, and one close() raising cannot skip
    # the next.
    conn = sqlite3.connect(config.sqlite_path)
    try:
        kuzu = KuzuStore(graph_path, read_only=True)
        try:
            kuzu.set_query_timeout(TIMEOUT_MS)
            lance = LanceStore(config.lancedb_dir)
            yield Stores(conn=conn, kuzu=kuzu, lance=lance)
        finally:
            kuzu.close()
    finally:
        conn.close()
