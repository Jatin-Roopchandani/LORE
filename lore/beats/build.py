"""Building story beats: the prompt, its answer schema and the code checks.

A beat is one quote-anchored moment about a character, faction or object
(see store.py). The model proposes beats for one chapter part; this module
decides which survive. Nothing is kept on the model's word: every beat's
quote must be found, by the claims' own matcher and normaliser, in one chunk
of the part, which is also the chunk the beat is pinned to.

Nothing here names a book, a person or a genre. The entity list, the book
title and the text arrive as data.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from lore.beats.store import KINDS, Beat, beat_id
from lore.ingest.models import Chunk
from lore.query.models import Claim, Passage
from lore.query.synthesize import Synthesis, _normalise, _words, verify_claims

# Up to this many characters of chapter text per request; a longer chapter is
# cut at chunk boundaries into parts, one request each.
MAX_PART_CHARS = 60_000
MAX_BEATS = 25
MAX_QUOTE_WORDS = 40
ENTITY_KINDS = ("Character", "Faction", "Object")
ENTITY_LIMIT = 12

BEATS_PROMPT = """You are reading part of one chapter of a book, to record its story beats. A beat is one moment that shows what a listed entity does, decides or feels, how it treats someone else, or how its situation changes.

Book: {book_title}
Position: {position}

Listed entities (name, kind, other names):
{entities}

Rules:
- Record up to 25 beats, in the order they happen in the text, spread over the whole text and not only its opening.
- Each beat is one moment about one listed entity, its subject, who comes first in "entities". Other listed entities that take part may follow it. Do not write a beat about anyone who is not listed, except as a later entry in "entities".
- Prefer moments that show an attitude or a change. Include moments shown through what someone does rather than stated outright: whom they follow, trust, avoid, obey or defy; what they record, keep, take or give up; how others react to them.
- "kind" is one of: action, decision, feeling, treatment (how the subject behaves toward someone), change (a change of state, status, allegiance or possession).
- "text" is one short sentence in neutral words saying what happens. It states only what the quote supports, with no interpretation and no guess at a motive.
- "quote" is copied word for word from the text below: one unbroken stretch of 4 to 40 words, with no ellipsis and no change of spelling or punctuation. It need not contain the entity's name.
- Use the listed names exactly as written.
- If nothing in the text fits, return an empty list.

Text:
{text}
"""


class BeatItem(BaseModel):
    entities: list[str]
    kind: str
    text: str
    quote: str


class BeatsAnswer(BaseModel):
    beats: list[BeatItem]


@dataclass(frozen=True)
class ChapterEntity:
    id: str
    name: str
    kind: str
    aliases: tuple[str, ...] = ()


class _Kuzu(Protocol):
    def execute(self, cypher: str, params: dict | None = None): ...


def prompt_hash() -> str:
    """Identifies the prompt text; a cached answer is valid only for its own."""
    return hashlib.sha256(BEATS_PROMPT.encode()).hexdigest()[:16]


def chapter_entities(kuzu: _Kuzu, book_id: str, chapter: int,
                     limit: int = ENTITY_LIMIT) -> list[ChapterEntity]:
    """The chapter's Character, Faction and Object entities, most mentioned
    first (by the number of the chapter's chunks that MENTION them)."""
    rows = kuzu.execute(
        "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)"
        " WHERE c.book_id = $book AND c.chapter_seq = $chapter"
        " AND e.kind IN $kinds"
        " RETURN e.id, e.name, e.kind, e.aliases, count(DISTINCT c) AS n"
        " ORDER BY n DESC, e.name LIMIT $limit",
        {"book": book_id, "chapter": chapter, "kinds": list(ENTITY_KINDS),
         "limit": limit})
    return [ChapterEntity(id=r[0], name=r[1], kind=r[2],
                          aliases=tuple(r[3] or ()))
            for r in rows]


def chapter_parts(chunks: Sequence[Chunk],
                  max_chars: int = MAX_PART_CHARS) -> list[list[Chunk]]:
    """`chunks` (one chapter, in order) cut at chunk boundaries into parts of
    at most `max_chars` characters. A chunk longer than that is a part alone."""
    parts: list[list[Chunk]] = []
    size = 0
    for chunk in chunks:
        if parts and size + len(chunk.text) <= max_chars:
            parts[-1].append(chunk)
            size += len(chunk.text)
        else:
            parts.append([chunk])
            size = len(chunk.text)
    return parts


def part_chapters(chunks: Iterable[Chunk]) -> dict[int, list[Chunk]]:
    """A book's chunks grouped by chapter, each in reading order."""
    grouped: dict[int, list[Chunk]] = defaultdict(list)
    for chunk in sorted(chunks, key=lambda c: (c.chapter_seq, c.seq)):
        grouped[chunk.chapter_seq].append(chunk)
    return dict(grouped)


def render_prompt(book_title: str, chapter: int, part: int, parts: int,
                  entities: Sequence[ChapterEntity],
                  chunks: Sequence[Chunk]) -> str:
    position = f"Chapter {chapter}"
    if parts > 1:
        position += f", part {part + 1} of {parts}"
    listing = "\n".join(
        f"- {e.name} ({e.kind})"
        + (f"; also called: {', '.join(e.aliases)}" if e.aliases else "")
        for e in entities)
    text = "\n\n".join(c.text for c in chunks)
    return BEATS_PROMPT.format(book_title=book_title, position=position,
                               entities=listing, text=text)


def verify_beats(raw: BeatsAnswer, book_id: str, chapter: int,
                 chunks: Sequence[Chunk],
                 entities: Sequence[ChapterEntity]) -> list[Beat]:
    """The beats of `raw` that pass the code checks, pinned to their chunk.

    Kept only if: the kind is known; the subject (first name) is a listed
    entity or one of its other names, other unlisted names being removed;
    the quote has MIN_QUOTE_WORDS to MAX_QUOTE_WORDS words and is found, by
    the claims matcher, in one chunk (the first that holds it); and it is not
    a repeat of an earlier beat by subject and quote. At most MAX_BEATS.
    """
    names: dict[str, str] = {}
    for own_names in (True, False):  # a primary name wins a clash
        for e in entities:
            for form in ([e.name] if own_names else e.aliases):
                if form.strip():
                    names.setdefault(form.strip().lower(), e.name)

    # (beat, resolved entity names), before the quote check.
    pending: list[tuple[BeatItem, tuple[str, ...]]] = []
    for item in raw.beats:
        if item.kind not in KINDS or not item.entities:
            continue
        subject = names.get(item.entities[0].strip().lower())
        if subject is None:
            continue
        resolved = [subject]
        for name in item.entities[1:]:
            known = names.get(name.strip().lower())
            if known is not None and known not in resolved:
                resolved.append(known)
        if _words(_normalise(item.quote)) > MAX_QUOTE_WORDS:
            continue
        pending.append((item, tuple(resolved)))

    # One claim per (beat, chunk): the matcher decides which chunk holds it.
    passages = [_as_passage(i, c) for i, c in enumerate(chunks, start=1)]
    claims: list[Claim] = []
    owner: dict[int, tuple[int, int]] = {}
    for b, (item, _names) in enumerate(pending):
        for passage in passages:
            claim = Claim(text="", passage=passage.n, quote=item.quote)
            owner[id(claim)] = (b, passage.n - 1)
            claims.append(claim)
    kept, _dropped = verify_claims(Synthesis(claims=claims, can_answer=True),
                                   passages)
    pinned: dict[int, int] = {}
    for claim in kept:
        b, ci = owner[id(claim)]
        pinned.setdefault(b, ci)

    out: list[Beat] = []
    seen: set[tuple[str, str]] = set()
    for b, (item, resolved) in enumerate(pending):
        if b not in pinned:
            continue
        key = (resolved[0].lower(), _normalise(item.quote))
        if key in seen:
            continue
        seen.add(key)
        out.append(Beat(
            id=beat_id(book_id, chapter, resolved, item.quote),
            book_id=book_id, chapter_seq=chapter,
            chunk_id=chunks[pinned[b]].id, entities=resolved, kind=item.kind,
            text=item.text.strip(), quote=item.quote.strip()))
        if len(out) == MAX_BEATS:
            break
    return out


def _as_passage(n: int, chunk: Chunk):
    return Passage(n=n, chunk_id=chunk.id, book_id=chunk.book_id,
                   chapter_seq=chunk.chapter_seq, text=chunk.text)


# --- the per-part answer cache ------------------------------------------------


def _text_hash(chunks: Sequence[Chunk]) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.id.encode() + b"\0" + c.text.encode() + b"\0")
    return h.hexdigest()[:16]


def cache_path(beats_dir: Path, book_id: str, chapter: int, part: int) -> Path:
    return beats_dir / book_id / f"{chapter}-{part}.json"


def read_cached(path: Path, chunks: Sequence[Chunk]) -> BeatsAnswer | None:
    """The saved answer, or None if absent, unreadable, or made for another
    prompt or other text (each file carries its own hashes)."""
    try:
        data = json.loads(path.read_text())
        if (data["prompt_hash"] != prompt_hash()
                or data["text_hash"] != _text_hash(chunks)):
            return None
        return BeatsAnswer.model_validate(data["answer"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_cached(path: Path, chunks: Sequence[Chunk], answer: BeatsAnswer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "prompt_hash": prompt_hash(), "text_hash": _text_hash(chunks),
        "answer": answer.model_dump()}))
    tmp.replace(path)
