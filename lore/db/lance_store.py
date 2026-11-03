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

_BEATS_SCHEMA = pa.schema([
    pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
    pa.field("id", pa.string()),
    pa.field("book_id", pa.string()),
])


_CODEX_SCHEMA = pa.schema([
    pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
    pa.field("id", pa.string()),
])


def _quoted(values) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in sorted(set(values)))


class LanceStore:
    def __init__(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(path))
        existing = self.table_names()
        if "chunks" not in existing:
            self._db.create_table("chunks", schema=_CHUNKS_SCHEMA)
        if "entities" not in existing:
            self._db.create_table("entities", schema=_ENTITIES_SCHEMA)
        if "beats" not in existing:
            self._db.create_table("beats", schema=_BEATS_SCHEMA)
        if "codex" not in existing:
            self._db.create_table("codex", schema=_CODEX_SCHEMA)

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

    def search_chunks_in_books(
        self, vector: list[float], book_ids: list[str], limit: int = 5
    ) -> list[dict]:
        """Nearest chunks of these books (a world's), prefiltered like
        `search_chunks_in_book`. No books, no rows."""
        if not book_ids:
            return []
        return (
            self._db.open_table("chunks")
            .search(vector)
            .where(f"book_id IN ({_quoted(book_ids)})", prefilter=True)
            .limit(limit)
            .to_list()
        )

    def book_has_chunks(self, book_id: str) -> bool:
        """Whether ONE book has any chunk rows at all.

        Chunks are embedded only by `lore graph load`, so a book that was
        chunked but never loaded has none, and a book-filtered search of it
        is empty for that reason rather than because nothing matched. The
        filter is quoted the same way as everywhere else in this class.
        """
        escaped = book_id.replace("'", "''")
        return self._db.open_table("chunks").count_rows(
            f"book_id = '{escaped}'") > 0

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

    def upsert_beats(self, rows: list[dict]) -> None:
        """Rows {id, book_id, vector}; an existing id is replaced."""
        if not rows:
            return
        self.delete_beats([r["id"] for r in rows])
        self._db.open_table("beats").add(rows)

    def delete_beats(self, beat_ids: list[str]) -> None:
        if beat_ids:
            self._db.open_table("beats").delete(f"id IN ({_quoted(beat_ids)})")

    def search_beats(
        self, vector: list[float], beat_ids: list[str], k: int
    ) -> list[dict]:
        """The `k` nearest of THESE beats. Beats carry no entity column (the
        entity links change on every relink), so the caller passes the ids
        it got from `beats_for_entities`. The filter runs inside the search.
        """
        if not beat_ids or k <= 0:
            return []
        return (
            self._db.open_table("beats")
            .search(vector)
            .where(f"id IN ({_quoted(beat_ids)})", prefilter=True)
            .limit(k)
            .to_list()
        )

    def upsert_codex(self, rows: list[dict]) -> None:
        """Rows {id, vector}, one per codex statement; an existing id is
        replaced."""
        if not rows:
            return
        self.delete_codex([r["id"] for r in rows])
        self._db.open_table("codex").add(rows)

    def delete_codex(self, statement_ids: list[str]) -> None:
        if statement_ids:
            self._db.open_table("codex").delete(
                f"id IN ({_quoted(statement_ids)})")

    def search_codex(
        self, vector: list[float], statement_ids: list[str], k: int
    ) -> list[dict]:
        """The `k` nearest of THESE codex statements. Like `search_beats`, the
        caller passes the ids it got from SQLite (an entry's entity link moves
        on relink, so the vector rows carry none). The filter runs inside the
        search."""
        if not statement_ids or k <= 0:
            return []
        return (
            self._db.open_table("codex")
            .search(vector)
            .where(f"id IN ({_quoted(statement_ids)})", prefilter=True)
            .limit(k)
            .to_list()
        )

    def count(self, table: str) -> int:
        return self._db.open_table(table).count_rows()
