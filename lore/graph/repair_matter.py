"""`lore repair matter`: classify an existing book's sections and take what its
front and back matter produced out of every store. No model is asked anything.

A book ingested before sections were classified (lore/ingest/matter.py) had
its licence header, contents page, title page and licence extracted like
chapters. This classifies its sections (from the source file when it is still
there and parses to the same sections, else from the stored chunk text) and
stores them, which on its own makes extraction, beats, the keyword index, a
later load and the reading position's numbering skip them. Then it deletes
what those sections already produced:

- story beats anchored in them (rows, entity links, vectors);
- codex statements quoting them (rows, vectors). The summaries stay: they
  are hidden under a reading position, and the codex stage rewrites an entry
  whose evidence changed;
- the graph's evidence: REL edges citing their chunks, their Chunk nodes
  (with their MENTIONS) and Chapter nodes, and their chunk vectors;
- an entity left with no evidence at all and no alias link, with its vector
  (and codex entry). An entity that still has evidence elsewhere stays, its
  vector recomputed without those chunks, and loses this book from its
  `books` only when nothing of it is left in the book.

The chunk rows in SQLite stay: they are the faithful record of the file.

Idempotent: a second run finds nothing left to delete. Like
`remove_book_from_graph`, the plan is read first, then vectors, the graph in
one transaction, and SQLite last.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from lore.codex import store as codex_store
from lore.ingest.matter import STORY, Classified, Section, classify
from lore.ingest.registry import _write_sections, chunks_for_book, section_rows


@dataclass
class MatterRepair:
    book_id: str
    title: str
    source: str = ""  # "file" or "chunks": what the sections were read from
    sections: list[Classified] = field(default_factory=list)
    changed: bool = False  # the stored classification differs
    chunk_ids: list[str] = field(default_factory=list)  # non-story chunks
    beats: list[tuple[str, int, str]] = field(default_factory=list)  # id, chapter, text
    codex: list[tuple[str, str, str]] = field(default_factory=list)  # id, entity, statement
    rel_edges: list[tuple[str, str, str, str]] = field(default_factory=list)  # s, pred, o, chunk
    chunk_nodes: int = 0
    chapter_nodes: int = 0
    vector_chunks: list[str] = field(default_factory=list)
    entities_deleted: list[tuple[str, str]] = field(default_factory=list)  # id, name
    books_released: dict[str, list[str]] = field(default_factory=dict)  # id -> new books
    recentred: list[str] = field(default_factory=list)  # kept entities whose vector moves

    @property
    def deletes_anything(self) -> bool:
        return bool(self.beats or self.codex or self.rel_edges or self.chunk_nodes
                    or self.chapter_nodes or self.vector_chunks or self.entities_deleted
                    or self.books_released)


# --- the sections -----------------------------------------------------------------------


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _from_file(path: Path, chunks_by_seq: dict[int, list[str]]) -> list[Section] | None:
    """The source file's sections, when it parses to sections that hold the
    stored chunks (same chapter_seqs, each chapter's first chunk opening as
    found in its section); None otherwise."""
    try:
        if path.suffix.lower() == ".epub":
            from lore.ingest.epub import parse_epub
            parsed = parse_epub(path)
        elif path.suffix.lower() == ".pdf":
            from lore.ingest.pdf import parse_pdf
            parsed = parse_pdf(path)
        else:
            return None
    except Exception:  # noqa: BLE001 -- any unreadable file falls back to chunks
        return None
    by_seq = {c.seq: c for c in parsed.chapters}
    for seq, texts in chunks_by_seq.items():
        chapter = by_seq.get(seq)
        # The opening words only: the chunker drops licence sentences, so a
        # longer prefix can span a gap.
        opening = " ".join(_flat(texts[0]).split(" ")[:6])
        if chapter is None or opening not in _flat(chapter.text):
            return None
    return [Section(c.seq, c.title, c.text) for c in parsed.chapters]


def _source_path(file_path: str, roots: list[Path]) -> Path | None:
    path = Path(file_path)
    if path.is_absolute():
        return path if path.exists() else None
    for root in roots:
        if (root / path).exists():
            return root / path
    return None


def book_sections(conn: sqlite3.Connection, book_id: str,
                  roots: list[Path]) -> tuple[list[Section], str]:
    """The book's sections and where they were read from ("file"/"chunks")."""
    chunks = chunks_for_book(conn, book_id, story_only=False)
    by_seq: dict[int, list[str]] = {}
    for c in chunks:
        by_seq.setdefault(c.chapter_seq, []).append(c.text)
    row = conn.execute("SELECT file_path FROM books WHERE id = ?", (book_id,)).fetchone()
    path = _source_path(row[0], roots) if row and row[0] else None
    if path is not None and by_seq:
        found = _from_file(path, by_seq)
        if found is not None:
            return found, "file"
    return [Section(seq, None, " ".join(texts)) for seq, texts in sorted(by_seq.items())], "chunks"


# --- the plan --------------------------------------------------------------------------


def _rows(kuzu, cypher: str, params: dict) -> list:
    return list(kuzu.execute(cypher, params))


def plan_repair(conn: sqlite3.Connection, kuzu, lance, book_id: str,
                roots: list[Path]) -> MatterRepair:
    """What the repair would do for one book; changes nothing."""
    title = (conn.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
             or (book_id,))[0]
    report = MatterRepair(book_id=book_id, title=title)
    sections, report.source = book_sections(conn, book_id, roots)
    report.sections = classify(sections)
    report.changed = section_rows(conn, book_id) != report.sections
    hidden = {c.seq for c in report.sections if c.matter != STORY}
    if not hidden:
        return report
    ids = sorted(c.id for c in chunks_for_book(conn, book_id, story_only=False)
                 if c.chapter_seq in hidden)
    report.chunk_ids = ids
    id_set = set(ids)

    marks = ",".join("?" * len(hidden))
    report.beats = [(r[0], r[1], r[2]) for r in conn.execute(
        f"SELECT id, chapter_seq, text FROM beats WHERE book_id = ? AND (chapter_seq IN ({marks})"
        f" OR chunk_id IN ({','.join('?' * len(ids)) or 'NULL'})) ORDER BY chapter_seq, id",
        (book_id, *sorted(hidden), *ids))]
    if ids:
        report.codex = [(r[0], r[1], r[2]) for r in conn.execute(
            f"SELECT id, entity_id, statement FROM codex_entries WHERE chunk_id IN"
            f" ({','.join('?' * len(ids))}) ORDER BY entity_id, position", ids)]
    if lance is not None:
        report.vector_chunks = sorted(lance.present_chunks(ids))
    if kuzu is None:
        return report

    report.rel_edges = [(r[0], r[1], r[2], r[3]) for r in _rows(
        kuzu, "MATCH (s:Entity)-[r:REL]->(o:Entity) WHERE r.chunk_id IN $ids"
        " RETURN s.name, r.predicate, o.name, r.chunk_id ORDER BY r.chunk_id, s.name, o.name",
        {"ids": ids})]
    report.chunk_nodes = int(_rows(kuzu, "MATCH (c:Chunk) WHERE c.id IN $ids RETURN count(c)",
                                   {"ids": ids})[0][0])
    report.chapter_nodes = int(_rows(
        kuzu, "MATCH (c:Chapter) WHERE c.book_id = $book AND c.seq IN $seqs RETURN count(c)",
        {"book": book_id, "seqs": sorted(hidden)})[0][0])

    # Entities whose evidence these chunks are part of.
    touched = {r[0] for r in _rows(
        kuzu, "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) WHERE c.id IN $ids RETURN DISTINCT e.id",
        {"ids": ids})}
    mentioned = set(touched)
    touched |= {i for r in _rows(
        kuzu, "MATCH (s:Entity)-[r:REL]->(o:Entity) WHERE r.chunk_id IN $ids RETURN s.id, o.id",
        {"ids": ids}) for i in r}
    if not touched:
        return report
    nodes = {r[0]: (r[1], list(r[2] or ())) for r in _rows(
        kuzu, "MATCH (e:Entity) WHERE e.id IN $ids RETURN e.id, e.name, e.books",
        {"ids": sorted(touched)})}
    aliased = {i for r in _rows(
        kuzu, "MATCH (a:Entity)-[:ALIAS_OF]->(c:Entity) WHERE a.id IN $ids OR c.id IN $ids"
        " RETURN a.id, c.id", {"ids": sorted(touched)}) for i in r}
    # What is left of each once these chunks are gone: (book of a mention or edge).
    left: dict[str, set[str]] = {i: set() for i in nodes}
    for entity, book in _rows(
            kuzu, "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) WHERE e.id IN $ents"
            " AND NOT c.id IN $ids RETURN e.id, c.book_id",
            {"ents": sorted(nodes), "ids": ids}):
        left[entity].add(book)
    for s, o, book in _rows(
            kuzu, "MATCH (s:Entity)-[r:REL]-(o:Entity) WHERE s.id IN $ents"
            " AND NOT r.chunk_id IN $ids RETURN s.id, o.id, r.book_id",
            {"ents": sorted(nodes), "ids": ids}):
        left[s].add(book)
    for entity_id, (name, books) in sorted(nodes.items()):
        if entity_id in aliased:
            continue  # an alias decision is the resolver's; leave it whole
        if not left[entity_id]:
            report.entities_deleted.append((entity_id, name))
        elif book_id in books and book_id not in left[entity_id]:
            report.books_released[entity_id] = [b for b in books if b != book_id]
    gone = {i for i, _ in report.entities_deleted}
    report.recentred = sorted(i for i in mentioned if i in nodes and i not in gone)
    return report


# --- applying it ------------------------------------------------------------------------


def apply_repair(conn: sqlite3.Connection, kuzu, lance, report: MatterRepair) -> None:
    """Carry out `report` (from `plan_repair`). Safe to repeat."""
    from lore.graph.loader import _centroid, _run_in_transaction

    gone = [i for i, _ in report.entities_deleted]
    beat_ids = [b[0] for b in report.beats]
    codex_ids = [c[0] for c in report.codex]
    gone_codex = codex_store.statement_ids(conn, gone) if gone else []

    # ---- Vectors first.
    if lance is not None:
        lance.delete_beats(beat_ids)
        lance.delete_codex(sorted(set(codex_ids) | set(gone_codex)))
        lance.delete_chunks(report.chunk_ids)
        lance.delete_entities(gone)
        if report.recentred and kuzu is not None:
            books = sorted({b for r in kuzu.execute(
                "MATCH (e:Entity) WHERE e.id IN $ids RETURN e.books",
                {"ids": report.recentred}) for b in (r[0] or ())})
            per: dict[str, list[tuple[str, list[float]]]] = {}
            for chunk_id, entity_ids, vector in lance.chunk_entity_vectors(books):
                for e in entity_ids:
                    if e in report.recentred:
                        per.setdefault(e, []).append((chunk_id, list(vector)))
            names = {r[0]: (r[1], r[2]) for r in kuzu.execute(
                "MATCH (e:Entity) WHERE e.id IN $ids RETURN e.id, e.name, e.kind",
                {"ids": sorted(per)})}
            rows = [{"vector": _centroid([v for _c, v in sorted(per[e])]), "entity_id": e,
                     "name": names[e][0], "kind": names[e][1]}
                    for e in sorted(per) if e in names]
            if rows:
                lance.delete_entities([r["entity_id"] for r in rows])
                lance.add_entities(rows)

    # ---- The graph, one transaction.
    if kuzu is not None and report.chunk_ids:
        hidden = sorted({c.seq for c in report.sections if c.matter != STORY})
        statements = [
            ("MATCH ()-[r:REL]->() WHERE r.chunk_id IN $ids DELETE r", {"ids": report.chunk_ids}),
            ("MATCH (c:Chunk) WHERE c.id IN $ids DETACH DELETE c", {"ids": report.chunk_ids}),
            ("MATCH (c:Chapter) WHERE c.book_id = $book AND c.seq IN $seqs DETACH DELETE c",
             {"book": report.book_id, "seqs": hidden}),
        ]
        for entity_id in gone:
            statements.append(("MATCH (e:Entity {id: $id}) DETACH DELETE e", {"id": entity_id}))
        for entity_id, books in sorted(report.books_released.items()):
            statements.append(("MATCH (e:Entity {id: $id}) SET e.books = $books",
                               {"id": entity_id, "books": books}))
        _run_in_transaction(kuzu, statements)

    # ---- SQLite last.
    if gone:
        codex_store.delete_entities(conn, gone)
    try:
        conn.execute("BEGIN")
        _write_sections(conn, report.book_id, report.sections)
        for start in range(0, len(beat_ids), 500):
            batch = beat_ids[start:start + 500]
            marks = ",".join("?" * len(batch))
            conn.execute(f"DELETE FROM beat_entities WHERE beat_id IN ({marks})", batch)
            conn.execute(f"DELETE FROM beats WHERE id IN ({marks})", batch)
        for start in range(0, len(codex_ids), 500):
            batch = codex_ids[start:start + 500]
            conn.execute(f"DELETE FROM codex_entries WHERE id IN ({','.join('?' * len(batch))})",
                         batch)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
