"""Load resolved triplets into Kuzu and LanceDB, one book at a time.

Idempotent by construction: nodes are MERGEd (so an entity shared with
another book survives a reload), while this book's REL and MENTIONS edges
and its LanceDB chunk rows are deleted and rewritten, because
re-extraction can legitimately change them.

Failure containment — the two stores have no shared transaction, so the
write is staged in three phases and ordered so that the largest, most
failure-prone body of work sits under the one real transaction available:

  1. Read and compute. Artifacts, registry, resolution, predicate
     normalization, entity centroids and the *entire* Kuzu statement plan
     are built in memory. Nothing has been mutated, so any failure here
     (a missing artifact, a truncated embedder response, an unresolvable
     book) leaves both stores exactly as they were.
  2. Kuzu, inside one explicit transaction. The book's old REL/MENTIONS
     edges are deleted and the new graph written in a single BEGIN/COMMIT.
     A failure rolls the whole thing back: the previous graph survives.
  3. LanceDB, last. Delete-then-add per table, each delete immediately
     followed by its add, with the rows already built. LanceDB offers no
     transaction spanning a delete and an add, so a failure here — and
     only here — can leave the vector store short of this book's rows
     while Kuzu already holds the new graph. That is raised as
     `PartialLoadError`, never swallowed; re-running `load_book` repairs
     it, because the load is deterministic and idempotent.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.graph.artifacts import completed_unit_dirs, read_embeddings, read_triplets
from lore.ingest.registry import chunks_for_book
from lore.resolve.normalize import normalize_name
from lore.resolve.predicates import normalize_predicates
from lore.resolve.resolver import Mention, resolve_mentions

Statement = tuple[str, dict]


@dataclass
class LoadReport:
    book_id: str
    entities: int = 0
    alias_links: int = 0
    rel_edges: int = 0
    chunks: int = 0
    mentions: int = 0


class PartialLoadError(RuntimeError):
    """The graph was written but the vector store was left incomplete.

    The only genuinely non-atomic window in the load. Loud on purpose:
    the caller must re-run the load rather than treat the book as ready.
    """


def _display_name(candidates: Iterable[str]) -> str:
    """The longest surface form, ties broken alphabetically.

    The same rule `lore.resolve.resolver` uses, applied again here so that
    merging with a name already on the node is content-derived rather than
    load-order-derived.
    """
    return sorted(candidates, key=lambda s: (-len(s), s))[0]


def _named_kinds(row: dict) -> list[tuple[str, str]]:
    """The (name, kind) pairs a triplet row names, junk mentions dropped.

    A name with no word characters normalizes to "" and mints no entity
    (see `normalize_name`); minting a node for it here would put a junk
    entity in the graph, so such names are skipped at the source.
    """
    names = list(row.get("entity_names") or [])
    kinds = list(row.get("entity_kinds") or [])
    pairs: list[tuple[str, str]] = []
    for index, name in enumerate(names):
        text = str(name)
        if not normalize_name(text):
            continue
        pairs.append((text, str(kinds[index]) if index < len(kinds) else "Concept"))
    return pairs


def _mentions_from(rows: list[dict], book_id: str) -> list[Mention]:
    """One Mention per (name, kind) pair a triplet row names, with its
    evidence as local context for the embedding pass."""
    seen: set[tuple[str, str]] = set()
    mentions: list[Mention] = []
    for row in rows:
        for name, kind in _named_kinds(row):
            if (name, kind) in seen:
                continue
            seen.add((name, kind))
            mentions.append(
                Mention(name=name, kind=kind, book_id=book_id,
                        context=str(row.get("evidence") or ""))
            )
    return mentions


def _centroid(vectors: list[list[float]]) -> list[float]:
    total = [0.0] * len(vectors[0])
    for vector in vectors:
        for index, value in enumerate(vector):
            total[index] += float(value)
    return [value / len(vectors) for value in total]


def _existing_entities(kuzu: KuzuStore, ids: list[str]) -> dict[str, tuple[str, list]]:
    """Name and aliases already on the Entity nodes we are about to write.

    Entity ids are a hash of (normalized name, kind) with no book in them,
    so a node can already exist because another book named the same
    character. Overwriting its aliases with only this book's view would
    silently drop what the other book knew, so both are unioned below.
    """
    if not ids:
        return {}
    return {
        row[0]: (row[1], list(row[2] or []))
        for row in kuzu.execute(
            "MATCH (e:Entity) WHERE e.id IN $ids RETURN e.id, e.name, e.aliases",
            {"ids": ids},
        )
    }


def _run_in_transaction(kuzu: KuzuStore, statements: list[Statement]) -> None:
    kuzu.execute("BEGIN TRANSACTION")
    try:
        for cypher, params in statements:
            kuzu.execute(cypher, params)
        kuzu.execute("COMMIT")
    except BaseException:
        try:
            kuzu.execute("ROLLBACK")
        except Exception:
            # Kuzu aborts the transaction itself when one of its own
            # statements fails, and then refuses an explicit ROLLBACK.
            # Either way the transaction is not committed.
            pass
        raise


async def load_book(
    config: LoreConfig,
    conn: sqlite3.Connection,
    kuzu: KuzuStore,
    lance: LanceStore,
    book_id: str,
    embed: Callable[[list[str]], Awaitable[list[list[float]]]],
    adjudicate,
) -> LoadReport:
    # ---- Phase 1: read and compute. No store is mutated below this line.
    dirs = completed_unit_dirs(conn, config, book_id)
    if not dirs:
        raise ValueError(f"no completed extraction units for book {book_id}")

    triplets = read_triplets(dirs)
    embeddings = {row["chunk_id"]: list(row["vector"]) for row in read_embeddings(dirs)}
    chunks = chunks_for_book(conn, book_id)
    chapter_of = {chunk.id: chunk.chapter_seq for chunk in chunks}

    resolution = await resolve_mentions(
        _mentions_from(triplets, book_id), embed, adjudicate
    )
    predicate_map = await normalize_predicates(
        [str(row.get("predicate") or "") for row in triplets], embed
    )
    entity_for = resolution.mention_map

    # Surface form -> entity id, for a subject/object the row did not list
    # among its entity_names. First match wins: if the same surface name
    # was mentioned under two different kinds, the one the resolver saw
    # first (its own canonical ordering, not our read order) is used.
    by_surface: dict[str, str] = {}
    for (name, _kind, _book), target in entity_for.items():
        by_surface.setdefault(name, target)

    row = conn.execute(
        "SELECT title, author FROM books WHERE id = ?", (book_id,)
    ).fetchone()
    title, author = (row[0], row[1] or "") if row else ("", "")

    report = LoadReport(book_id=book_id, entities=len(resolution.entities),
                        alias_links=len(resolution.aliases))

    # Which canonical entities each chunk mentions — the bridge LanceDB
    # rows carry, and the source of the MENTIONS edges.
    chunk_entities: dict[str, set[str]] = {}
    rel_statements: list[Statement] = []
    for row in triplets:
        chunk_id = str(row["chunk_id"])
        per_row: dict[str, str] = {}
        for name, kind in _named_kinds(row):
            target = entity_for.get((name, kind, book_id))
            if target is None:
                continue
            per_row.setdefault(name, target)
            chunk_entities.setdefault(chunk_id, set()).add(target)

        subject = str(row.get("subject") or "")
        obj = str(row.get("object") or "")
        subject_id = per_row.get(subject, by_surface.get(subject))
        object_id = per_row.get(obj, by_surface.get(obj))
        if subject_id is None or object_id is None:
            continue
        raw = str(row.get("predicate") or "")
        rel_statements.append((
            "MATCH (s:Entity {id: $s}), (o:Entity {id: $o})"
            " CREATE (s)-[:REL {predicate: $pred, predicate_raw: $raw,"
            " evidence: $ev, confidence: $conf, chunk_id: $chunk,"
            " book_id: $book, chapter_seq: $seq, run_id: $run}]->(o)",
            {"s": subject_id, "o": object_id,
             "pred": predicate_map.get(raw, raw), "raw": raw,
             "ev": str(row.get("evidence") or ""),
             "conf": float(row.get("confidence") or 0.0),
             "chunk": chunk_id, "book": book_id,
             # 0 means "chunk not in the registry": chapters are 1-based,
             # so no real chapter can be confused with it.
             "seq": int(chapter_of.get(chunk_id, 0)),
             "run": str(row.get("run_id") or "")},
        ))
    report.rel_edges = len(rel_statements)

    # Entity node writes, canonicals first; an alias surface form only
    # gets its own node if no canonical already claims that id.
    node_writes: dict[str, dict] = {}
    for entity in resolution.entities:
        node_writes[entity.id] = {
            "id": entity.id, "name": entity.name, "kind": entity.kind,
            "aliases": list(entity.aliases),
        }
    alias_statements: list[Statement] = []
    for alias in resolution.aliases:
        if alias.alias_id == alias.canonical_id:
            continue
        if alias.alias_id not in node_writes:
            node_writes[alias.alias_id] = {
                "id": alias.alias_id, "name": alias.alias_name,
                "kind": alias.kind, "aliases": [],
            }
        alias_statements.append((
            "MATCH (a:Entity {id: $alias}), (c:Entity {id: $canonical})"
            " MERGE (a)-[:ALIAS_OF]->(c)",
            {"alias": alias.alias_id, "canonical": alias.canonical_id},
        ))

    existing = _existing_entities(kuzu, list(node_writes))
    for entity_id_value, params in node_writes.items():
        prior_name, prior_aliases = existing.get(entity_id_value, ("", []))
        params["aliases"] = sorted(set(params["aliases"]) | set(prior_aliases))
        params["name"] = _display_name(
            [params["name"], prior_name] if prior_name else [params["name"]]
        )

    statements: list[Statement] = [
        ("MATCH ()-[r:REL]->() WHERE r.book_id = $book DELETE r", {"book": book_id}),
        ("MATCH (c:Chunk)-[m:MENTIONS]->() WHERE c.book_id = $book DELETE m",
         {"book": book_id}),
        ("MERGE (b:Book {id: $id}) SET b.title = $title, b.author = $author",
         {"id": book_id, "title": title, "author": author}),
    ]
    for chapter_seq in sorted({chunk.chapter_seq for chunk in chunks}):
        chapter_id = f"{book_id}:{chapter_seq}"
        statements.append((
            "MERGE (c:Chapter {id: $id}) SET c.book_id = $book, c.seq = $seq,"
            " c.title = $title",
            {"id": chapter_id, "book": book_id, "seq": chapter_seq,
             "title": f"Chapter {chapter_seq}"},
        ))
        statements.append((
            "MATCH (b:Book {id: $book}), (c:Chapter {id: $chapter})"
            " MERGE (b)-[:HAS_CHAPTER]->(c)",
            {"book": book_id, "chapter": chapter_id},
        ))
    for chunk in chunks:
        statements.append((
            "MERGE (c:Chunk {id: $id}) SET c.book_id = $book, c.chapter_seq = $seq",
            {"id": chunk.id, "book": book_id, "seq": chunk.chapter_seq},
        ))
        statements.append((
            "MATCH (ch:Chapter {id: $chapter}), (c:Chunk {id: $chunk})"
            " MERGE (ch)-[:HAS_CHUNK]->(c)",
            {"chapter": f"{book_id}:{chunk.chapter_seq}", "chunk": chunk.id},
        ))
    for params in node_writes.values():
        statements.append((
            "MERGE (e:Entity {id: $id}) SET e.name = $name, e.kind = $kind,"
            " e.aliases = $aliases",
            params,
        ))
    statements.extend(alias_statements)
    statements.extend(rel_statements)
    # MENTIONS only for chunks the registry actually knows: a triplet row
    # naming a chunk_id with no Chunk node would MATCH nothing, writing no
    # edge while still inflating the report.
    for chunk_id in sorted(chunk_entities):
        if chunk_id not in chapter_of:
            continue
        for entity_id_value in sorted(chunk_entities[chunk_id]):
            statements.append((
                "MATCH (c:Chunk {id: $chunk}), (e:Entity {id: $entity})"
                " MERGE (c)-[:MENTIONS]->(e)",
                {"chunk": chunk_id, "entity": entity_id_value},
            ))
            report.mentions += 1

    lance_chunk_rows = [
        {"vector": embeddings[chunk.id], "chunk_id": chunk.id, "book_id": book_id,
         "chapter_seq": chunk.chapter_seq, "text": chunk.text,
         "entity_ids": sorted(chunk_entities.get(chunk.id, set()))}
        for chunk in chunks
        if chunk.id in embeddings
    ]

    # An entity's vector is the centroid of the chunks that mention it —
    # every one of them, across chapters — so a passage search and an
    # entity search live in the same space. An entity whose chunks carry
    # no embedding (e.g. its chunk_id is not in the artifacts) falls back
    # to embedding its name.
    per_entity_vectors: dict[str, list[list[float]]] = {}
    for chunk_id, entity_ids in chunk_entities.items():
        vector = embeddings.get(chunk_id)
        if vector is None:
            continue
        for entity_id_value in entity_ids:
            per_entity_vectors.setdefault(entity_id_value, []).append(vector)
    uncovered = [e for e in resolution.entities if e.id not in per_entity_vectors]
    fallback: dict[str, list[float]] = {}
    if uncovered:
        vectors = await embed([entity.name for entity in uncovered])
        fallback = {
            entity.id: list(vector) for entity, vector in zip(uncovered, vectors)
        }
    lance_entity_rows = [
        {"vector": (_centroid(per_entity_vectors[entity.id])
                    if entity.id in per_entity_vectors else fallback[entity.id]),
         "entity_id": entity.id, "name": entity.name, "kind": entity.kind}
        for entity in resolution.entities
    ]

    # ---- Phase 2: the graph, under one transaction.
    _run_in_transaction(kuzu, statements)

    # ---- Phase 3: the vectors. The one window without a transaction.
    try:
        lance.delete_chunks_for_book(book_id)
        if lance_chunk_rows:
            lance.add_chunks(lance_chunk_rows)
        lance.delete_entities([entity.id for entity in resolution.entities])
        if lance_entity_rows:
            lance.add_entities(lance_entity_rows)
    except Exception as exc:
        raise PartialLoadError(
            f"book {book_id}: the graph was written but the vector store was"
            " left incomplete. Re-run the load to repair it."
        ) from exc
    report.chunks = len(lance_chunk_rows)
    return report
