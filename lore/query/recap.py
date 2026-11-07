"""Recap answers: what happens over a stretch of the story, told in order.

"Summarise the story so far", "what happened in book 2", "recap chapters 3 to
6", "what has happened since X left": such a question names no character, and
seed-entity retrieval cannot cover a narrative span. The decomposer marks it as
a recap and records the stretch it asks about (decompose.Recap): the whole
visible story, one book, a range of chapters, or everything since an event.

Retrieval reads the stretch itself, not a neighbourhood of the question:
- every chunk of the span the reader may see (the scope's books, through the
  one visibility predicate, visible.sees), story text only (no licence or
  table of contents), in story order (position.order_passages);
- every beat of those chapters (lore/beats), in story order (timeline.order_beats),
  near-duplicates of one moment dropped.
The span is cut into at most RECAP_MAX_SECTIONS stretches of consecutive
chapters, one book at a time, balanced by how much text each chapter holds. A
stretch shows up to RECAP_SECTION_BEATS beats; past that, an even spread across
its chapters (timeline.coverage_order within each), so every chapter is
represented. Its passages are its beats' passages, plus a few passages spread
over any chapter with few beats or none (THIN_CHAPTER_PASSAGES), so a book with
no beats at all is recapped from passages spread evenly across the span. The
passage budget (RECAP_SECTION_CHARS) is shared round-robin across the chapters.

Each stretch is then a section, as in a deep answer (deep.py): its own claims
step (quotes verified, faithfulness checked; a claim restating an earlier
section's fact is dropped), and its own writer, asked for a chronological
narrative with transitions and no interpretation (write.RECAP_LENGTH_RULE). The
answer opens with a one- or two-sentence gist of the whole span, written after
the sections from their claims and checked like every prose. A span over
several books groups its sections under the book titles.

Faithfulness is as everywhere else: beats are a map of where the passages are,
never evidence. Nothing here knows any library or any question.
"""
from __future__ import annotations

from lore.ingest.models import chunk_seq

import asyncio
import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

from lore.beats.store import Beat, beats_for_books
from lore.extract.embed import QUERY_PREFIX
from lore.ingest.registry import story_chapters
from lore.query import deep as deep_module
from lore.query import visible as visible_module
from lore.query.decompose import Recap
from lore.query.events import EDGES_PER_PART_CAP, edge_key, emit
from lore.query.models import (
    Answer, AnswerSection, BookInfo, Claim, Evidence, Passage, ProseSentence, SectionSpec,
    TemplateCall,
)
from lore.query.position import order_passages
from lore.query.retrieval import books_named, search_chunks
from lore.query.scope import Scope
from lore.query.seed import _entities
from lore.query.subgraph import run_template
from lore.query.synthesize import CHECK_SKIPPED_NOTE, _compose, _content_words, synthesize
from lore.query.timeline import coverage_order, order_beats, timeline_beats
from lore.query.write import SHORT_ANSWER_HEADING, compose_text
from lore.resolve.entityhood import is_boilerplate_chunk

log = logging.getLogger(__name__)

# How many sections a recap may have, and how much text (in passages) one
# section spans before the span is cut into more: a short span is one section
# per chapter or two, a whole book RECAP_MAX_SECTIONS. A section costs three
# requests (claims, writer, check), so a whole-book recap is about 8 * 3 + 2.
RECAP_MAX_SECTIONS = 8
SECTION_CHUNKS = 16
# A section's own budget: the claims it may keep, the characters of passages it
# reads, and the beats it is shown (past that, an even spread across its chapters).
RECAP_SECTION_CLAIMS = 12
RECAP_SECTION_CHARS = 18_000
RECAP_SECTION_BEATS = 24
# A chapter with fewer beats than this gets passages to make up the difference,
# spread over the chapter: three for a chapter with no beats.
THIN_CHAPTER_PASSAGES = 3
# Requests: the same ceiling as a deep answer, with room for the short answer.
RECAP_MAX_REQUESTS = deep_module.DEEP_MAX_REQUESTS
# Sections drafted at once. Each reads only its own chapters, so drafting them
# together changes nothing they see; they are still numbered and shown in story
# order. Three keep well inside a free key's requests per minute.
RECAP_CONCURRENCY = 3
# The short answer: the gist, at most this many sentences, from at most this many
# claims spread over the sections.
SHORT_SENTENCES = 2
SHORT_CLAIMS = 40
# Two beats of one passage whose texts share this share of their content words
# tell one moment: the later is dropped.
SAME_BEAT_OVERLAP = 0.6

# Said when the stretch asked about holds nothing the reader may see.
NOTHING_VISIBLE = "Nothing in that part of the story is within what you have read."

_ORDINALS = {w: i for i, w in enumerate(
    "first second third fourth fifth sixth seventh eighth ninth tenth".split(), start=1)}
_NUMBERS = {w: i for i, w in enumerate(
    "one two three four five six seven eight nine ten".split(), start=1)}


# --- the span ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    book_id: str
    chapter: int
    text: str


@dataclass(frozen=True)
class Chapter:
    """One chapter of the span: its story-text chunks in story order."""

    book_id: str
    chapter: int
    chunks: tuple[Chunk, ...]

    @property
    def key(self) -> tuple[str, int]:
        return (self.book_id, self.chapter)


@dataclass(frozen=True)
class Span:
    """What a recap reads: the span's chapters and beats, both in story order.
    `requested` is set when the question named a book or chapters that hold
    nothing the reader may see."""

    chapters: tuple[Chapter, ...]
    beats: tuple[Beat, ...]
    order_note: str = ""
    requested: bool = False
    # (book, chapter_seq) -> the chapter's number as the book counts it (front
    # matter is not a chapter; lore/ingest/matter.py), for the section headings.
    numbers: Mapping[tuple[str, int], int] = field(default_factory=dict)


def chapter_numbers(conn: sqlite3.Connection,
                    book_ids: Sequence[str]) -> dict[tuple[str, int], int]:
    """(book, chapter_seq) -> the book's own chapter number, story chapters only
    (every chapter, numbered in order, for a book not classified)."""
    return {(b, c["seq"]): c["number"] for b in sorted(set(book_ids))
            for c in story_chapters(conn, b)}


def chapter_names(conn: sqlite3.Connection,
                  book_ids: Sequence[str]) -> dict[tuple[str, int], str]:
    """(book, chapter_seq) -> the chapter's own heading, where the book has one."""
    return {(b, c["seq"]): c["heading"] for b in sorted(set(book_ids))
            for c in story_chapters(conn, b) if c.get("heading")}


_seq = chunk_seq  # a chunk's numeric position in its book


def _number(text: str) -> int | None:
    """A book's number as asked: "2", "book 2", "second", "book two"."""
    for word in re.findall(r"\w+", text.casefold()):
        if word.isdigit():
            return int(word)
        if word in _ORDINALS:
            return _ORDINALS[word]
        if word in _NUMBERS:
            return _NUMBERS[word]
    return None


def resolve_books(text: str, titles: Mapping[str, str],
                  infos: Mapping[str, BookInfo]) -> list[str]:
    """The books `text` names among `titles` (id -> title): by title, else by
    number in their series (a series index, never a guess from titles or ids)."""
    if not text:
        return []
    named = books_named(text, titles)
    if named:
        return named
    n = _number(text)
    if n is not None:
        numbered = [b for b in titles if (infos.get(b) or BookInfo(title=b)).series
                    and (infos.get(b) or BookInfo(title=b)).series_index == n]
        if numbered:
            return numbered
    folded = " ".join(text.casefold().split())
    if len(folded) < 4:  # too short to stand for a title
        return []
    return [b for b, t in titles.items()
            if t and (folded in t.casefold() or t.casefold() in folded)]


def _not_story(conn: sqlite3.Connection, book_ids: Sequence[str]) -> set[tuple[str, int]]:
    """(book, chapter_seq) of the sections classified as front or back matter, when
    the library has that classification (a `sections` table); none otherwise."""
    try:
        rows = conn.execute("SELECT book_id, chapter_seq FROM sections WHERE matter != 'story'")
    except sqlite3.OperationalError:  # a library without the classification
        return set()
    wanted = set(book_ids)
    return {(str(b), int(c)) for b, c in rows if b in wanted}


def _load_chunks(conn: sqlite3.Connection, book_ids: Sequence[str], visible) -> list[Chunk]:
    """Every story chunk of these books that `visible` lets the reader see: no
    licence or table of contents, and no section classified as front or back matter."""
    out: list[Chunk] = []
    ids = sorted(set(book_ids))
    matter = _not_story(conn, ids)
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        for chunk_id, book_id, chapter, text in conn.execute(
                "SELECT id, book_id, chapter_seq, text FROM chunks WHERE book_id IN"
                f" ({','.join('?' * len(batch))})", batch):
            if visible_module.sees(visible, book_id, int(chapter)) \
                    and (str(book_id), int(chapter)) not in matter \
                    and not is_boilerplate_chunk(text or ""):
                out.append(Chunk(str(chunk_id), str(book_id), int(chapter), text or ""))
    return out


def _ordered(chunks: Sequence[Chunk], books: Mapping[str, BookInfo] | None):
    """The chunks in story order, and the line saying which books' order is unknown."""
    passages = [Passage(n=i, chunk_id=c.chunk_id, book_id=c.book_id, chapter_seq=c.chapter,
                        text="") for i, c in enumerate(chunks)]
    order = order_passages(passages, books)
    return [chunks[p.n] for p in order.ordered], order.note()


def dedupe_beats(beats: Sequence[Beat]) -> list[Beat]:
    """The beats without restatements: a beat of the same passage whose text
    shares SAME_BEAT_OVERLAP of its content words with an earlier one is dropped."""
    kept: list[Beat] = []
    words: list[frozenset[str]] = []
    for b in beats:
        mine = _content_words(b.text)
        if any(k.chunk_id == b.chunk_id and mine and w
               and len(mine & w) / len(mine | w) >= SAME_BEAT_OVERLAP
               for k, w in zip(kept, words)):
            continue
        kept.append(b)
        words.append(mine)
    return kept


async def gather_span(conn: sqlite3.Connection, lance, recap: Recap, *, scope: Scope,
                      question: str, titles: Mapping[str, str],
                      books: Mapping[str, BookInfo], embed=None,
                      book_ids: Sequence[str] | None = None) -> Span:
    """The chapters and beats of the stretch `recap` asks about, within what the
    scope lets the reader see. `book_ids` (books the question names) narrows the
    span to them; `embed` finds a "since" span's event (it is skipped without one)."""
    in_scope = {b: t for b, t in titles.items() if scope.allows(b)}
    candidates = list(book_ids) if book_ids else list(in_scope)
    requested = False
    if recap.span in ("book", "chapters") and recap.book:
        asked = resolve_books(recap.book, in_scope, books)
        if asked:
            candidates, requested = asked, True
        elif resolve_books(recap.book, titles, books):
            # A book of the library that is outside what the reader may see.
            return Span((), (), requested=True)
    chunks = _load_chunks(conn, candidates, scope.books)
    ordered, note = _ordered(chunks, books)
    numbers = chapter_numbers(conn, sorted({c.book_id for c in ordered}))
    if recap.span == "chapters":
        # "Chapters 3 to 5" are the book's own chapter numbers, not raw sections.
        book = _chapters_book(ordered, scope, requested)
        first, last = recap.first or 0, recap.last if recap.last is not None else 10 ** 9
        ordered = [c for c in ordered if c.book_id == book
                   and first <= numbers.get((c.book_id, c.chapter), c.chapter) <= last]
        requested = True
    elif recap.span == "since" and ordered:
        anchor = await _anchor(lance, recap.since, ordered, conn, scope, embed)
        if anchor is not None:
            ordered = _since(ordered, anchor, books)
    chapters = _chapters(ordered)
    keys = {c.key for c in chapters}
    shown = {c.chunk_id for c in ordered}
    try:
        beats = beats_for_books(conn, sorted({c.book_id for c in ordered}))
    except sqlite3.OperationalError:  # a library not migrated to beats yet
        beats = []
    beats = [b for b in beats if (b.book_id, b.chapter_seq) in keys and b.chunk_id in shown
             and visible_module.sees(scope.books, b.book_id, b.chapter_seq)]
    beats = dedupe_beats(order_beats(beats, books))
    return Span(tuple(chapters), tuple(beats), note, requested and not chapters,
                {c.key: numbers[c.key] for c in chapters if c.key in numbers})


def _chapters_book(ordered: Sequence[Chunk], scope: Scope, requested: bool) -> str | None:
    """The book a chapter range is read in: the one asked for, else the book of the
    reading position, else the first in story order."""
    present = list(dict.fromkeys(c.book_id for c in ordered))
    if not present:
        return None
    if not requested and scope.position is not None and scope.position.book_id in present:
        return scope.position.book_id
    return present[0]


async def _anchor(lance, since: str, ordered: Sequence[Chunk], conn, scope: Scope,
                  embed) -> Chunk | None:
    """The chunk where a "since" span's event happens: the beat nearest the event's
    words among the span's beats, else the nearest visible chunk."""
    if embed is None or not since:
        return None
    vector = (await embed([QUERY_PREFIX + since]))[0]
    by_id = {c.chunk_id: c for c in ordered}
    try:
        beats = [b for b in beats_for_books(conn, sorted({c.book_id for c in ordered}))
                 if b.chunk_id in by_id]
    except sqlite3.OperationalError:
        beats = []
    rows = []
    if beats and hasattr(lance, "search_beats"):
        try:
            rows = lance.search_beats(vector, [b.id for b in beats], 1)
        except Exception:  # noqa: BLE001 -- no beat vectors (yet): the nearest chunk will do
            log.warning("beat search failed; the recap's start is the nearest passage")
    chunk_of = {b.id: b.chunk_id for b in beats}
    for row in rows:
        hit = by_id.get(chunk_of.get(str(row.get("id")), ""))
        if hit is not None:
            return hit
    books = sorted({c.book_id for c in ordered})
    for row in search_chunks(lance, vector, 5, book_ids=visible_module.restrict(scope.books, books)):
        hit = by_id.get(str(row.get("chunk_id")))
        if hit is not None:
            return hit
    return None


def _since(ordered: Sequence[Chunk], anchor: Chunk,
           books: Mapping[str, BookInfo]) -> list[Chunk]:
    """The chunks from `anchor` on: the rest of its book, and the books known to
    come after it in its series. A book whose order against it is unknown is left
    out: "after" is never guessed."""
    relation = visible_module.order_against(books, {c.book_id for c in ordered}, anchor.book_id)
    out = []
    for c in ordered:
        if c.book_id == anchor.book_id:
            if (c.chapter, _seq(c.chunk_id)) >= (anchor.chapter, _seq(anchor.chunk_id)):
                out.append(c)
        elif relation.get(c.book_id) == "after":
            out.append(c)
    return out


def _chapters(ordered: Sequence[Chunk]) -> list[Chapter]:
    groups: dict[tuple[str, int], list[Chunk]] = {}
    for c in ordered:
        groups.setdefault((c.book_id, c.chapter), []).append(c)
    return [Chapter(b, ch, tuple(cs)) for (b, ch), cs in groups.items()]


# --- the sections -----------------------------------------------------------------------

@dataclass(frozen=True)
class Stretch:
    """One section of a recap: consecutive chapters of one book."""

    book_id: str
    chapters: tuple[Chapter, ...]
    heading: str
    focus: str
    group: str = ""


def balanced_split(weights: Sequence[int], k: int) -> list[list[int]]:
    """Indices 0..n-1 cut into at most `k` consecutive, non-empty groups of about
    equal total weight."""
    n = len(weights)
    k = max(1, min(k, n))
    total = sum(max(w, 1) for w in weights)
    groups: list[list[int]] = []
    current: list[int] = []
    acc = 0
    for j, w in enumerate(weights):
        current.append(j)
        acc += max(w, 1)
        left_chapters, left_groups = n - j - 1, k - len(groups) - 1
        if left_groups > 0 and left_chapters > 0 and (
                acc >= total * (len(groups) + 1) / k or left_chapters == left_groups):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def chapter_label(chapters: Sequence[int], name: str | None = None) -> str:
    """"Chapter 4", "Chapter 4 · The Name" when one named chapter, or
    "Chapters 3–6" for several (first to last)."""
    first, last = min(chapters), max(chapters)
    if first != last:
        return f"Chapters {first}–{last}"
    return f"Chapter {first} · {name}" if name else f"Chapter {first}"


def plan_stretches(chapters: Sequence[Chapter], titles: Mapping[str, str],
                   max_sections: int = RECAP_MAX_SECTIONS,
                   per_section: int = SECTION_CHUNKS,
                   numbers: Mapping[tuple[str, int], int] | None = None,
                   names: Mapping[tuple[str, int], str] | None = None) -> list[Stretch]:
    """The span's sections, in story order. Each holds consecutive chapters of
    one book; their number grows with the span's text (one per `per_section`
    chunks, at most `max_sections`, at least one per book), and they are shared
    among the books by how much text each holds. A span over several books
    groups its sections under the book titles. Headings use the book's own
    chapter numbers (`numbers`, from `Span.numbers`), else the raw chapter_seq."""
    numbers, names = numbers or {}, names or {}
    by_book: dict[str, list[Chapter]] = {}
    for c in chapters:
        by_book.setdefault(c.book_id, []).append(c)
    if not by_book:
        return []
    weight = {b: sum(len(c.chunks) for c in cs) for b, cs in by_book.items()}
    total = sum(weight.values())
    n = max(len(by_book), min(max_sections, math.ceil(total / max(per_section, 1))))
    share = {b: max(1, math.floor(n * weight[b] / max(total, 1))) for b in by_book}
    spare = n - sum(share.values())
    for b in sorted(by_book, key=lambda b: -(n * weight[b] / max(total, 1) - share[b])):
        if spare <= 0:
            break
        share[b] += 1
        spare -= 1
    multi = len(by_book) > 1
    out: list[Stretch] = []
    for book_id, mine in by_book.items():
        title = titles.get(book_id) or book_id
        for group in balanced_split([len(c.chunks) for c in mine], share[book_id]):
            members = tuple(mine[i] for i in group)
            plain = chapter_label([numbers.get(c.key, c.chapter) for c in members])
            label = chapter_label([numbers.get(c.key, c.chapter) for c in members],
                                  names.get(members[0].key) if len(members) == 1 else None)
            out.append(Stretch(book_id, members, label,
                               f"{plain[0].lower()}{plain[1:]} of {title}",
                               group=title if multi else ""))
    return out


def spread_by_chapter(beats: Sequence[Beat], budget: int) -> list[Beat]:
    """At most `budget` of `beats` (in story order), spread evenly across their
    chapters: each chapter gets a fair share (one each before any gets two),
    taken along the chapter by `coverage_order`; with fewer slots than chapters,
    chapters are chosen by `coverage_order` too. Story order is kept."""
    if len(beats) <= budget:
        return list(beats)
    if budget <= 0:
        return []
    groups: dict[tuple[str, int], list[int]] = {}
    for i, b in enumerate(beats):
        groups.setdefault((b.book_id, b.chapter_seq), []).append(i)
    keys = list(groups)
    if budget < len(keys):
        keys = [keys[i] for i in sorted(coverage_order(len(keys))[:budget])]
    quota = {k: 0 for k in keys}
    left = budget
    turn = [keys[i] for i in coverage_order(len(keys))]
    while left > 0:
        moved = False
        for k in turn:
            if left > 0 and quota[k] < len(groups[k]):
                quota[k] += 1
                left -= 1
                moved = True
        if not moved:
            break
    chosen = {groups[k][j] for k in keys for j in coverage_order(len(groups[k]))[:quota[k]]}
    return [beats[i] for i in sorted(chosen)]


def section_evidence(stretch: Stretch, beats: Sequence[Beat],
                     max_chars: int = RECAP_SECTION_CHARS) -> tuple[Evidence, list[Beat]]:
    """A section's passages and the beats shown with them. Each chapter offers its
    chosen beats' passages (spread along the chapter), then, when it has fewer than
    THIN_CHAPTER_PASSAGES beats, passages spread over the chapter to make up the
    difference. Chapters take turns, in story order, until `max_chars` is spent, so
    every chapter is represented before any gets more. Passages are numbered in
    story order."""
    keys = {c.key for c in stretch.chapters}
    mine = [b for b in beats if (b.book_id, b.chapter_seq) in keys]
    chosen = spread_by_chapter(mine, RECAP_SECTION_BEATS)
    offers: list[list[Chunk]] = []
    for chapter in stretch.chapters:
        by_id = {c.chunk_id: c for c in chapter.chunks}
        own = [b for b in chosen if (b.book_id, b.chapter_seq) == chapter.key]
        offer: list[Chunk] = []
        for i in coverage_order(len(own)):
            hit = by_id.get(own[i].chunk_id)
            if hit is not None and hit not in offer:
                offer.append(hit)
        fill = max(0, THIN_CHAPTER_PASSAGES - len(own))
        rest = [c for c in chapter.chunks if c not in offer]
        offer += [rest[i] for i in coverage_order(len(rest))[:fill]]
        offers.append(offer)
    taken: list[Chunk] = []
    used = 0
    truncated = False
    for turn in range(max((len(o) for o in offers), default=0)):
        for offer in offers:
            if turn < len(offer):
                if used + len(offer[turn].text) <= max_chars:
                    taken.append(offer[turn])
                    used += len(offer[turn].text)
                else:
                    truncated = True
    rank = {c.chunk_id: i for i, c in enumerate(ch for chapter in stretch.chapters
                                                 for ch in chapter.chunks)}
    taken.sort(key=lambda c: rank[c.chunk_id])
    passages = tuple(Passage(n=i, chunk_id=c.chunk_id, book_id=c.book_id, chapter_seq=c.chapter,
                             text=c.text) for i, c in enumerate(taken, start=1))
    kept = {p.chunk_id for p in passages}
    shown = [b for b in chosen if b.chunk_id in kept]
    line = timeline_beats(shown)
    return Evidence(edges=(), passages=passages, truncated=truncated, timeline=line,
                    timelines=(line,) if line else ()), shown


# --- the answer -------------------------------------------------------------------------

async def short_gist(question: str, sections: Sequence[AnswerSection], claims: Sequence[Claim],
                     passages: Sequence[Passage], models, *,
                     book_titles: Mapping[str, str] | None = None,
                     books: Mapping[str, BookInfo] | None = None) -> tuple[ProseSentence, ...]:
    """The recap's short answer: one or two checked sentences that give the gist of
    the whole span, from claims spread over every section; code's "no direct
    summary" line when none survives."""
    picked = deep_module._spread(sections, SHORT_CLAIMS)
    numbers = [k for _, k in picked]
    # The gist must reach the end of the stretch, not stop at its first chapters:
    # the writer is told where the stretch ends, and a longer recap gets a third sentence.
    sentences = SHORT_SENTENCES + (1 if len(sections) >= 4 else 0)
    ask = (f"{question} (The gist must run from the start of the stretch to where things "
           f"stand at its end, in {sections[-1].heading}, in at most {sentences} sentences.)"
           if sections else question)
    try:
        got = await deep_module.cited_prose(
            ask, [ask], [claims[k - 1] for k in numbers], passages, models,
            length="recap_short", book_titles=dict(book_titles or {}), books=dict(books or {}))
    except Exception:  # noqa: BLE001 -- the sections are written; the gist is an extra
        log.exception("the recap's short answer failed")
        got = None
    chosen = [s for s in got or () if s.kind == "fact"][:sentences]
    if not chosen:
        return (ProseSentence(text=deep_module.NO_SUMMARY_LINE, claims=(), passages=(),
                              new_paragraph=True, kind="limit", short=True),)
    return tuple(replace(s, claims=tuple(numbers[k - 1] for k in s.claims),
                         new_paragraph=i == 0, short=True) for i, s in enumerate(chosen))


def compose_recap(sections: Sequence[AnswerSection], short: Sequence[ProseSentence]) -> str:
    """The recap as text: the short answer, then each section under its heading,
    and, over several books, each book's title before its first section."""
    blocks = [SHORT_ANSWER_HEADING + "\n" + compose_text(short)] if short else []
    last = None
    for s in sections:
        if s.group and s.group != last:
            blocks.append(f"# {s.group}")
        last = s.group
        blocks.append(f"## {s.heading}\n\n{s.text}")
    return "\n\n".join(blocks)


async def draft_stretch(stretch: Stretch, span: Span, *, question: str, models,
                        titles: Mapping[str, str], books: Mapping[str, BookInfo],
                        registry: deep_module.Registry, covered: Sequence[str]):
    """One section's checked claims and prose, before it is numbered: (answer,
    evidence), or (None, evidence) when nothing was found. Drafts of different
    sections may run at once: each reads only its own chapters."""
    evidence, _shown = section_evidence(stretch, span.beats,
                                        min(models.evidence_chars, RECAP_SECTION_CHARS))
    if not evidence.passages:
        return None, evidence
    unseen = deep_module._unseen(registry)
    spec = SectionSpec(part_of=question, max_claims=RECAP_SECTION_CLAIMS, length="recap",
                       focus=stretch.focus, covered=tuple(covered), skip=unseen)
    answer = await synthesize(f"What happens in {stretch.focus}?", evidence, models.synthesize,
                              mode="graph", check=models.check, write=models.write,
                              book_titles=dict(titles), books=dict(books), section=spec)
    return (answer if answer.claims else None), evidence


def register_stretch(stretch: Stretch, answer, evidence,
                     registry: deep_module.Registry) -> tuple[AnswerSection | None, list, list]:
    """Number a drafted section into the answer (sections are registered in
    story order): (section or None, its claims, the passages it added)."""
    if answer is None:
        return None, [], []
    local = {p.n: p for p in evidence.passages}
    claims, offset, added = registry.add(answer.claims, local)
    prose = deep_module.remap_prose(answer.prose, offset, registry.claims) if answer.prose else ()
    if prose:
        prose = deep_module.keep_own(prose, offset + 1, offset + len(claims), registry.claims)
    text, style = ((compose_text(prose, answer.unanswered, answer.gaps), "prose") if prose
                   else (_compose(claims), "claims"))
    section = AnswerSection(heading=stretch.heading, prose=prose, text=text,
                            claims_start=offset + 1, claims_end=offset + len(claims), style=style,
                            note=answer.note, group=stretch.group, group_title=stretch.group,
                            title="")
    return section, claims, added


# The graph view: each section stages the entities its stretch's beats name most
# and the relationships evidenced in its chapters, as a deep section does.
GRAPH_ENTITIES = 6
GRAPH_EDGES_PER_ENTITY = 10


def stretch_graph(kuzu, stretch: "Stretch", beats: Sequence[Beat], scope: Scope,
                  books: Mapping[str, BookInfo]) -> tuple[list, list]:
    """(seeds, relationship rows) for one section: up to GRAPH_ENTITIES entities
    most named by the beats in the stretch's chapters, and their relationships
    whose evidence lies in those chapters and within the reading position.
    Empty when the stretch has no linked beats or the graph can't be read."""
    keys = {c.key for c in stretch.chapters}
    counts: dict[str, int] = {}
    for b in beats:
        if (b.book_id, b.chapter_seq) in keys:
            for e in dict.fromkeys(x for x in b.entity_ids if x):
                counts[e] = counts.get(e, 0) + 1
    ids = sorted(counts, key=lambda e: (-counts[e], e))[:GRAPH_ENTITIES]
    if not ids or kuzu is None:
        return [], []
    try:
        seeds = visible_module.shown_seeds(
            kuzu, scope.books,
            _entities(kuzu, ids, {i: float(counts[i]) for i in ids}, frozenset(), scope.books), books)
        rows, seen = [], set()
        for seed in seeds:
            found = run_template(kuzu, TemplateCall("across_books", {
                "entity_id": seed.id, "limit": GRAPH_EDGES_PER_ENTITY}))
            for r in found:
                if (r.book_id, r.chapter_seq) in keys and scope.sees(r.book_id, r.chapter_seq) \
                        and edge_key(r) not in seen:
                    seen.add(edge_key(r))
                    rows.append(r)
        return seeds, visible_module.shown_rows(kuzu, scope.books, rows, books)
    except Exception:  # noqa: BLE001 -- the graph view is a picture; the answer goes on without it
        log.exception("recap graph for %r failed", stretch.heading)
        return [], []


async def answer_recap(conn, kuzu, lance, question: str, models, recap: Recap, *, scope: Scope,
                       titles: Mapping[str, str], books: Mapping[str, BookInfo],
                       book_ids: Sequence[str] | None = None, on_event=None) -> Answer | None:
    """The recap answer, or None when the span holds nothing to recap and the
    question named no stretch of its own (the caller then answers in the ordinary
    way). Events, as a deep answer's: the `recap` step, `plan`, `parts`, then
    `section` (start and done, or skipped) for each section, `evidence`, and
    `short_answer`."""
    spend = deep_module.Spend()
    models = replace(models, synthesize=spend.counted(models.synthesize),
                     write=spend.counted(models.write) if models.write else None,
                     check=spend.counted(models.check) if models.check else None)
    emit(on_event, "step", name="recap", state="start", part=None, detail=None)
    span = await gather_span(conn, lance, recap, scope=scope, question=question, titles=titles,
                             books=books, embed=models.embed, book_ids=book_ids)
    # A beat was written over the whole story: under a reading position its names
    # are the ones the reader has met.
    span = replace(span, beats=tuple(visible_module.shown_beats(kuzu, scope.books,
                                                                list(span.beats), books)))
    stretches = plan_stretches(span.chapters, titles, numbers=span.numbers,
                               names=chapter_names(conn, sorted({c.book_id for c in span.chapters})))
    chapters = len(span.chapters)
    found = (f"{len(span.beats)} beats" if span.beats
             else f"{sum(len(c.chunks) for c in span.chapters)} passages")
    emit(on_event, "step", name="recap", state="done", part=None,
         detail=f"{chapters} chapter{'s' if chapters != 1 else ''} · {found}" if chapters
         else "nothing found")
    if not stretches:
        if span.requested:
            return Answer(text=NOTHING_VISIBLE, passages=(), mode="empty", template="recap",
                          note=None, sub_questions=(question,))
        return None
    emit(on_event, "plan", scope=span.order_note, candidates=len(span.beats),
         sections=[{"index": i, "heading": s.heading, "title": "",
                    "group": s.group, "group_title": s.group, "entities": []}
                   for i, s in enumerate(stretches)])
    emit(on_event, "parts", sub_questions=[s.heading for s in stretches])
    registry = deep_module.Registry()
    done: list[AnswerSection] = []
    skipped: list[str] = []
    failures: list[BaseException] = []
    empties = 0
    edges: list = []
    gate = asyncio.Semaphore(RECAP_CONCURRENCY)

    async def draft(index: int, stretch: Stretch):
        """("skipped", reason) | ("failed", error) | ("ok", answer, evidence)."""
        nonlocal edges
        async with gate:
            if spend.n + deep_module.SECTION_REQUESTS + deep_module.TAIL_REQUESTS > RECAP_MAX_REQUESTS:
                return ("skipped", "budget")
            if models.spend_ok is not None and not models.spend_ok():
                return ("skipped", "reserve")
            emit(on_event, "step", name="section", state="start", part=index, detail=None)
            emit(on_event, "section", index=index, state="start", kind="section",
                 heading=stretch.heading, group=stretch.group, group_title=stretch.group, title="")
            seeds, rows = stretch_graph(kuzu, stretch, span.beats, scope, books)
            if seeds:
                emit(on_event, "seeds", part=index, entities=list(seeds))
                emit(on_event, "edges", part=index, template="across_books", total=len(rows),
                     edges=rows[:EDGES_PER_PART_CAP])
                edges = edges + rows
            try:
                answer, evidence = await draft_stretch(
                    stretch, span, question=question, models=models, titles=titles, books=books,
                    registry=registry, covered=[s.heading for s in stretches[:index]])
            except Exception as err:  # noqa: BLE001 -- one section failing leaves the others
                log.exception("recap section %r failed", stretch.heading)
                return ("failed", err)
            return ("ok", answer, evidence)

    tasks = [asyncio.create_task(draft(i, s)) for i, s in enumerate(stretches)]
    try:
        for index, (stretch, task) in enumerate(zip(stretches, tasks)):
            meta = {"group": stretch.group, "group_title": stretch.group, "title": ""}
            outcome = await task
            if outcome[0] == "skipped":
                skipped.append(stretch.heading)
                emit(on_event, "section", index=index, state="skipped", kind="section",
                     heading=stretch.heading, reason=outcome[1], **meta)
                continue
            if outcome[0] == "failed":
                failures.append(outcome[1])
                emit(on_event, "section", index=index, state="done", kind="section",
                     heading=stretch.heading, style="failed", text="", prose=[], claims=[],
                     claims_range=None, passages=[], note="the section could not be written", **meta)
                emit(on_event, "step", name="section", state="done", part=index, detail="failed")
                continue
            section, claims, added = register_stretch(stretch, outcome[1], outcome[2], registry)
            if section is None:
                empties += 1
                emit(on_event, "section", index=index, state="done", kind="section",
                     heading=stretch.heading, style="empty", text="", prose=[], claims=[],
                     claims_range=None, passages=[], note=None, **meta)
                emit(on_event, "step", name="section", state="done", part=index,
                     detail="nothing found")
                continue
            done.append(section)
            event = deep_module._section_event(index, "section", section, stretch.heading, claims,
                                               added, section.style)
            emit(on_event, "section", **(event | meta))
            emit(on_event, "step", name="section", state="done", part=index,
                 detail=f"{len(claims)} claims")
    finally:
        for task in tasks:
            task.cancel()
    if not done:
        if failures and len(failures) + len(skipped) == len(stretches):
            raise failures[-1]
        return None
    claims, passages = registry.claims, registry.passages
    emit(on_event, "evidence", chunk_ids=[p.chunk_id for p in passages],
         edge_keys=[edge_key(e) for e in dict.fromkeys(edges)])
    short = await short_gist(question, done, claims, passages, models, book_titles=titles,
                             books=books)
    emit(on_event, "short_answer", prose=list(short), text=compose_text(short),
         claims=[c.model_dump() for c in claims],
         passages=[deep_module._passage_json(p) for p in passages])
    notes = [CHECK_SKIPPED_NOTE] if any(s.note and CHECK_SKIPPED_NOTE in s.note for s in done) else []
    if skipped:
        notes.append("stopped at the request limit; not written: " + "; ".join(skipped))
    return Answer(text=compose_recap(done, short), passages=tuple(passages), mode="graph",
                  template="recap", note="; ".join(notes) or None,
                  model=models.synthesis_model(), sub_questions=tuple(s.heading for s in done),
                  claims=tuple(claims), answer_style="sectioned", sections=tuple(done),
                  short_answer=short, requests=spend.n, unwritten=len(failures) + empties)
