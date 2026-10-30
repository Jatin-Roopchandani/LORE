"""Building the codex: the prompt, its answer schema and the code checks.

A codex entry is a quote-verified profile of one significant entity (see
store.py). The model proposes a summary and statements from one entity's
evidence pack; this module decides which statements survive. Nothing is kept
on the model's word: every statement's quote must be found, by the claims'
own matcher and normaliser, in one passage of the pack, which is also the
chunk the statement is pinned to.

The evidence pack is the entity's story beats (a guide to the moments that
matter) and numbered passages: the chunks of those beats and the world's
chunks that name the entity most, in story order. Only passages within the
entity's world are used.

Nothing here names a book, a person or a genre. The entity, its books and the
text arrive as data.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from lore.beats.store import Beat, beats_for_entities
from lore.codex.store import ASPECTS, CodexEntry, Statement
from lore.query.models import BookInfo, Claim, Passage
from lore.query.position import order_passages
from lore.query.synthesize import Synthesis, _normalise, _words, verify_claims
from lore.query.timeline import coverage_order, order_beats

# At most this many entries per book in the world (so a world of three books
# may hold three times as many).
CODEX_MAX_PER_BOOK = 150
# An entity is significant when this many chunks of its world mention it.
MIN_MENTION_CHUNKS = 4
ENTITY_KINDS = ("Concept", "Faction", "Place", "Object", "Character")
MAX_BEATS = 24
MAX_PASSAGES = 30
MAX_STATEMENTS = 40
MAX_QUOTE_WORDS = 40
MAX_SUMMARY_CHARS = 240

CODEX_PROMPT = """You are writing a reference entry about one {kind} in a story world, from numbered passages of its books. The entry records what the books establish about it, so that a reader can look it up later. It never interprets.

Subject: {name} ({kind}){aliases}

Moments already noted about it, in story order (a guide to where to look; they are not evidence):
{moments}

Passages, in story order:
{passages}

Rules:
- "summary" is one plain sentence of at most 40 words saying what the subject is and what part it plays in the story.
- "statements" has up to 40 items. Each is one fact that a passage establishes about the subject, under one "aspect": nature (what it is: its look, kind, qualities, status), history (where it came from and what happened to it), abilities (what it can do, or how it works and what its rules are), relationships (how it stands to people, groups, places or things), role (what it does in the events), change (how it differs later from earlier: say what it was and what it became).
- Cover every aspect the passages give evidence for, and cover the whole span of the passages, not only their opening. A statement that adds something is better than one that repeats another.
- A statement says only what its quote supports, in neutral words: no opinion, no guess at a motive, no theme, no verdict.
- "quote" is copied word for word from one passage: one unbroken stretch of 4 to 40 words, with no ellipsis and no change of spelling or punctuation. It need not contain the subject's name, but the passage must make clear that it is about the subject.
- Use nothing outside the passages. If they establish little, return few statements; if nothing, an empty list.
"""


class StatementItem(BaseModel):
    aspect: str
    text: str
    quote: str


class CodexAnswer(BaseModel):
    summary: str
    statements: list[StatementItem]


@dataclass(frozen=True)
class CodexEntity:
    id: str
    name: str
    kind: str
    aliases: tuple[str, ...] = ()
    mentions: int = 0  # chunks of the world that mention it


@dataclass(frozen=True)
class Evidence:
    """One entity's pack: `beats` in story order (at most MAX_BEATS) and
    `passages` in story order, numbered from 1, with a `labels` line each."""
    beats: tuple[Beat, ...]
    passages: tuple[Passage, ...]
    labels: tuple[str, ...]


class _Kuzu(Protocol):
    def execute(self, cypher: str, params: dict | None = None): ...


def prompt_hash() -> str:
    """Identifies the prompt text; a cached answer is valid only for its own."""
    return hashlib.sha256(CODEX_PROMPT.encode()).hexdigest()[:16]


def significant_entities(kuzu: _Kuzu, book_ids: Sequence[str], limit: int,
                         min_chunks: int | None = None) -> list[CodexEntity]:
    """The world's Concept, Faction, Place, Object and Character entities that
    at least `min_chunks` of its chunks mention, most mentioned first, at most
    `limit`. `book_ids` are the world's books. `min_chunks` defaults to
    MIN_MENTION_CHUNKS."""
    if not book_ids or limit <= 0:
        return []
    rows = kuzu.execute(
        "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)"
        " WHERE c.book_id IN $books AND e.kind IN $kinds"
        " WITH e, count(DISTINCT c) AS n WHERE n >= $min"
        " RETURN e.id, e.name, e.kind, e.aliases, n"
        " ORDER BY n DESC, e.name, e.id LIMIT $limit",
        {"books": sorted(book_ids), "kinds": list(ENTITY_KINDS),
         "min": MIN_MENTION_CHUNKS if min_chunks is None else min_chunks,
         "limit": limit})
    return [CodexEntity(id=r[0], name=r[1], kind=r[2],
                        aliases=tuple(r[3] or ()), mentions=int(r[4]))
            for r in rows]


def _occurrences(text: str, forms: Sequence[str]) -> int:
    low = text.lower()
    return sum(low.count(f.lower()) for f in forms if f.strip())


def evidence_pack(conn: sqlite3.Connection, kuzu: _Kuzu, entity: CodexEntity,
                  book_ids: Sequence[str],
                  books: Mapping[str, BookInfo] | None) -> Evidence:
    """The entity's beats and passages within `book_ids`, in story order.

    Beats: all of its beats in the world, or MAX_BEATS spread evenly over the
    story. Passages: the chunks of those beats first, then the chunks that
    name the entity most (by how often its name or aliases occur), up to
    MAX_PASSAGES. Passage order follows `position.order_passages`, so two
    books are ordered only when their series says how.
    """
    world = set(book_ids)
    beats = order_beats([b for b in beats_for_entities(conn, [entity.id])
                         if b.book_id in world], books)
    if len(beats) > MAX_BEATS:
        keep = sorted(coverage_order(len(beats))[:MAX_BEATS])
        beats = [beats[i] for i in keep]

    mention_ids = [r[0] for r in kuzu.execute(
        "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity {id: $id})"
        " WHERE c.book_id IN $books RETURN DISTINCT c.id",
        {"id": entity.id, "books": sorted(world)})]
    beat_chunks = list(dict.fromkeys(b.chunk_id for b in beats))
    rows = _chunk_rows(conn, sorted(set(mention_ids) | set(beat_chunks)))
    texts = {cid: r[2] for cid, r in rows.items()}
    forms = [entity.name, *entity.aliases]
    chosen = [c for c in beat_chunks if c in texts][:MAX_PASSAGES]
    rest = sorted((c for c in set(mention_ids) if c in texts and c not in chosen),
                  key=lambda c: (-_occurrences(texts[c], forms), c))
    chosen += rest[:MAX_PASSAGES - len(chosen)]

    unordered = [Passage(n=i, chunk_id=cid, book_id=rows[cid][0],
                        chapter_seq=rows[cid][1], text=texts[cid]) for i, cid in enumerate(chosen)]
    order = order_passages(unordered, books)
    by_n = {p.n: p for p in unordered}
    passages, labels = [], []
    for number, pos in enumerate(order.ordered, start=1):
        p = by_n[pos.n]
        passages.append(Passage(n=number, chunk_id=p.chunk_id, book_id=p.book_id,
                                chapter_seq=p.chapter_seq, text=p.text))
        labels.append(pos.label())
    return Evidence(beats=tuple(beats), passages=tuple(passages),
                    labels=tuple(labels))


def _chunk_rows(conn: sqlite3.Connection,
                ids: Sequence[str]) -> dict[str, tuple[str, int, str]]:
    """chunk id -> (book id, chapter, text); an unknown id is absent."""
    out: dict[str, tuple[str, int, str]] = {}
    for start in range(0, len(ids), 500):
        batch = list(ids[start:start + 500])
        for r in conn.execute(
                "SELECT id, book_id, chapter_seq, text FROM chunks WHERE id IN"
                f" ({','.join('?' * len(batch))})", batch):
            out[r[0]] = (r[1], r[2], r[3])
    return out


def evidence_hash(entity: CodexEntity, evidence: Evidence) -> str:
    """Identifies what the prompt shows about the entity (not the template)."""
    payload = json.dumps([
        entity.name, entity.kind, list(entity.aliases),
        [b.text for b in evidence.beats],
        [[p.chunk_id, label, p.text]
         for p, label in zip(evidence.passages, evidence.labels)]])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def render_prompt(entity: CodexEntity, evidence: Evidence) -> str:
    aliases = f"; also called: {', '.join(entity.aliases)}" if entity.aliases else ""
    moments = "\n".join(f"- {b.text}" for b in evidence.beats) or "(none)"
    passages = "\n\n".join(
        f"[{p.n}] {label}\n{p.text}"
        for p, label in zip(evidence.passages, evidence.labels))
    return CODEX_PROMPT.format(kind=entity.kind, name=entity.name,
                               aliases=aliases, moments=moments,
                               passages=passages)


def verify_entry(raw: CodexAnswer, entity: CodexEntity, world_id: int,
                 evidence: Evidence, evidence_hash_: str) -> CodexEntry | None:
    """The entry for `raw`, or None when no statement survives.

    A statement is kept only if its aspect is known, its text is not empty,
    its quote has at most MAX_QUOTE_WORDS words and is found, by the claims
    matcher (which also enforces the minimum length), in one passage of the
    pack, the first that holds it, which is the chunk it is pinned to; a
    repeat of an earlier statement's aspect and quote is dropped. Statements
    are ordered by aspect, then story order, then the model's order, at most
    MAX_STATEMENTS. The summary is one line of at most MAX_SUMMARY_CHARS
    characters, or the first kept statement when the model gave none.
    """
    pending = [s for s in raw.statements
               if s.aspect in ASPECTS and s.text.strip() and s.quote.strip()
               and _words(_normalise(s.quote)) <= MAX_QUOTE_WORDS]
    # One claim per (statement, passage): the matcher decides which passage
    # holds the quote.
    claims: list[Claim] = []
    owner: dict[int, tuple[int, int]] = {}
    for si, item in enumerate(pending):
        for pi, passage in enumerate(evidence.passages):
            claim = Claim(text="", passage=passage.n, quote=item.quote)
            owner[id(claim)] = (si, pi)
            claims.append(claim)
    kept, _dropped = verify_claims(
        Synthesis(claims=claims, can_answer=True), evidence.passages)
    pinned: dict[int, int] = {}
    for claim in kept:
        si, pi = owner[id(claim)]
        pinned.setdefault(si, pi)

    chosen: list[tuple[int, int, int, StatementItem]] = []
    seen: set[tuple[str, str]] = set()
    for si, item in enumerate(pending):
        if si not in pinned:
            continue
        key = (item.aspect, _normalise(item.quote))
        if key in seen:
            continue
        seen.add(key)
        chosen.append((ASPECTS.index(item.aspect), pinned[si], si, item))
    chosen.sort(key=lambda t: t[:3])
    statements = tuple(
        Statement(aspect=item.aspect, text=" ".join(item.text.split()),
                  quote=item.quote.strip(),
                  chunk_id=evidence.passages[pi].chunk_id)
        for _a, pi, _s, item in chosen[:MAX_STATEMENTS])
    if not statements:
        return None
    summary = " ".join(raw.summary.split())[:MAX_SUMMARY_CHARS].rstrip()
    return CodexEntry(
        entity_id=entity.id, world_id=world_id, name=entity.name,
        kind=entity.kind, summary=summary or statements[0].text,
        statements=statements, prompt_hash=prompt_hash(),
        evidence_hash=evidence_hash_)


# --- the per-entity answer cache ------------------------------------------------


def cache_path(codex_dir: Path, entity_id: str, evidence_hash_: str) -> Path:
    """One file per (entity, evidence): an entity whose evidence changes and
    then changes back (a book moved out and in again) finds its answer."""
    return codex_dir / f"{entity_id}-{evidence_hash_}.json"


def read_cached(path: Path, evidence_hash_: str) -> CodexAnswer | None:
    """The saved answer, or None if absent, unreadable, or made for another
    prompt or other evidence (each file carries its own hashes)."""
    try:
        data = json.loads(path.read_text())
        if (data["prompt_hash"] != prompt_hash()
                or data["evidence_hash"] != evidence_hash_):
            return None
        return CodexAnswer.model_validate(data["answer"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_cached(path: Path, evidence_hash_: str, answer: CodexAnswer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "prompt_hash": prompt_hash(), "evidence_hash": evidence_hash_,
        "answer": answer.model_dump()}))
    tmp.replace(path)
