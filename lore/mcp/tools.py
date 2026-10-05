"""The MCP tools, as plain functions of a LoreConfig.

Plain so they are testable without a server; a later task registers them.
No tool raises through the protocol: every outcome is a dict a client can
read, either the tool's shape or an `error(...)` envelope.
"""
from __future__ import annotations

import functools

from lore.config import LoreConfig
from lore.ingest.registry import list_books
from lore.mcp.shapes import BookOut, IngestStatus, JobOut, error
from lore.mcp.stores import StoresMissing, open_stores


def _guarded(fn):
    """Turn StoresMissing into the no_graph envelope, for every tool.

    One decorator, so no tool's handling can drift from the others'.
    StoresUnavailable subclasses StoresMissing, so a graph that exists but
    is locked or corrupt lands here too, carrying its own honest message.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StoresMissing as exc:
            return error("no_graph", str(exc))
    return wrapper


@_guarded
def library(config: LoreConfig) -> dict:
    with open_stores(config) as stores:
        books = list_books(stores.conn)
        # One query, not one per book: the entity count per book id. Verified
        # against Kuzu: UNWIND skips entities whose `books` is NULL, and a
        # book with no entities simply has no row, hence the .get(..., 0).
        counts: dict[str, int] = {}
        for row in stores.kuzu.execute(
            "MATCH (e:Entity) UNWIND e.books AS book RETURN book, count(e)"
        ):
            counts[row[0]] = int(row[1])
        return {"books": [
            BookOut(id=b["id"], title=b["title"], author=b["author"],
                    status=b["status"], chunk_count=b["chunk_count"],
                    entity_count=counts.get(b["id"], 0)).model_dump()
            for b in books
        ]}


@_guarded
def ingest_status(config: LoreConfig, book_id: str) -> dict:
    with open_stores(config) as stores:
        books = {b["id"]: b for b in list_books(stores.conn)}
        if book_id not in books:
            return error("not_found", f"no book with id {book_id!r}")
        jobs = [
            JobOut(kind=r[0], state=r[1], detail=r[2], error=r[3],
                   updated_at=r[4])
            for r in stores.conn.execute(
                "SELECT kind, state, detail, error, updated_at FROM jobs"
                " WHERE book_id = ? ORDER BY updated_at, id",
                (book_id,),
            )
        ]
        # A dead letter is a job in state "failed": orchestrator/run.py only
        # ever sets queued, dispatched, completed, failed -- there is no
        # "dead" state, so filtering on it would always be empty and hide
        # every failed extraction. Do not "correct" this to "dead".
        dead = [j for j in jobs if j.state == "failed"]
        return IngestStatus(book_id=book_id, title=books[book_id]["title"],
                            status=books[book_id]["status"], jobs=jobs,
                            dead_letters=dead).model_dump()
