"""Embedded Kuzu graph store — SPEC.md §6 schema, applied idempotently."""
from __future__ import annotations

from pathlib import Path

import kuzu

_SCHEMA: list[str] = [
    "CREATE NODE TABLE Book (id STRING, title STRING, author STRING, PRIMARY KEY (id))",
    "CREATE NODE TABLE Chapter (id STRING, book_id STRING, seq INT64, title STRING, PRIMARY KEY (id))",
    "CREATE NODE TABLE Chunk (id STRING, book_id STRING, chapter_seq INT64, PRIMARY KEY (id))",
    (
        "CREATE NODE TABLE Entity (id STRING, name STRING, kind STRING, "
        "aliases STRING[], books STRING[], PRIMARY KEY (id))"
    ),
    "CREATE REL TABLE HAS_CHAPTER (FROM Book TO Chapter)",
    "CREATE REL TABLE HAS_CHUNK (FROM Chapter TO Chunk)",
    "CREATE REL TABLE MENTIONS (FROM Chunk TO Entity)",
    "CREATE REL TABLE ALIAS_OF (FROM Entity TO Entity)",
    (
        "CREATE REL TABLE REL (FROM Entity TO Entity, predicate STRING, "
        "predicate_raw STRING, evidence STRING, confidence DOUBLE, "
        "chunk_id STRING, book_id STRING, chapter_seq INT64, run_id STRING)"
    ),
]


class KuzuStore:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        if read_only:
            # For readers such as `lore ask`: no directory, no schema, no
            # migration, and Kuzu itself refuses any write. Opening a path
            # with no database raises rather than creating one.
            self._db = kuzu.Database(str(path), read_only=True)
            self._conn = kuzu.Connection(self._db)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = kuzu.Database(str(path))
        self._conn = kuzu.Connection(self._db)
        existing = self.table_names()
        for ddl in _SCHEMA:
            table = ddl.split()[3]  # CREATE NODE|REL TABLE <name> ...
            if table not in existing:
                self._conn.execute(ddl)

        if "REL" in existing:
            columns = {
                row[1]
                for row in self._conn.execute("CALL table_info('REL') RETURN *")
            }
            if "predicate_raw" not in columns:
                self._conn.execute("ALTER TABLE REL ADD predicate_raw STRING")

        if "Entity" in existing:
            columns = {
                row[1]
                for row in self._conn.execute("CALL table_info('Entity') RETURN *")
            }
            if "books" not in columns:
                # Which books an entity was seen in. Without it the loader
                # cannot tell a book rewriting its own node from a genuine
                # cross-book merge, and would fuse the two silently.
                self._conn.execute("ALTER TABLE Entity ADD books STRING[]")

    def execute(self, cypher: str, params: dict | None = None) -> kuzu.QueryResult:
        return self._conn.execute(cypher, parameters=params or {})

    def table_names(self) -> set[str]:
        result = self._conn.execute("CALL show_tables() RETURN name")
        return {row[0] for row in result}

    def close(self) -> None:
        self._conn.close()
        self._db.close()
