"""The codex in SQLite: one profile per significant entity, saved, loaded, relinked.

A codex entry is a quote-verified profile of one entity (see build.py): a
one-line summary and statements grouped by aspect, each statement pinned to
the chunk that holds its quote. It stores no interpretation.

Rows are keyed by entity id. `relink` follows the resolver: an entity that was
merged into another (it is now an alias stub) hands its entry to the entity it
became, and an entity that is gone loses its entry.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Protocol, Sequence

ASPECTS = ("nature", "history", "abilities", "relationships", "role", "change")


@dataclass(frozen=True)
class Statement:
    """One verified statement. `id` and `position` are set by `save_entry`
    (the statement's place in its entry, in aspect then story order)."""
    aspect: str
    text: str
    quote: str
    chunk_id: str
    id: str = ""
    position: int = 0


@dataclass(frozen=True)
class CodexSummary:
    entity_id: str
    world_id: int
    name: str
    kind: str
    summary: str
    prompt_hash: str
    evidence_hash: str


@dataclass(frozen=True)
class CodexEntry:
    entity_id: str
    world_id: int
    name: str
    kind: str
    summary: str
    statements: tuple[Statement, ...]
    prompt_hash: str = ""
    evidence_hash: str = ""


class _Kuzu(Protocol):
    def execute(self, cypher: str, params: dict | None = None): ...


def statement_id(entity_id: str, position: int, text: str) -> str:
    key = "\x1f".join([entity_id, str(position), text])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _batches(items: Sequence[str], size: int = 500):
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def save_entry(conn: sqlite3.Connection, entry: CodexEntry) -> list[str]:
    """Replace this entity's entry with `entry`. Idempotent. Returns the ids of
    the statements it now holds, in order (the ids the vector index needs)."""
    ids: list[str] = []
    with conn:
        conn.execute("DELETE FROM codex_entries WHERE entity_id=?",
                     (entry.entity_id,))
        conn.execute("DELETE FROM codex_summaries WHERE entity_id=?",
                     (entry.entity_id,))
        conn.execute(
            "INSERT INTO codex_summaries (entity_id, world_id, name, kind,"
            " summary, prompt_hash, evidence_hash) VALUES (?,?,?,?,?,?,?)",
            (entry.entity_id, entry.world_id, entry.name, entry.kind,
             entry.summary, entry.prompt_hash, entry.evidence_hash))
        for position, s in enumerate(entry.statements):
            sid = statement_id(entry.entity_id, position, s.text)
            ids.append(sid)
            conn.execute(
                "INSERT INTO codex_entries (id, entity_id, world_id, aspect,"
                " position, statement, quote, chunk_id) VALUES (?,?,?,?,?,?,?,?)",
                (sid, entry.entity_id, entry.world_id, s.aspect, position,
                 s.text, s.quote, s.chunk_id))
    return ids


def statement_ids(conn: sqlite3.Connection,
                  entity_ids: Sequence[str]) -> list[str]:
    """The ids of every saved statement of these entities."""
    out: list[str] = []
    for batch in _batches(sorted(set(entity_ids))):
        out += [r[0] for r in conn.execute(
            "SELECT id FROM codex_entries WHERE entity_id IN"
            f" ({','.join('?' * len(batch))}) ORDER BY id", batch)]
    return out


def with_entries(conn: sqlite3.Connection,
                 entity_ids: Sequence[str]) -> list[str]:
    """The ones among these entity ids that have an entry."""
    out: list[str] = []
    for batch in _batches(sorted(set(entity_ids))):
        out += [r[0] for r in conn.execute(
            "SELECT entity_id FROM codex_summaries WHERE entity_id IN"
            f" ({','.join('?' * len(batch))}) ORDER BY entity_id", batch)]
    return out


def delete_entities(conn: sqlite3.Connection,
                    entity_ids: Sequence[str]) -> list[str]:
    """Delete these entities' entries. Returns the statement ids that went, so
    the caller can drop them from the vector index too. Safe to repeat."""
    gone = statement_ids(conn, entity_ids)
    with conn:
        for batch in _batches(sorted(set(entity_ids))):
            marks = ",".join("?" * len(batch))
            conn.execute(f"DELETE FROM codex_entries WHERE entity_id IN ({marks})",
                         batch)
            conn.execute(f"DELETE FROM codex_summaries WHERE entity_id IN ({marks})",
                         batch)
    return gone


def _summary(r) -> CodexSummary:
    return CodexSummary(entity_id=r[0], world_id=r[1], name=r[2], kind=r[3],
                        summary=r[4], prompt_hash=r[5], evidence_hash=r[6])


_SUMMARY_COLS = ("entity_id, world_id, name, kind, summary, prompt_hash,"
                 " evidence_hash")


def summary_for(conn: sqlite3.Connection, entity_id: str) -> CodexSummary | None:
    r = conn.execute(f"SELECT {_SUMMARY_COLS} FROM codex_summaries"
                     " WHERE entity_id=?", (entity_id,)).fetchone()
    return _summary(r) if r else None


def summaries_for_world(conn: sqlite3.Connection,
                        world_id: int) -> list[CodexSummary]:
    """Every entity of the world that has an entry, with its one-line summary
    (what the planner reads), by name."""
    return [_summary(r) for r in conn.execute(
        f"SELECT {_SUMMARY_COLS} FROM codex_summaries WHERE world_id=?"
        " ORDER BY lower(name), entity_id", (world_id,))]


def entries_for(conn: sqlite3.Connection,
                entity_ids: Sequence[str]) -> list[CodexEntry]:
    """The entries of these entities (those that have one), in the order the
    ids are given, each once. Statements are in saved order."""
    wanted = list(dict.fromkeys(entity_ids))
    summaries: dict[str, CodexSummary] = {}
    for batch in _batches(wanted):
        for r in conn.execute(
                f"SELECT {_SUMMARY_COLS} FROM codex_summaries WHERE entity_id IN"
                f" ({','.join('?' * len(batch))})", batch):
            summaries[r[0]] = _summary(r)
    stated: dict[str, list[Statement]] = {}
    for batch in _batches(list(summaries)):
        for r in conn.execute(
                "SELECT entity_id, id, aspect, position, statement, quote,"
                " chunk_id FROM codex_entries WHERE entity_id IN"
                f" ({','.join('?' * len(batch))}) ORDER BY entity_id, position",
                batch):
            stated.setdefault(r[0], []).append(Statement(
                aspect=r[2], text=r[4], quote=r[5], chunk_id=r[6], id=r[1],
                position=r[3]))
    return [CodexEntry(
        entity_id=s.entity_id, world_id=s.world_id, name=s.name, kind=s.kind,
        summary=s.summary, statements=tuple(stated.get(s.entity_id, ())),
        prompt_hash=s.prompt_hash, evidence_hash=s.evidence_hash)
        for eid in wanted if (s := summaries.get(eid))]


def world_ids_with_codex(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT world_id FROM codex_summaries ORDER BY world_id")]


def count(conn: sqlite3.Connection, world_id: int) -> int:
    """Entries (entities with a profile) in the world."""
    return conn.execute("SELECT COUNT(*) FROM codex_summaries WHERE world_id=?",
                        (world_id,)).fetchone()[0]


def relink(conn: sqlite3.Connection, kuzu: _Kuzu, lance=None) -> int:
    """Follow the resolver. Returns the number of entries moved or dropped.

    - An entity that is now an alias stub (ALIAS_OF) hands its entry to the
      entity it points to (followed to the end of the chain). If that entity
      already has an entry, the stub's is dropped: the next build rewrites the
      survivor's from all of its evidence.
    - An entity that is no longer in the graph loses its entry.

    `lance` (a store with delete_codex), when given, loses the dropped
    statements' vectors. Ids, not names, decide: an entity name can recur in
    another world, so it says nothing about identity.
    """
    stubs = {a: c for a, c in kuzu.execute(
        "MATCH (a:Entity)-[:ALIAS_OF]->(c:Entity) RETURN a.id, c.id")}
    alive = {r[0] for r in kuzu.execute("MATCH (e:Entity) RETURN e.id")}
    held = {r[0] for r in conn.execute("SELECT entity_id FROM codex_summaries")}

    def target(entity_id: str) -> str | None:
        seen = {entity_id}
        while entity_id in stubs:
            entity_id = stubs[entity_id]
            if entity_id in seen:
                return None
            seen.add(entity_id)
        return entity_id if entity_id in alive else None

    moved = dropped = 0
    dropped_statements: list[str] = []
    for entity_id in sorted(held):
        if entity_id in alive and entity_id not in stubs:
            continue
        new = target(entity_id)
        if new is None or new in held:
            dropped_statements += delete_entities(conn, [entity_id])
            dropped += 1
            continue
        with conn:
            for table in ("codex_summaries", "codex_entries"):
                conn.execute(f"UPDATE {table} SET entity_id=? WHERE entity_id=?",
                             (new, entity_id))
        held.discard(entity_id)
        held.add(new)
        moved += 1
    if lance is not None and dropped_statements:
        lance.delete_codex(dropped_statements)
    return moved + dropped
