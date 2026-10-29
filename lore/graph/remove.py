"""Take one book out of the graph and the vector indexes.

The inverse of `load_book` for the stores it writes (Kuzu and LanceDB), plus
the book's story beats. What the registry holds stays: the chunks in SQLite
and every cache (extraction artifacts, adjudication, speakers, disambiguation
answers, beats JSON), so loading the book again asks nothing new.

Entities are shared between books, so nothing is deleted just because this
book mentions it: the book is dropped from `Entity.books`, and the node goes
only when no book is left. Alias nodes follow the same rule, and an alias
form that only a deleted alias node carried is dropped from the canonical
entity it pointed to.

Idempotent and resumable. The vector store goes first, then the graph in one
transaction, then SQLite: a run stopped between two of them leaves the next
run something to find and finish, because each step is decided from what the
graph and SQLite still hold, and every delete is safe to repeat.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore import worlds
from lore.graph.loader import (
    Statement, _centroid, _note_reconciled, _run_in_transaction)
from lore.resolve.normalize import normalize_name
from lore.resolve.resolver import display_name


@dataclass
class RemovalReport:
    book_id: str
    chunks: int = 0  # Chunk nodes deleted
    rel_edges: int = 0
    entities_deleted: int = 0  # no book left (aliases included)
    entities_released: int = 0  # kept: other books still list them
    aliases_pruned: int = 0  # surface forms dropped from a kept entity
    vector_chunks: int = 0  # Lance chunk rows deleted
    beats: int = 0
    shared_with: tuple[str, ...] = ()  # the other books of the kept entities


def _count(kuzu: KuzuStore, cypher: str, params: dict) -> int:
    return int(next(iter(kuzu.execute(cypher, params)))[0])


def remove_book_from_graph(
    conn: sqlite3.Connection, kuzu: KuzuStore, lance: LanceStore, book_id: str
) -> RemovalReport:
    report = RemovalReport(book_id=book_id)
    # The first graph open after migration 9 heals worlds split around an
    # already-fused entity (see worlds.ensure_reconciled); registry rows only.
    _note_reconciled(worlds.ensure_reconciled(conn, kuzu))

    # ---- Read. Nothing is changed until the plan is complete.
    owned = {
        row[0]: (row[1], row[2], list(row[3] or ()), list(row[4] or ()))
        for row in kuzu.execute(
            "MATCH (e:Entity) WHERE list_contains(e.books, $book)"
            " RETURN e.id, e.name, e.kind, e.aliases, e.books",
            {"book": book_id})
    }
    orphans = sorted(i for i, (*_x, books) in owned.items()
                     if set(books) <= {book_id})
    released = {i: v for i, v in owned.items() if i not in set(orphans)}
    shared_with = sorted({b for *_x, books in released.values()
                          for b in books} - {book_id})

    # Alias forms only a deleted alias node carried. The node's own name is
    # the surface form; the canonical lists its normalized form.
    pruned: dict[str, set[str]] = {}
    if orphans and released:
        for alias_name, canonical in kuzu.execute(
                "MATCH (a:Entity)-[:ALIAS_OF]->(c:Entity) WHERE a.id IN $ids"
                " RETURN a.name, c.id", {"ids": orphans}):
            if canonical in released and alias_name:
                pruned.setdefault(canonical, set()).add(normalize_name(alias_name))

    kept: dict[str, tuple[str, str, list[str], list[str]]] = {}
    for node_id, (name, kind, aliases, books) in released.items():
        drop = pruned.get(node_id, set())
        new_aliases = [a for a in aliases if a not in drop]
        report.aliases_pruned += len(aliases) - len(new_aliases)
        new_name = name
        if drop and normalize_name(name) in drop:
            # The shown name was this book's form; take the best of the forms
            # other alias nodes still carry, else keep it.
            left = [r[0] for r in kuzu.execute(
                "MATCH (a:Entity)-[:ALIAS_OF]->(c:Entity {id: $id})"
                " WHERE NOT a.id IN $gone RETURN a.name",
                {"id": node_id, "gone": orphans}) if r[0]]
            if left:
                new_name = display_name(left)
        kept[node_id] = (new_name, kind, new_aliases,
                         [b for b in books if b != book_id])

    report.chunks = _count(
        kuzu, "MATCH (c:Chunk) WHERE c.book_id = $book RETURN count(c)",
        {"book": book_id})
    report.rel_edges = _count(
        kuzu, "MATCH ()-[r:REL]->() WHERE r.book_id = $book RETURN count(r)",
        {"book": book_id})
    report.entities_deleted = len(orphans)
    report.entities_released = len(kept)
    report.shared_with = tuple(shared_with)

    beat_ids = [r[0] for r in conn.execute(
        "SELECT id FROM beats WHERE book_id = ? ORDER BY id", (book_id,))]
    report.beats = len(beat_ids)

    # ---- Vectors first (see the module docstring).
    lance.delete_beats(beat_ids)
    before = lance.count("chunks")
    lance.delete_chunks_for_book(book_id)
    report.vector_chunks = before - lance.count("chunks")
    lance.delete_entities(orphans)
    if kept and shared_with:
        # A kept entity's vector is the mean of every chunk that mentions it,
        # this book's included until now: recompute it from the others.
        per_entity: dict[str, list[tuple[str, list[float]]]] = {}
        for chunk_id, entity_ids, vector in lance.chunk_entity_vectors(shared_with):
            for entity_id in entity_ids:
                if entity_id in kept:
                    per_entity.setdefault(entity_id, []).append(
                        (chunk_id, list(vector)))
        rows = [
            {"vector": _centroid([v for _cid, v in sorted(per_entity[i])]),
             "entity_id": i, "name": kept[i][0], "kind": kept[i][1]}
            for i in sorted(per_entity)
        ]
        if rows:
            lance.delete_entities([r["entity_id"] for r in rows])
            lance.add_entities(rows)

    # ---- The graph, one transaction.
    statements: list[Statement] = [
        ("MATCH ()-[r:REL]->() WHERE r.book_id = $book DELETE r", {"book": book_id}),
        # DETACH takes the Chunk's MENTIONS and HAS_CHUNK edges with it.
        ("MATCH (c:Chunk) WHERE c.book_id = $book DETACH DELETE c",
         {"book": book_id}),
        ("MATCH (c:Chapter) WHERE c.book_id = $book DETACH DELETE c",
         {"book": book_id}),
        ("MATCH (b:Book {id: $book}) DETACH DELETE b", {"book": book_id}),
    ]
    for node_id in orphans:
        statements.append(
            ("MATCH (e:Entity {id: $id}) DETACH DELETE e", {"id": node_id}))
    for node_id in sorted(kept):
        name, _kind, aliases, books = kept[node_id]
        statements.append((
            "MATCH (e:Entity {id: $id}) SET e.name = $name, e.aliases = $aliases,"
            " e.books = $books",
            {"id": node_id, "name": name, "aliases": aliases, "books": books}))
    _run_in_transaction(kuzu, statements)

    # ---- SQLite last: the beats' ids were read before anything was deleted.
    with conn:
        conn.execute(
            "DELETE FROM beat_entities WHERE beat_id IN"
            " (SELECT id FROM beats WHERE book_id = ?)", (book_id,))
        conn.execute("DELETE FROM beats WHERE book_id = ?", (book_id,))
        # No longer in the graph, so no longer searchable.
        conn.execute("UPDATE books SET status = 'chunked'"
                     " WHERE id = ? AND status = 'graphed'", (book_id,))
    return report
