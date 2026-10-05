"""Embedded LanceDB vector store. EMBED_DIM matches nomic-embed-text-v1.5."""
from __future__ import annotations

from pathlib import Path

import lancedb
import pyarrow as pa

EMBED_DIM = 768

_CHUNKS_SCHEMA = pa.schema([
    pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
    pa.field("chunk_id", pa.string()),
    pa.field("book_id", pa.string()),
    pa.field("chapter_seq", pa.int64()),
    pa.field("text", pa.string()),
    pa.field("entity_ids", pa.list_(pa.string())),
])

_ENTITIES_SCHEMA = pa.schema([
    pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
    pa.field("entity_id", pa.string()),
    pa.field("name", pa.string()),
    pa.field("kind", pa.string()),
])


class LanceStore:
    def __init__(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(path))
        existing = self.table_names()
        if "chunks" not in existing:
            self._db.create_table("chunks", schema=_CHUNKS_SCHEMA)
        if "entities" not in existing:
            self._db.create_table("entities", schema=_ENTITIES_SCHEMA)

    def table_names(self) -> set[str]:
        return set(self._db.list_tables().tables)

    def add_chunks(self, rows: list[dict]) -> None:
        self._db.open_table("chunks").add(rows)

    def search_chunks(self, vector: list[float], limit: int = 5) -> list[dict]:
        table = self._db.open_table("chunks")
        return table.search(vector).limit(limit).to_list()

    def search_chunks_in_book(
        self, vector: list[float], book_id: str, limit: int = 5
    ) -> list[dict]:
        """Nearest chunks of ONE book. The filter runs inside the search.

        `prefilter=True` is explicit: filtering the global top-`limit`
        afterwards returns fewer rows than asked for, or none, when the
        book's chunks are not among the library's nearest.
        """
        escaped = book_id.replace("'", "''")
        return (
            self._db.open_table("chunks")
            .search(vector)
            .where(f"book_id = '{escaped}'", prefilter=True)
            .limit(limit)
            .to_list()
        )

    def add_entities(self, rows: list[dict]) -> None:
        self._db.open_table("entities").add(rows)

    def chunk_entity_vectors(
        self, book_ids: list[str]
    ) -> list[tuple[str, list[str], list[float]]]:
        """(chunk_id, entity_ids, vector) for every chunk row in these books.

        The graph loader recomputes an entity's centroid from every chunk
        that mentions it, which for an entity shared between books means
        chunk rows another book wrote. `limit(None)` is explicit: a query
        builder default would silently truncate a long book to its first
        page of chunks and produce a quietly wrong centroid.
        """
        if not book_ids:
            return []
        quoted = ", ".join(
            "'" + b.replace("'", "''") + "'" for b in sorted(set(book_ids))
        )
        rows = (
            self._db.open_table("chunks")
            .search()
            .where(f"book_id IN ({quoted})")
            .select(["chunk_id", "entity_ids", "vector"])
            .limit(None)
            .to_list()
        )
        return [
            (row["chunk_id"], list(row["entity_ids"] or []), list(row["vector"]))
            for row in rows
        ]

    def chunk_vectors(self, chunk_ids: list[str]) -> dict[str, list[float]]:
        """chunk_id -> stored vector, for the ids that have a row.

        The query agent ranks a template's candidate passages against the
        question with these. An unknown id is simply absent. `limit(None)`
        is explicit for the same reason as in `chunk_entity_vectors`: the
        builder's default page would leave most candidates unscored, and an
        unscored candidate ranks last.
        """
        if not chunk_ids:
            return {}
        quoted = ", ".join(
            "'" + c.replace("'", "''") + "'" for c in sorted(set(chunk_ids))
        )
        rows = (
            self._db.open_table("chunks")
            .search()
            .where(f"chunk_id IN ({quoted})")
            .select(["chunk_id", "vector"])
            .limit(None)
            .to_list()
        )
        return {row["chunk_id"]: list(row["vector"]) for row in rows}

    def delete_chunks_for_book(self, book_id: str) -> None:
        escaped = book_id.replace("'", "''")
        self._db.open_table("chunks").delete(f"book_id = '{escaped}'")

    def delete_entities(self, entity_ids: list[str]) -> None:
        if not entity_ids:
            return
        quoted = ", ".join("'" + e.replace("'", "''") + "'" for e in entity_ids)
        self._db.open_table("entities").delete(f"entity_id IN ({quoted})")

    def count(self, table: str) -> int:
        return self._db.open_table(table).count_rows()
