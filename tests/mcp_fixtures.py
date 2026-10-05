"""One builder for the MCP tests' on-disk store triples.

The repo has no conftest.py and imports explicitly per file, so this is a
plain module: each test file wraps `build_store_triple` in its own fixture
and names it for what it holds. Do not add a shared `live_config` fixture
here -- two files once used that name for two different stores.

Rows are plain tuples so a test reads as a table:
  books    (id, title)
  chunks   (id, book_id, chapter_seq)
  entities (id, name, kind, books, aliases)
  edges    (subject, object, predicate, chunk_id, book_id, chapter_seq)
  aliases  (alias_id, canonical_id)   -- an ALIAS_OF edge, as the loader writes it
  jobs     (kind, book_id, state, error, updated_at)
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.db.sqlite import open_db


def empty_config(tmp_path: Path) -> LoreConfig:
    """A config pointing at a data dir with no stores in it."""
    return LoreConfig(data_dir=tmp_path / "nothing")


def build_store_triple(
    tmp_path: Path,
    *,
    books: Sequence[tuple] = (),
    chunks: Sequence[tuple] = (),
    entities: Sequence[tuple] = (),
    edges: Sequence[tuple] = (),
    aliases: Sequence[tuple] = (),
    jobs: Sequence[tuple] = (),
) -> LoreConfig:
    """A real Kuzu + LanceDB + SQLite triple, built the way the CLI builds them."""
    config = LoreConfig(data_dir=tmp_path / "data")
    graph = KuzuStore(config.kuzu_dir / "graph")
    for entity_id, name, kind, entity_books, entity_aliases in entities:
        graph.execute(
            "CREATE (:Entity {id: $id, name: $name, kind: $kind,"
            " books: $books, aliases: $aliases})",
            {"id": entity_id, "name": name, "kind": kind,
             "books": list(entity_books), "aliases": list(entity_aliases)},
        )
    for alias_id, canonical_id in aliases:
        graph.execute(
            "MATCH (a:Entity {id: $a}), (c:Entity {id: $c})"
            " CREATE (a)-[:ALIAS_OF]->(c)",
            {"a": alias_id, "c": canonical_id},
        )
    for subject, obj, predicate, chunk_id, book_id, chapter_seq in edges:
        graph.execute(
            "MATCH (s:Entity {id: $s}), (o:Entity {id: $o})"
            " CREATE (s)-[:REL {predicate: $p, predicate_raw: $p,"
            " evidence: $ev, confidence: 0.9, chunk_id: $chunk,"
            " book_id: $book, chapter_seq: $seq, run_id: 'r'}]->(o)",
            {"s": subject, "o": obj, "p": predicate,
             "ev": f"{subject} {predicate} {obj}", "chunk": chunk_id,
             "book": book_id, "seq": chapter_seq},
        )
    graph.close()
    LanceStore(config.lancedb_dir)
    conn = open_db(config.sqlite_path)
    for book_id, title in books:
        conn.execute(
            "INSERT INTO books (id, title, file_path) VALUES (?, ?, 'x.txt')",
            (book_id, title),
        )
    for chunk_id, book_id, chapter_seq in chunks:
        conn.execute(
            "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
            " VALUES (?, ?, ?, 1, 1, 'text')", (chunk_id, book_id, chapter_seq),
        )
    for kind, book_id, state, err, ts in jobs:
        conn.execute(
            "INSERT INTO jobs (kind, book_id, state, error, updated_at)"
            " VALUES (?, ?, ?, ?, ?)", (kind, book_id, state, err, ts),
        )
    conn.commit()
    conn.close()
    return config
