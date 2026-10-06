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


class StoresUnavailable(StoresMissing):
    """The graph exists but could not be opened.

    Subclasses StoresMissing so the tool decorator's isinstance check maps it
    to the `no_graph` envelope with no change. That code is imprecise: the
    honest state is "graph busy or broken", but the spec fixes the code set
    and has no busy code, so the message carries the truth.
    """


@dataclass(frozen=True)
class Stores:
    conn: sqlite3.Connection
    # None when the caller's scope left that store out (see open_stores).
    kuzu: KuzuStore | None
    lance: LanceStore | None


_NOT_BUILT = ("no graph yet -- run `lore ingest`, `lore process`, then"
              " `lore graph load` before querying")


@contextmanager
def open_graph(config: LoreConfig) -> Iterator[KuzuStore]:
    """Open the Kuzu graph read-only, with the query timeout, and close it.

    Raises StoresMissing if there is no graph, StoresUnavailable if it
    exists but cannot be opened. On its own so `library` can try the graph
    best-effort after reading SQLite.
    """
    graph_path = config.kuzu_dir / "graph"
    if not graph_path.exists():
        raise StoresMissing(_NOT_BUILT)
    try:
        kuzu = KuzuStore(graph_path, read_only=True)
    except RuntimeError as exc:
        # Narrow on purpose: only the open. While `lore graph load`
        # holds the write lock this is what Kuzu raises, which is the
        # very scenario this module exists for.
        raise StoresUnavailable(
            "the graph exists but could not be opened \u2014 a rebuild"
            " (`lore graph load`) may be running, or the graph may be"
            f" corrupt: {exc}"
        ) from exc
    try:
        kuzu.set_query_timeout(TIMEOUT_MS)
        yield kuzu
    finally:
        kuzu.close()


@contextmanager
def open_stores(config: LoreConfig, *, kuzu: bool = True,
                lance: bool = True) -> Iterator[Stores]:
    """Open SQLite plus the requested stores, yield them, and close them.

    SQLite is always opened, READ-ONLY (`file:...?mode=ro`, as `lore ask`
    opens it): a write through `stores.conn` raises "attempt to write a
    readonly database". It is deliberately not `open_db`, which runs
    migrations; a query must not migrate the user's library as a side
    effect. The Gemini ledger gets its migration through its own recorder
    path, and lore_search's budget check uses its own `open_db` connection.
    Kuzu is opened read-only. LanceDB is opened as LanceStore opens it,
    which has no read-only mode.

    The scope: only the requested stores must exist and are opened; the
    others are None. `ingest_status` asks for SQLite alone and
    `vector_search` for SQLite and LanceDB, so neither refuses while the
    graph is absent or locked by a rebuild -- which is exactly when
    `ingest_status` is wanted. The default is all three.

    The existence check is the one `lore ask` makes, and it runs before
    anything opens: `sqlite3.connect` on a missing path, even read-only,
    must never be what reports "not built".
    """
    if not (config.sqlite_path.is_file()
            and (not kuzu or (config.kuzu_dir / "graph").exists())
            and (not lance or config.lancedb_dir.is_dir())):
        raise StoresMissing(_NOT_BUILT)

    # Nested try/finally, opened in order: a failure partway through opening
    # (Kuzu fine, LanceDB raises) or inside the caller's body still releases
    # every handle that was acquired, and one close() raising cannot skip
    # the next.
    conn = sqlite3.connect(f"file:{config.sqlite_path}?mode=ro", uri=True)
    try:
        if not kuzu:
            yield Stores(conn=conn, kuzu=None,
                         lance=LanceStore(config.lancedb_dir) if lance else None)
            return
        with open_graph(config) as graph:
            yield Stores(conn=conn, kuzu=graph,
                         lance=LanceStore(config.lancedb_dir) if lance else None)
    finally:
        conn.close()
