"""Story beats in SQLite: save per chapter part, load by entity, relink.

A beat stores entity *names*; `relink` maps them to the current entity ids
(aliases and merged stubs included) so beats follow resolver changes.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Protocol, Sequence

KINDS = ("action", "decision", "feeling", "treatment", "change")


@dataclass(frozen=True)
class Beat:
    """One quote-anchored moment. `entities[0]` is the subject; `entity_ids`
    is parallel to `entities` ('' where a name is not linked), filled on load.
    """
    id: str
    book_id: str
    chapter_seq: int
    chunk_id: str
    entities: tuple[str, ...]
    kind: str
    text: str
    quote: str
    entity_ids: tuple[str, ...] = ()


class _Kuzu(Protocol):
    def execute(self, cypher: str, params: dict | None = None): ...


def beat_id(book_id: str, chapter_seq: int, entities: Sequence[str],
            quote: str) -> str:
    key = "\x1f".join([book_id, str(chapter_seq), "\x1e".join(entities), quote])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def save_beats(conn: sqlite3.Connection, book_id: str, chapter: int, part: int,
               beats: Sequence[Beat]) -> None:
    """Replace this (book, chapter, part)'s beats with `beats`. Idempotent."""
    with conn:
        conn.execute(
            "DELETE FROM beat_entities WHERE beat_id IN (SELECT id FROM beats"
            " WHERE book_id=? AND chapter_seq=? AND part=?)",
            (book_id, chapter, part))
        conn.execute(
            "DELETE FROM beats WHERE book_id=? AND chapter_seq=? AND part=?",
            (book_id, chapter, part))
        for b in beats:
            conn.execute(
                "INSERT OR REPLACE INTO beats (id, book_id, chapter_seq, part,"
                " chunk_id, kind, text, quote) VALUES (?,?,?,?,?,?,?,?)",
                (b.id, book_id, chapter, part, b.chunk_id, b.kind, b.text,
                 b.quote))
            conn.execute("DELETE FROM beat_entities WHERE beat_id=?", (b.id,))
            conn.executemany(
                "INSERT INTO beat_entities (beat_id, position, name)"
                " VALUES (?,?,?)",
                [(b.id, i, n) for i, n in enumerate(b.entities)])


def beats_for_entities(conn: sqlite3.Connection,
                       entity_ids: Sequence[str]) -> list[Beat]:
    """Every beat involving any of these entities, once each, in chapter order
    within a book (books ordered by id; callers re-order with
    `position.order_passages`)."""
    ids = sorted(set(entity_ids))
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT b.id, b.book_id, b.chapter_seq, b.chunk_id, b.kind, b.text,"
        f" b.quote FROM beats b WHERE b.id IN (SELECT beat_id FROM"
        f" beat_entities WHERE entity_id IN ({marks}))"
        f" ORDER BY b.book_id, b.chapter_seq, b.part, b.id", ids).fetchall()
    out: list[Beat] = []
    for r in rows:
        ents = conn.execute(
            "SELECT name, entity_id FROM beat_entities WHERE beat_id=?"
            " ORDER BY position", (r[0],)).fetchall()
        out.append(Beat(
            id=r[0], book_id=r[1], chapter_seq=r[2], chunk_id=r[3], kind=r[4],
            text=r[5], quote=r[6], entities=tuple(e[0] for e in ents),
            entity_ids=tuple(e[1] or "" for e in ents)))
    return out


def count(conn: sqlite3.Connection, book_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM beats WHERE book_id=?",
                        (book_id,)).fetchone()[0]


def _name_map(kuzu: _Kuzu) -> dict[str, str]:
    """lower-cased name or alias -> canonical entity id."""
    stubs = {a: c for a, c in kuzu.execute(
        "MATCH (a:Entity)-[:ALIAS_OF]->(c:Entity) RETURN a.id, c.id")}
    rows = list(kuzu.execute(
        "MATCH (e:Entity) RETURN e.id, e.name, e.aliases"))
    mapping: dict[str, str] = {}
    # Own names first, then aliases, so a primary name wins a clash.
    for pass_names in (True, False):
        for eid, name, aliases in sorted(rows, key=lambda r: r[0]):
            forms = [name] if pass_names else list(aliases or [])
            for form in forms:
                if form:
                    mapping.setdefault(form.strip().lower(),
                                       stubs.get(eid, eid))
    return mapping


def relink(conn: sqlite3.Connection, kuzu: _Kuzu) -> int:
    """Point every beat's entity names at the current entity ids. Returns the
    number of rows whose id changed. A name nothing matches becomes NULL."""
    mapping = _name_map(kuzu)
    changed = 0
    with conn:
        for name, current in conn.execute(
                "SELECT DISTINCT name, entity_id FROM beat_entities").fetchall():
            new = mapping.get(name.strip().lower())
            if new != current:
                changed += conn.execute(
                    "UPDATE beat_entities SET entity_id=? WHERE name=?",
                    (new, name)).rowcount
    return changed
