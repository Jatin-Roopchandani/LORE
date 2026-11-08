"""Deep answers: an outline built from the graph and the codex, then one
section at a time (spec 2026-10-30, sections 10-12).

A broad question ("tell me about ...") deserves a detailed, sourced chapter, not
a paragraph. For one, the planner gathers the most significant entities in the
question's scope (a world, some books, or one named entity's facets), makes ONE
model call that groups them into sections, and code validates the result
(unknown entities dropped, empty sections dropped, at most DEEP_MAX_SECTIONS).

The plan is built around the question, not around the entities: each section
addresses one distinct aspect of the question as asked (its `focus`), the
candidates only say where to look, and a section whose subject does not bear on
the question is dropped. A section is then answered as a part of its own: its
entities are the seeds, with their relationships, their timeline and a capped
slice of their codex entries as evidence; it has its own claims step (given the
question and its focus, keeping only facts that bear on both), its own check and
its own writer, which has no word cap and opens with how the section answers its
aspect (`synthesize.synthesize(section=...)`). A section that passes streams to
the UI on its own; one that fails falls back to its claim list without touching
the others. The answer opens with a short answer, written after the sections
from the claims of all of them (`short_answer`): a few checked fact sentences,
and, for an analytical question, a reading checked by the viewpoint rules; when
nothing survives, code says no direct summary could be verified. Then come the
contents list and the sections.

A question with an interpretive part is not planned this way. Its sections are
its own parts and lenses (`answer_lenses`): one per lens, headed by the lens
question, each with its own writer and check; the answer opens with the short
answer (one reading per interpretive part), and a closing section holds the
counter-case and the limits (interpret.py). Deep mode only lifts the length and
claim caps of those sections.

Faithfulness is as everywhere else: every sentence cites checked claims and
every claim quotes a numbered passage. The planner, the codex and the timeline
only say where to look.

Gated by `decompose.DEEP`. Nothing here knows any library or any question.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

from pydantic import BaseModel

from lore.beats.store import beats_for_entities
from lore.codex import store as codex_store
from lore.extract.embed import QUERY_PREFIX
from lore.inference.gemini import GeminiOutputError
from lore.query import codexslice, hubselect, interpret
from lore.query import timeline as timeline_module
from lore.query import write as write_module
from lore.query.events import EDGES_PER_PART_CAP, edge_key, emit
from lore.query.models import (
    Answer, AnswerSection, BookInfo, Claim, EdgeRow, Evidence, Passage, ProseSentence,
    SectionSpec, SeedEntity, TemplateCall,
)
from lore.query import visible as visible_module
from lore.query.retrieval import search_chunks
from lore.query.scope import Scope
from lore.query.seed import _entities, named_seeds
from lore.query.subgraph import assemble_ranked, relevance
from lore.query.synthesize import (
    CHECK_SKIPPED_NOTE, INTRO_LENGTHS, PROSE_FALLBACK_NOTE, REFUSAL, _compose, _content_words, _finish,
    _numbers, _orphan_repair,
    _normalise, _overlap, _same_evidence, check_answer, shaped_claims, synthesize,
)
from lore.query.viewpoint import load_viewpoint
from lore.query.write import assemble, compose_sections, compose_text, split_items, verify_prose, write_answer
from lore.resolve.normalize import normalize_name

log = logging.getLogger(__name__)

# The planner's section count: asked for MIN_SECTIONS to DEEP_MAX_SECTIONS, and
# capped at the maximum in code.
DEEP_MAX_SECTIONS = 12
MIN_SECTIONS = 3
# Candidates per kind shown to the planner, and the entities one section reads.
PER_KIND = 12
SECTION_ENTITIES = 8
# A section's own budget: the claims it may keep, the characters of passages it
# reads (at most the models' own evidence budget), the facts per book it takes
# from each entity's relationships, and the nearest passages added to those.
SECTION_CLAIMS = 12
SECTION_CHARS = 18_000
SECTION_EDGES_PER_BOOK = 10
SECTION_NEAR_K = 8
SECTION_SPREAD = 12
SECTION_RELEVANT = 12
# Requests: a section costs its claims call, its writer and its check; the
# short answer a writer and a check. The deep path
# stops starting sections when the next would pass DEEP_MAX_REQUESTS (leaving
# room for the tail), or when the models' `spend_ok` says the day's answer
# reserve is reached.
DEEP_MAX_REQUESTS = 45
SECTION_REQUESTS = 3
TAIL_REQUESTS = 2
# The short answer: at most this many sentences (a reading included), written from
# at most this many claims, spread over all the sections.
SHORT_ANSWER_SENTENCES = 5
SHORT_ANSWER_CLAIMS = 60
# Said by code when no short answer passed its check.
NO_SUMMARY_LINE = ("No direct summary could be verified from the evidence found; "
                   "the sections below give the checked facts.")
# The closing section of an interpretive answer: the counter-case and the limits.
CLOSE_HEADING = "Counter-case and limits"
# Facets of one named entity.
MAX_FACET_RELATIONS = 8
PERIODS = 3
MIN_PERIOD_BEATS = 6
MAX_SUMMARY_CHARS = 160
MAX_HEADING_CHARS = 80
MAX_FOCUS_CHARS = 200

# Two planned sections whose focus lines share this share of their content words (Jaccard,
# function words dropped) cover one aspect: most of what one says the other says too.
SAME_ASPECT_OVERLAP = 0.6

# Candidate kinds, in the order the planner sees them.
KINDS = ("Concept", "Faction", "Place", "Object", "Character", "Event")


# --- the planner -----------------------------------------------------------------

class PlanSection(BaseModel):
    heading: str = ""
    focus: str = ""
    entities: list[str] = []


class DeepPlan(BaseModel):
    overview: str = ""
    analytical: bool = False
    sections: list[PlanSection] = []


PLANNER_PROMPT = """You are planning the sections of a long, detailed answer to a question, built from
the books of a story world.

Question: {question}

Candidates: the people, groups, places, things and ideas that the books establish
within the scope of the question, grouped by kind and most significant first. Each
line gives an id, a name and, where known, one line saying what it is. They are
evidence pointers: places to look for facts, not the structure of the answer.
{candidates}

Plan the answer around the question as asked, not around the candidates.
Rules:
- Give between {min_sections} and {max_sections} sections. Each section addresses
  one distinct aspect of the question as asked. For example, when the question asks
  about methods, techniques or stages, each section is one method, technique or
  stage; when it asks about causes, each section is one cause. No two sections may
  cover the same aspect: when two aspects overlap, merge them into one section.
- Each section has:
  - "focus": one line saying the aspect of the question that the section answers,
    worded as that aspect and not as a topic.
  - "heading": a short noun phrase of at most 6 words that names that aspect, with
    no trailing punctuation and no claim about the answer.
  - "entities": the ids of the candidates the section draws its facts from.
- Drop any section whose subject does not bear on the question, however significant
  its candidates are. A candidate that is only incidental to the question gets no
  section of its own.
- Put foundations before details, so that a reader meets an idea before the things
  built on it.
- Use only ids from the list above. Never add a section that none of the candidates
  can supply facts for.
- A candidate may be in two sections when it matters to both.
- "overview" is one plain sentence saying what the whole answer covers.
- "analytical" is true when the question asks for an analysis, an assessment, an
  explanation of how or why, or the significance of something, and false when it
  only asks what the books state.
"""


@dataclass(frozen=True)
class Candidate:
    """Something a section may cover. `id` is an entity id or, for a facet of
    one entity, a derived id. `entity_ids` are the entities that stand for it
    (the section's seeds). A facet may narrow the entity's codex (`aspect`) or
    its timeline (`beat_ids`)."""

    id: str
    name: str
    kind: str
    entity_ids: tuple[str, ...]
    summary: str = ""
    score: int = 0
    aspect: str = ""
    beat_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedSection:
    heading: str
    candidates: tuple[Candidate, ...]
    focus: str = ""

    @property
    def entity_ids(self) -> list[str]:
        return list(dict.fromkeys(e for c in self.candidates for e in c.entity_ids))[:SECTION_ENTITIES]


def _batches(items: Sequence[str], size: int = 500):
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def _one_line(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


_CITATION = re.compile(r"\[\s*\d+(?:\s*[,;\u2013-]\s*\d+)*\s*\]")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKUP = re.compile(r"[*`~#>|]+|(?<!\w)_+|_+(?!\w)")


def clean_heading(text, limit: int = MAX_HEADING_CHARS) -> str:
    """A planner's heading as a heading: citations ("[3]") and markup (emphasis,
    code, links, "#") removed, on one line, at most about `limit` characters.
    It is planner text, never a claim, so it is not trusted to be clean."""
    text = _LINK.sub(r"\1", str(text or ""))
    text = " ".join(_MARKUP.sub("", _CITATION.sub("", text)).split())
    if len(text) > limit:  # cut at a word boundary, never inside a word
        cut = text[:limit + 1].rsplit(" ", 1)[0] if " " in text[:limit + 1] else text.split(" ", 1)[0]
        if len(cut) > 2 * limit:  # one absurd unbroken string: the only case that is cut inside a word
            cut = text[:limit]
        text = cut.rstrip(" :;,.-\u2013\u2014") + "\u2026"
        return text.lstrip(" :.-")
    return text.strip(" :.-")


def _counts(conn: sqlite3.Connection, sql: str, ids: Sequence[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for batch in _batches(list(ids)):
        try:
            for key, n in conn.execute(sql.format(marks=",".join("?" * len(batch))), batch):
                out[key] = int(n)
        except sqlite3.OperationalError:  # a library not migrated to beats or the codex yet
            return {}
    return out


def _richness(conn: sqlite3.Connection, ids: Sequence[str],
              visible=None) -> tuple[dict[str, int], dict[str, int]]:
    """(beats, codex statements) per entity id; under a reading position
    (`visible`), only those the reader has read are counted."""
    if visible_module.capped(visible):
        wanted = set(ids)
        beats: dict[str, int] = {}
        for b in _beats_of(ids, conn, visible):
            for e in set(b.entity_ids) & wanted:
                beats[e] = beats.get(e, 0) + 1
        cx: dict[str, int] = {}
        for line in _statements(conn, ids, visible):
            cx[line[0]] = cx.get(line[0], 0) + 1
        return beats, cx
    beats = _counts(conn, "SELECT entity_id, COUNT(DISTINCT beat_id) FROM beat_entities"
                          " WHERE entity_id IN ({marks}) GROUP BY entity_id", ids)
    cx = _counts(conn, "SELECT entity_id, COUNT(*) FROM codex_entries"
                       " WHERE entity_id IN ({marks}) GROUP BY entity_id", ids)
    return beats, cx


def _statements(conn: sqlite3.Connection, ids: Sequence[str], visible) -> list[tuple[str, str, str]]:
    """(entity id, aspect, text) of the codex statements of `ids` the reader has
    read (`visible`)."""
    try:
        entries = codex_store.entries_for(conn, list(ids))
    except sqlite3.OperationalError:
        return []
    where = codexslice._positions(conn, [s.chunk_id for e in entries for s in e.statements])
    return [(e.entity_id, s.aspect, s.text) for e in entries for s in e.statements
            if s.chunk_id in where and visible_module.sees(visible, *where[s.chunk_id])]


def _summaries(conn: sqlite3.Connection, ids: Sequence[str], visible=None) -> dict[str, str]:
    """One-line codex summaries by entity id. None while a reading position is
    on: a summary is written over the whole story."""
    out: dict[str, str] = {}
    if visible_module.capped(visible):
        return out
    try:
        for entity_id in ids:
            found = codex_store.summary_for(conn, entity_id)
            if found and found.summary:
                out[entity_id] = _one_line(found.summary, MAX_SUMMARY_CHARS)
    except sqlite3.OperationalError:
        return {}
    return out


def _span(kuzu, book_ids: frozenset[str] | None) -> dict[str, set[tuple[str, int]]]:
    """Per live entity, the (book, chapter) places its relationships fall in (the
    visible ones, under a reading position)."""
    where = "WHERE true" + visible_module.book_clause(book_ids) + " " if book_ids else ""
    params = visible_module.book_params(book_ids) if book_ids else {}
    places: dict[str, set[tuple[str, int]]] = {}
    for end in ("a", "b"):
        for entity_id, book, chapter, _ in kuzu.execute(
                f"MATCH (a:Entity)-[r:REL]->(b:Entity) {where}"
                f"RETURN {end}.id, r.book_id, r.chapter_seq, count(r)", params):
            places.setdefault(entity_id, set()).add((str(book), int(chapter)))
    return places


def _scoped_entities(kuzu, book_ids: frozenset[str] | None, book_names: frozenset[str],
                     infos: Mapping[str, BookInfo] | None = None) -> dict[str, tuple]:
    rows = kuzu.execute("MATCH (e:Entity) WHERE e.kind IN $kinds RETURN e.id, e.name, e.kind, e.books",
                        {"kinds": list(KINDS)})
    found = {r[0]: (str(r[1]), str(r[2]), tuple(r[3] or ())) for r in rows
             if (book_ids is None or any(b in book_ids for b in r[3] or ()))
             and normalize_name(str(r[1] or "")) not in book_names}
    if visible_module.capped(book_ids):  # only those the reader has met, under the name met
        names = visible_module.visible_names(kuzu, book_ids, list(found), infos)
        found = {i: (visible_module.display_name(names, i, v[0]), v[1],
                     tuple(b for b in v[2] if b in book_ids))
                 for i, v in found.items() if i in names}
    return found


def gather_candidates(conn: sqlite3.Connection, kuzu, *, book_ids: frozenset[str] | None,
                      book_names: frozenset[str] = frozenset(),
                      focus: Sequence[str] = (),
                      books: Mapping[str, BookInfo] | None = None) -> list[Candidate]:
    """The most significant entities in scope, at most PER_KIND of each kind,
    ordered by kind (KINDS) and within a kind by significance: how many places in
    the story its relationships fall in, plus its beats and its codex
    statements. Entities the question names (`focus`) always stay. Book titles
    and entities with no relationships are left out. Under a reading position
    (`book_ids` a `VisibleBooks`) only what has been read counts, entities are
    named by the earliest alias met, and no codex summary is shown."""
    entities = _scoped_entities(kuzu, book_ids, book_names, books)
    places = _span(kuzu, book_ids)
    live = [i for i in entities if i in places]
    beats, statements = _richness(conn, live, book_ids)
    summaries = _summaries(conn, live, book_ids)
    scored = []
    for i in live:
        name, kind, _ = entities[i]
        score = len(places[i]) + beats.get(i, 0) + statements.get(i, 0)
        scored.append(Candidate(id=i, name=name, kind=kind, entity_ids=(i,),
                                summary=summaries.get(i, ""), score=score))
    out: list[Candidate] = []
    for kind in KINDS:
        mine = sorted((c for c in scored if c.kind == kind),
                      key=lambda c: (c.id not in focus, -c.score, c.name, c.id))
        out += mine[:PER_KIND]
    return out


def facet_candidates(conn: sqlite3.Connection, kuzu, focal: SeedEntity, *,
                     book_ids: frozenset[str] | None,
                     books: Mapping[str, BookInfo] | None,
                     book_names: frozenset[str] = frozenset()) -> list[Candidate]:
    """For a question about one entity: the entity itself, the aspects its codex
    entry covers, up to PERIODS stretches of its story, and the entities it is
    most involved with."""
    found = codex_store.entries_for(conn, [focal.id]) if _has_codex(conn) else []
    hidden = visible_module.capped(book_ids)  # a reading position: the summary is whole-story
    summary = _one_line(found[0].summary, MAX_SUMMARY_CHARS) if found and not hidden else ""
    read = ([t for _e, a, t in _statements(conn, [focal.id], book_ids)] if hidden else None)
    out = [Candidate(id=focal.id, name=focal.name, kind=focal.kind, entity_ids=(focal.id,),
                     summary=summary)]
    if found:
        for aspect in codex_store.ASPECTS:
            texts = [visible_module.respell(s.text, [(found[0].name, focal.name)]) if hidden
                     else s.text
                     for s in found[0].statements
                     if s.aspect == aspect and (read is None or s.text in read)]
            if texts:
                out.append(Candidate(
                    id=f"{focal.id}#{aspect}", name=f"{focal.name}: {aspect}", kind="Aspect",
                    entity_ids=(focal.id,), summary=_one_line(texts[0], MAX_SUMMARY_CHARS),
                    score=len(texts), aspect=aspect))
    beats = visible_module.shown_beats(kuzu, book_ids, _beats_of([focal.id], conn, book_ids), books)
    ordered = timeline_module.order_beats(beats, books) if len(beats) >= MIN_PERIOD_BEATS else []
    for k in range(PERIODS if ordered else 0):
        mine = ordered[k * len(ordered) // PERIODS:(k + 1) * len(ordered) // PERIODS]
        if mine:
            out.append(Candidate(
                id=f"{focal.id}#period{k + 1}",
                name=f"{focal.name}: stretch {k + 1} of {PERIODS} of its story",
                kind="Period", entity_ids=(focal.id,),
                summary=_one_line(mine[0].text, MAX_SUMMARY_CHARS), score=len(mine),
                beat_ids=tuple(b.id for b in mine)))
    where = visible_module.book_clause(book_ids) + " " if book_ids else ""
    params = {"id": focal.id, "limit": MAX_FACET_RELATIONS * 3}
    if book_ids:
        params.update(visible_module.book_params(book_ids))
    neighbours = kuzu.execute(
        f"MATCH (a:Entity)-[r:REL]-(b:Entity) WHERE a.id = $id AND b.id <> $id {where}"
        "RETURN b.id AS other, count(r) AS n ORDER BY n DESC, other LIMIT $limit", params)
    ranked = [(str(r[0]), int(r[1])) for r in neighbours]
    known = _scoped_entities(kuzu, book_ids, book_names, books)
    summaries = _summaries(conn, [i for i, _ in ranked], book_ids)
    for other, n in [r for r in ranked if r[0] in known][:MAX_FACET_RELATIONS]:
        name, kind, _ = known[other]
        out.append(Candidate(id=other, name=name, kind=kind, entity_ids=(other, focal.id),
                             summary=summaries.get(other, ""), score=n))
    return out


def _has_codex(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1 FROM codex_summaries LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


def _beats_of(entity_ids: Sequence[str], conn: sqlite3.Connection,
              book_ids: frozenset[str] | None) -> list:
    try:
        beats = beats_for_entities(conn, list(entity_ids))
    except sqlite3.OperationalError:  # a library not migrated to beats yet
        return []
    return [b for b in beats if not book_ids
            or visible_module.sees(book_ids, b.book_id, b.chapter_seq)]


def planner_prompt(question: str, candidates: Sequence[Candidate],
                   max_sections: int = DEEP_MAX_SECTIONS) -> str:
    lines: list[str] = []
    for kind in (*KINDS, "Aspect", "Period"):
        mine = [c for c in candidates if c.kind == kind]
        if not mine:
            continue
        lines.append(f"{kind}:")
        lines += [f"- {c.id} | {c.name}" + (f" | {c.summary}" if c.summary else "") for c in mine]
    return PLANNER_PROMPT.format(question=question, candidates="\n".join(lines),
                                 min_sections=MIN_SECTIONS, max_sections=max_sections)


def validate_plan(raw, candidates: Sequence[Candidate],
                  max_sections: int | None = None) -> list[PlannedSection]:
    """The planner's sections as the answer may use them. Any entity that is not
    a candidate is dropped, as is a repeat within a section; a section left with
    no entity is dropped, and so is one with no focus (the aspect of the question
    it answers); a section's heading is its short label cleaned, or else its focus
    shortened (`lens_heading`); a section whose heading repeats an earlier one
    joins it; and the plan is cut to `max_sections` (DEEP_MAX_SECTIONS)."""
    cap = DEEP_MAX_SECTIONS if max_sections is None else max_sections
    known = {c.id: c for c in candidates}
    # casefolded heading -> (heading, focus, members)
    sections: dict[str, tuple[str, str, list[Candidate]]] = {}
    for item in getattr(raw, "sections", None) or []:
        ids = [i for i in dict.fromkeys(str(x).strip() for x in (getattr(item, "entities", None) or []))
               if i in known]
        focus = clean_heading(getattr(item, "focus", ""), MAX_FOCUS_CHARS)
        if not ids or not focus:
            continue
        members = [known[i] for i in ids]
        heading = lens_heading(focus, getattr(item, "heading", ""))
        mine = sections.setdefault(heading.casefold(), (heading, focus, []))[2]
        mine += [c for c in members if c not in mine]
    merged: list[tuple[str, str, list[Candidate]]] = []
    for heading, focus, members in sections.values():
        twin = next((m for m in merged if _overlap(m[1], focus) >= SAME_ASPECT_OVERLAP), None)
        if twin is None:
            merged.append((heading, focus, list(members)))
        else:
            twin[2].extend(c for c in members if c not in twin[2])
    return [PlannedSection(h, tuple(cs), f) for h, f, cs in merged[:max(cap, 0)]]


# --- one section -----------------------------------------------------------------

@dataclass
class Spend:
    """Counts the model requests of one deep answer."""

    n: int = 0

    def counted(self, generate):
        """`generate`, counting each request. A reply the model wrote but that
        is not usable (malformed or cut-off JSON) is asked for once more, on the
        next model of the cascade when there is one; the retry is a request too."""
        async def call(prompt, schema):
            self.n += 1
            try:
                return await generate(prompt, schema)
            except GeminiOutputError as err:
                self.n += 1
                log.warning("retrying after an unusable reply from %s", err.model or "the model")
                return await (generate(prompt, schema, avoid=err.model) if err.model
                              else generate(prompt, schema))
        return call


def repair_budget(spend: Spend, spend_ok, reserve: int):
    """The `spend_ok` a deep answer's sections ask before an ORPHAN_REPAIR round
    (write + check): the day's reserve (`spend_ok`, None for none) and room for
    those two requests under DEEP_MAX_REQUESTS, `reserve` more kept back."""
    def ok() -> bool:
        return (spend.n + 2 + reserve <= DEEP_MAX_REQUESTS
                and (spend_ok is None or bool(spend_ok())))
    return ok


@dataclass
class Registry:
    """The answer's own numbering: passages and claims from every section,
    numbered as they arrive (a section's local numbers become these)."""

    passages: list[Passage] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    _at: dict[str, int] = field(default_factory=dict)

    def passage(self, p: Passage) -> int:
        if p.chunk_id not in self._at:
            self.passages.append(replace(p, n=len(self.passages) + 1))
            self._at[p.chunk_id] = len(self.passages)
        return self._at[p.chunk_id]

    def add(self, claims: Sequence[Claim], local: Mapping[int, Passage]):
        """Add one section's claims (their passage numbers local). Returns the
        remapped claims, the local-to-answer claim number map and the passages
        this added."""
        stray = [c.passage for c in claims if c.passage not in local]
        if stray:  # dropping one would shift the claim numbers the prose cites
            raise ValueError(f"claims cite passages outside their section: {sorted(set(stray))}")
        before = len(self.passages)
        offset = len(self.claims)
        mapped = [c.model_copy(update={"passage": self.passage(local[c.passage])})
                  for c in claims]
        self.claims += mapped
        return mapped, offset, self.passages[before:]


def remap_prose(prose: Sequence[ProseSentence], offset: int,
                answer_claims: Sequence[Claim]) -> tuple[ProseSentence, ...]:
    """A section's prose with its claim numbers shifted into the answer's and its
    passages recomputed from the claims they cite (`answer_claims`: every claim
    of the answer so far, in the answer's numbering)."""
    out = []
    for s in prose:
        claims = tuple(k + offset for k in s.claims)
        out.append(replace(s, claims=claims, passages=tuple(sorted(
            {answer_claims[k - 1].passage for k in claims if 1 <= k <= len(answer_claims)}))))
    return tuple(out)


@dataclass
class SectionResult:
    section: AnswerSection | None
    claims: list[Claim]
    new_passages: list[Passage]
    edges: list[EdgeRow]
    seeds: list[SeedEntity]
    failed: str | None = None
    offtopic: bool = False  # claims were written but none bear on the question's subject
    redundant: bool = False  # every claim restated an earlier section's: left out, not failed


_NAME_STOP = frozenset({"the", "and", "of", "von", "van", "de", "la", "le", "sir", "mr", "mrs", "dr"})


def _name_words(name: str) -> set[str]:
    return {w for w in re.findall(r"\w+", name.casefold()) if len(w) >= 3 and w not in _NAME_STOP}


def bears_on(claims: Sequence[Claim], subject: Sequence[SeedEntity]) -> bool:
    """True when some claim's text or quote names one of the `subject` entities
    (its name, or any distinctive word of it)."""
    words = set().union(*(_name_words(s.name) for s in subject)) if subject else set()
    if not words:
        return True  # nothing to check against
    return any(words & set(re.findall(r"\w+", f"{c.text} {c.quote}".casefold())) for c in claims)


def section_question(heading: str, names: Sequence[str]) -> str:
    return f"{heading} ({', '.join(names)})" if names else heading


def _unseen(registry: "Registry"):
    """A filter for a section's claims: those that do not restate a fact an earlier
    section already wrote (the same passage, and `_same_fact`'s word overlap). Each
    fact then appears in one section, the earliest that wrote it."""
    def keep(claims, passages):
        keep.dropped += len(claims)
        chunk = {p.n: p.chunk_id for p in passages}
        out = []
        for c in claims:
            at = registry._at.get(chunk.get(c.passage, ""), -1)
            probe = c.model_copy(update={"passage": at})
            if not any(_same_fact(prior, probe) for prior in registry.claims):
                out.append(c)
        keep.dropped -= len(out)
        return out
    keep.dropped = 0
    return keep


def _restrictions(section: PlannedSection):
    """Per entity, the codex aspects and the beat ids a section is narrowed to.
    An entity named plainly in the section is not narrowed."""
    aspects: dict[str, set[str]] = {}
    beat_ids: dict[str, set[str]] = {}
    for entity in section.entity_ids:
        mine = [c for c in section.candidates if entity in c.entity_ids]
        if any(not (c.aspect or c.beat_ids) for c in mine):
            continue
        if any(c.aspect for c in mine):
            aspects[entity] = {c.aspect for c in mine if c.aspect}
        if any(c.beat_ids for c in mine):
            beat_ids[entity] = {b for c in mine for b in c.beat_ids}
    return aspects, beat_ids


async def run_section(index: int, section: PlannedSection, vector: list[float], *, question: str,
                      conn, kuzu, lance, models, scope: Scope, titles: dict[str, str],
                      books: Mapping[str, BookInfo], registry: Registry,
                      book_ids: frozenset[str] | None, on_event=None,
                      subject: Sequence[SeedEntity] = (),
                      covered: Sequence[str] = ()) -> SectionResult:
    """Answer one section from its own evidence: its entities' relationships,
    timeline and codex slice, and the passages nearest it. The section's claims
    and prose come back in the answer's numbering (the registry's). The claims
    step is asked the section's focus (the aspect of `question` it answers) and
    keeps only the facts that bear on it."""
    seeds = visible_module.shown_seeds(
        kuzu, scope.books,
        _entities(kuzu, section.entity_ids, {i: 0.0 for i in section.entity_ids},
                  frozenset(), scope.books), books)
    names = [s.name for s in seeds]
    ask = section_question(section.focus or section.heading, names)
    rows: list[EdgeRow] = []
    for seed in seeds:
        rows += hubselect.search(
            kuzu, TemplateCall("across_books", {"entity_id": seed.id,
                                                "limit": SECTION_EDGES_PER_BOOK}),
            lance=lance, vector=vector, question=ask,
            keep=lambda found: visible_module.shown_rows(
                kuzu, scope.books, [r for r in found if scope.sees(r.book_id, r.chapter_seq)],
                books))
    # The graph view stages a section's starting entities and relationships as it
    # starts, as it does for each part of an ordinary answer (part = the section).
    emit(on_event, "seeds", part=index, entities=list(seeds))
    emit(on_event, "edges", part=index, template="across_books", total=len(rows),
         edges=rows[:EDGES_PER_PART_CAP])
    aspects, beat_ids = _restrictions(section)
    shown = book_ids if book_ids else None
    seed_ids = {s.id for s in seeds}

    def wanted(b) -> bool:  # a beat of an entity the section narrows is kept only if it is in its stretch
        return any(e not in beat_ids or b.id in beat_ids[e] for e in b.entity_ids if e in seed_ids)

    beats = [b for b in _beats_of([s.id for s in seeds if s.kind in timeline_module.TIMELINE_KINDS],
                                  conn, shown) if wanted(b)]
    beats = timeline_module.select_beats(beats, vector, lance, books) if beats else []
    beats = visible_module.shown_beats(kuzu, scope.books, beats, books)
    lines = codexslice.lines_for(conn, [s.id for s in seeds], book_ids=shown, aspects=aspects,
                                 visible=scope.books)
    lines = (codexslice.select_lines(conn, lines, [s.id for s in seeds], vector, lance, books,
                                     SECTION_SPREAD, SECTION_RELEVANT) if lines else [])
    lines = visible_module.shown_lines(kuzu, scope.books, lines, books)
    near = [(str(r["chunk_id"]), str(r.get("book_id", "")), int(r.get("chapter_seq", 0)))
            for r in search_chunks(lance, vector, SECTION_NEAR_K, book_ids=shown or scope.books)]
    chunk_ids = sorted({r.chunk_id for r in rows} | {c for c, _, _ in near}
                       | {b.chunk_id for b in beats} | {l.chunk_id for l in lines})
    scores = relevance(vector, lance.chunk_vectors(chunk_ids))
    extra = {}
    if beats:
        extra |= {"timelines": [timeline_module.timeline_beats(beats)],
                  "timeline_share": timeline_module.TIMELINE_SHARE}
    if lines:
        extra |= {"codexes": [tuple(lines)], "codex_share": codexslice.CODEX_SHARE}
    evidence = assemble_ranked(conn, rows, extra_chunks=near,
                               max_chars=min(models.evidence_chars, SECTION_CHARS),
                               score_lists=[scores], visible=scope.books, **extra)
    if not evidence.passages:
        return SectionResult(None, [], [], [], seeds)
    unseen = _unseen(registry)
    spec = SectionSpec(part_of=question, max_claims=SECTION_CLAIMS, length="section",
                       focus=section.focus, subject=tuple(s.name for s in subject),
                       covered=tuple(covered), skip=unseen)
    answer = await synthesize(ask, evidence, models.synthesize, mode="graph", check=models.check,
                              write=models.write, book_titles=titles, books=dict(books), section=spec,
                              spend_ok=getattr(models, "spend_ok", None))
    if not answer.claims:
        return SectionResult(None, [], [], list(evidence.edges), seeds, redundant=unseen.dropped > 0)
    # A section that also reads entities other than the subject is checked against
    # the subject: facts about someone else leave it out (a section of the subject's
    # own aspects is about it by construction).
    if subject and set(section.entity_ids) - {s.id for s in subject} \
            and not bears_on(answer.claims, subject):
        return SectionResult(None, [], [], list(evidence.edges), seeds, offtopic=True)
    local = {p.n: p for p in evidence.passages}
    claims, offset, added = registry.add(answer.claims, local)
    prose = remap_prose(answer.prose, offset, registry.claims) if answer.prose else ()
    if prose:  # a section cites only the claims it introduces
        prose = keep_own(prose, offset + 1, offset + len(claims), registry.claims)
    if prose:
        text, style = compose_text(prose, answer.unanswered, answer.gaps), "prose"
    else:
        text, style = _compose(claims), "claims"
    note = answer.note
    result = AnswerSection(heading=section.heading, prose=prose, text=text,
                           claims_start=offset + 1, claims_end=offset + len(claims), style=style,
                           entities=tuple(names), note=note, title=section.focus)
    return SectionResult(result, claims, added, list(evidence.edges), seeds)


# --- cited prose over the answer's claims (the short answer, a lens section) ---------------------------

async def cited_prose(question: str, parts: Sequence[str], claims: Sequence[Claim],
                      passages: Sequence[Passage], models, *, length: str | None,
                      shape: "interpret.Shape | None" = None,
                      book_titles: dict[str, str] | None = None,
                      books: dict[str, BookInfo] | None = None,
                      covered: Sequence[str] = (),
                      ) -> tuple[ProseSentence, ...] | None:
    """Prose over `claims` that passed the writer, the code guards and the
    check, or None. The sentences' claim numbers are 1-based into `claims`
    (already mapped back from the check's survivors); their passages are the
    claims' own."""
    if models.write is None or models.check is None or not claims:
        return None
    drafted = await write_answer(question, list(parts), claims, passages, models.write,
                                 book_titles=book_titles, books=books, shape=shape, length=length,
                                 covered=covered)
    if drafted is None:
        return None
    sentences = verify_prose(drafted, len(claims), [c.text for c in claims], shape)
    if not sentences:
        return None
    outcome = await check_answer(claims, passages, models.check, book_titles=book_titles,
                                 sentences=sentences, books=books, shape=shape)
    if not outcome.ran or outcome.sentence_ok is None:
        return None
    passage_of = [c.passage for c in claims]
    built = assemble(sentences, len(split_items(drafted)), outcome.claim_ok, outcome.sentence_ok,
                     passage_of)
    if not built:
        return None
    if write_module.orphan_repair_on():
        # ORPHAN_REPAIR, as in synthesize._finish: one round, else no orphan is shown.
        keep = write_module.kept_flags(sentences, outcome.claim_ok, outcome.sentence_ok)
        orphaned = write_module.orphans(sentences, keep)
        intro = length in INTRO_LENGTHS and shape is None and write_module.opener_cut(keep)
        if orphaned or intro:
            fixed = await _orphan_repair(
                question, list(parts), claims, passages, models.check, models.write, book_titles,
                books, shape, length, covered, sentences, keep, outcome, (), intro,
                getattr(models, "spend_ok", None))
            better = None
            if fixed is not None:
                again, written, checked = fixed
                better = assemble(again, written, checked.claim_ok, checked.sentence_ok, passage_of)
                if better and write_module.orphans(
                        again, write_module.kept_flags(again, checked.claim_ok, checked.sentence_ok)):
                    better = None
            if better:
                built, outcome = better, checked
            elif orphaned:
                return None
    survivors = [k for k, ok in enumerate(outcome.claim_ok, start=1) if ok]
    return tuple(replace(s, claims=tuple(survivors[k - 1] for k in s.claims)) for s in built)


def _spread(sections: Sequence[AnswerSection], cap: int) -> list[tuple[int, int]]:
    """(index in `sections`, answer claim number) of the claims the short answer
    reads: all of them, or, past `cap`, an even share of each section's, spread
    along it (never only the first claims)."""
    numbers = [list(range(s.claims_start, s.claims_end + 1)) for s in sections]
    if sum(map(len, numbers)) > cap:
        each = max(1, cap // max(len(sections), 1))
        numbers = [[mine[i * len(mine) // each] for i in range(each)] if len(mine) > each else mine
                   for mine in numbers]
    return [(i, k) for i, mine in enumerate(numbers) for k in mine]


async def short_answer(question: str, sections: Sequence[AnswerSection], claims: Sequence[Claim],
                       passages: Sequence[Passage], models, *, analytical: bool = False,
                       book_titles: dict[str, str] | None = None,
                       books: dict[str, BookInfo] | None = None) -> tuple[ProseSentence, ...]:
    """The short answer to a sectioned answer, written after its sections from the
    claims of all of them (`SHORT_ANSWER_CLAIMS`, spread over the sections): two to
    five sentences that answer `question` as asked, each citing claims of the
    answer (the answer's own numbering), checked as every prose is. They are fact
    sentences, with one exception: for an `analytical` question the opening
    sentence may be a reading, a synthesis that rests on facts of at least two
    sections and is checked by the viewpoint rules (interpret.py); each section is
    then a lens of one interpretive part. When nothing survives the writer, the
    guards and the check, code says no direct summary could be verified (the
    sections stand either way)."""
    picked = _spread(sections, SHORT_ANSWER_CLAIMS)
    numbers = [k for _, k in picked]
    shape = None
    if analytical and interpret.active():
        shape = interpret.Shape(
            viewpoint=models.viewpoint or load_viewpoint(), parts=(question,), kinds=("interpretive",),
            retrieval=tuple((s.heading, 0, True) for s in sections),
            claim_parts=tuple(frozenset({i}) for i, _ in picked))
    try:
        got = await cited_prose(question, [question], [claims[k - 1] for k in numbers], passages,
                                models, length="short", shape=shape, book_titles=book_titles,
                                books=books)
    except Exception:  # noqa: BLE001 -- the sections are written; the short answer is an extra
        log.exception("the short answer failed")
        got = None
    reading = next((s for s in got or () if s.kind == "reading"), None)
    chosen = ([reading] if reading else []) + [s for s in got or () if s.kind == "fact"]
    chosen = chosen[:SHORT_ANSWER_SENTENCES]
    if not chosen:
        return (ProseSentence(text=NO_SUMMARY_LINE, claims=(), passages=(), new_paragraph=True,
                              kind="limit", short=True),)
    return tuple(replace(s, claims=tuple(numbers[k - 1] for k in s.claims),
                         new_paragraph=i == 0, short=True) for i, s in enumerate(chosen))


# --- the whole answer -------------------------------------------------------------

def _passage_json(p: Passage) -> dict:
    return {"n": p.n, "chunk_id": p.chunk_id, "book_id": p.book_id,
            "chapter_seq": p.chapter_seq, "text": p.text}


def _section_event(index: int, kind: str, result: AnswerSection | None, heading: str,
                   claims: Sequence[Claim], new: Sequence[Passage], style: str) -> dict:
    return {
        "index": index, "state": "done", "kind": kind, "heading": heading, "style": style,
        "text": result.text if result else "",
        "prose": list(result.prose) if result else [],
        "claims": [c.model_dump() for c in claims],
        "claims_range": ([result.claims_start, result.claims_end]
                         if result and result.claims_end >= result.claims_start else None),
        "passages": [_passage_json(p) for p in new],
        "note": result.note if result else None}


async def answer_deep(conn, kuzu, lance, question: str, models, *, scope: Scope,
                      titles: dict[str, str], books: Mapping[str, BookInfo],
                      vector: list[float], plan, named_ids: Sequence[str] = (),
                      book_names: frozenset[str] = frozenset(),
                      book_ids: frozenset[str] | None = None,
                      on_event=None) -> Answer | None:
    """The deep answer, or None when it cannot be made (no candidates, a planner
    that failed or gave no usable section): the caller then answers in the
    ordinary way. Events: `plan`, then `section` (start and done, or skipped)
    for each section in order, then `short_answer`."""
    spend = Spend()
    models = replace(models, synthesize=spend.counted(models.synthesize),
                     write=spend.counted(models.write) if models.write else None,
                     check=spend.counted(models.check) if models.check else None)
    day_ok = models.spend_ok  # starting a section asks the day's reserve alone
    repair_ok = repair_budget(spend, day_ok, TAIL_REQUESTS)
    models = replace(models, spend_ok=repair_ok)
    scope_books = book_ids or scope.books
    focal = None
    subject: list[SeedEntity] = []
    if len(named_ids) >= 1:
        seeds = visible_module.shown_seeds(
            kuzu, scope.books,
            await named_seeds(kuzu, list(named_ids), exclude_names=book_names,
                              book_ids=scope.books), books)
        subject = list(seeds)
        focal = seeds[0] if len(seeds) == 1 else None
    emit(on_event, "step", name="plan", state="start", part=None, detail=None)
    candidates = (facet_candidates(conn, kuzu, focal, book_ids=scope_books, books=books,
                                   book_names=book_names)
                  if focal is not None else
                  gather_candidates(conn, kuzu, book_ids=scope_books, book_names=book_names,
                                    focus=list(named_ids), books=books))
    raw = None
    if candidates:
        try:
            raw = await models.synthesize(planner_prompt(question, candidates), DeepPlan)
        except Exception:  # noqa: BLE001 -- a failed planner costs the deep path, not the answer
            log.exception("the deep planner failed")
    planned = validate_plan(raw, candidates) if raw is not None else []
    emit(on_event, "step", name="plan", state="done", part=None,
         detail=f"{len(planned)} section" + ("s" if len(planned) != 1 else ""))
    if not planned:
        return None
    emit(on_event, "plan", scope=str(getattr(raw, "overview", "") or "").strip(),
         candidates=len(candidates),
         sections=[{"index": i, "heading": s.heading, "title": s.focus,
                    "entities": [{"id": c.id, "name": c.name, "kind": c.kind}
                                 for c in s.candidates]} for i, s in enumerate(planned)])

    # The sections are the answer's parts from here on: the graph view and the
    # steps list name them as it names the parts of an ordinary answer.
    emit(on_event, "parts", sub_questions=[s.heading for s in planned])
    # The short answer's writer and check.
    tail = TAIL_REQUESTS
    vectors = await models.embed([QUERY_PREFIX + section_question(
        s.focus or s.heading, [c.name for c in s.candidates]) for s in planned])
    registry = Registry()
    done: list[AnswerSection] = []
    edges: list[EdgeRow] = []
    seeds_seen: dict[str, SeedEntity] = {}
    skipped: list[str] = []
    failures: list[BaseException] = []
    empties = 0
    for index, (section, vec) in enumerate(zip(planned, vectors)):
        reason = None
        if spend.n + SECTION_REQUESTS + tail > DEEP_MAX_REQUESTS:
            reason = "budget"
        elif day_ok is not None and not day_ok():
            reason = "reserve"
        if reason:
            skipped.append(section.heading)
            emit(on_event, "section", index=index, state="skipped", kind="section",
                 heading=section.heading, reason=reason)
            continue
        emit(on_event, "step", name="section", state="start", part=index, detail=None)
        emit(on_event, "section", index=index, state="start", kind="section", heading=section.heading,
             title=section.focus)
        try:
            result = await run_section(index, section, vec, question=question, conn=conn,
                                       kuzu=kuzu, lance=lance, models=models, scope=scope,
                                       titles=titles, books=books, registry=registry,
                                       book_ids=scope_books, on_event=on_event, subject=subject,
                                       covered=[d.heading for d in done])
        except Exception as err:  # noqa: BLE001 -- one section failing leaves the others
            log.exception("deep section %r failed", section.heading)
            failures.append(err)
            emit(on_event, "section", index=index, state="done", kind="section",
                 heading=section.heading, style="failed", text="", prose=[], claims=[],
                 claims_range=None, passages=[], note="the section could not be written")
            emit(on_event, "step", name="section", state="done", part=index, detail="failed")
            continue
        edges += result.edges
        for s in result.seeds:
            seeds_seen.setdefault(s.id, s)
        if result.section is None:
            empties += 0 if result.offtopic or result.redundant else 1
            emit(on_event, "section", index=index, state="done", kind="section",
                 heading=section.heading, style="empty", text="", prose=[], claims=[],
                 claims_range=None, passages=[], note=None)
            emit(on_event, "step", name="section", state="done", part=index, detail="nothing found")
            continue
        done.append(result.section)
        emit(on_event, "section",
             **_section_event(index, "section", result.section, section.heading, result.claims,
                              result.new_passages, result.section.style),
             title=section.focus)
        emit(on_event, "step", name="section", state="done", part=index,
             detail=f"{len(result.claims)} claims")
    if not done:
        if failures and len(failures) + len(skipped) == len(planned):
            raise failures[-1]  # nothing worked, and not for want of budget: say why
        return None

    claims, passages = registry.claims, registry.passages
    # One `evidence` event for the whole answer (every section's relationships and the
    # passages its claims cite), as an ordinary answer sends before it writes.
    emit(on_event, "evidence", chunk_ids=[p.chunk_id for p in passages],
         edge_keys=[edge_key(e) for e in dict.fromkeys(edges)])
    # The short answer, from the claims of all the sections.
    short = await short_answer(question, done, claims, passages, models,
                               analytical=bool(getattr(raw, "analytical", False)),
                               book_titles=titles, books=dict(books))
    emit(on_event, "short_answer", prose=list(short), text=compose_text(short),
         claims=[c.model_dump() for c in claims], passages=[_passage_json(p) for p in passages])

    sections = list(done)

    note_parts = [CHECK_SKIPPED_NOTE] if any(
        s.note and CHECK_SKIPPED_NOTE in s.note for s in sections) else []
    if skipped:
        note_parts.append("stopped at the request limit; not written: " + "; ".join(skipped))
    return Answer(
        text=compose_sections(sections, short), passages=tuple(passages), mode="graph",
        template="deep", note="; ".join(note_parts) or None,
        model=models.synthesis_model(), sub_questions=tuple(s.heading for s in sections),
        claims=tuple(claims), edges=tuple(dict.fromkeys(edges)), seeds=tuple(seeds_seen.values()),
        answer_style="sectioned", sections=tuple(sections), short_answer=short,
        requests=spend.n, unwritten=len(failures) + empties)



# --- an interpretive question: its own parts and lenses are the sections ---------------

LABEL_CHARS = 48
QUESTION_HEADING_CHARS = 60


def capitalise(text: str) -> str:
    """`text` with its first letter in upper case and the rest as it was (unlike
    str.capitalize, which lowers the rest: names and acronyms keep their case)."""
    for i, ch in enumerate(text):
        if ch.isalpha():
            return text[:i] + ch.upper() + text[i + 1:]
    return text


def lens_heading(question: str, label: str = "") -> str:
    """A section's (or a part's) heading: the short label when it has one, else the
    question shortened at a word boundary (with an ellipsis), without its closing
    question mark. Always clean_heading, so no markup, citation or half word, and
    always with a capital first letter."""
    named = clean_heading(label, LABEL_CHARS).rstrip("? ").strip()
    if named:
        return capitalise(named)
    asked = clean_heading(str(question or "").strip().rstrip("? "), QUESTION_HEADING_CHARS)
    return capitalise(asked or str(question).strip())


def _renumber(prose: Sequence[ProseSentence], numbering: Mapping[int, int],
              claims: Sequence[Claim]) -> tuple[ProseSentence, ...]:
    """`prose` with each claim number mapped through `numbering` (a claim missing
    from it is dropped from the citation) and its passages recomputed from the
    claims it now cites. Code-written sentences (no claims) are kept as they are."""
    out = []
    for s in prose:
        mine = tuple(numbering[k] for k in s.claims if k in numbering)
        if s.claims and not mine:
            continue  # everything it cited is gone
        out.append(replace(s, claims=mine, passages=tuple(sorted(
            {claims[k - 1].passage for k in mine})) if s.claims else s.passages))
    return tuple(out)


# Two claims that cite the same evidence and whose words overlap this much state one fact
# (looser than the near-duplicate collapse, which drops one of them): they are written up
# in one section.
SAME_FACT_OVERLAP = 0.3


def _same_fact(a: Claim, b: Claim) -> bool:
    if _normalise(a.text) == _normalise(b.text) and _normalise(a.quote) == _normalise(b.quote):
        return True  # the very same claim, whichever copy of the passage it was found in
    return (_same_evidence(a, b) and _numbers(a.text) == _numbers(b.text)
            and _overlap(a.text, b.text) >= SAME_FACT_OVERLAP)


def keep_own(prose: Sequence[ProseSentence], first: int, last: int,
             claims: Sequence[Claim]) -> tuple[ProseSentence, ...]:
    """`prose` (the answer's numbering) limited to the claims `first`..`last`, the ones
    its section introduced: a citation of any other claim is dropped, and a sentence left
    with no citation is dropped with it. Code-written sentences (no claims) stay."""
    out = []
    for s in prose:
        mine = tuple(k for k in s.claims if first <= k <= last)
        if s.claims and not mine:
            continue
        if mine != s.claims:
            s = replace(s, claims=mine, passages=tuple(sorted(
                {claims[k - 1].passage for k in mine if 1 <= k <= len(claims)})))
        out.append(s)
    return tuple(out)


def assign_sections(claims: Sequence[Claim], found: Sequence[frozenset[int]],
                    focus: Sequence[str]) -> list[int]:
    """The section (index into `focus`, one text per section: its lens question and
    label) that writes up each claim, so that a claim is cited by exactly one section.

    A claim may go only to a lens that found it (`found`), the claim's own lens;
    with several, the one whose focus its words fit best, then the one holding the
    fewest claims, then the earlier. A claim no lens is known to have found may go
    to any section, by the same rules. Claims that state one fact (`_same_fact`) are
    placed together, among the lenses any of them came from, so a fact the lenses
    each found in their own words is not written up in several sections."""
    count = len(claims)
    leader = list(range(count))

    def root(i: int) -> int:
        while leader[i] != i:
            leader[i] = leader[leader[i]]
            i = leader[i]
        return i

    for i in range(count):
        for j in range(i):
            if _same_fact(claims[j], claims[i]):
                leader[root(i)] = root(j)
    clusters: dict[int, list[int]] = {}
    for i in range(count):
        clusters.setdefault(root(i), []).append(i)
    words = [_content_words(text) for text in focus]
    load = [0] * len(focus)
    owner = [0] * count
    for members in sorted(clusters.values(), key=lambda m: m[0]):
        own = sorted({ri for k in members for ri in found[k] if 0 <= ri < len(focus)})
        options = own or list(range(len(focus)))

        def fit(ri: int) -> int:
            return sum(len(_content_words(claims[k].text) & words[ri]) for k in members)
        best = min(options, key=lambda ri: (-fit(ri), load[ri], ri))
        for k in members:
            owner[k] = best
        load[best] += len(members)
    return owner


async def answer_lenses(question: str, plan, retrieval, evidence: Evidence, models, *, mode: str,
                        template: str | None = None, note: str | None = None,
                        book_titles: dict[str, str] | None = None,
                        books: dict[str, BookInfo] | None = None,
                        on_event=None) -> Answer:
    """A deep answer to a question with an interpretive part, organised by that
    question's own structure instead of a topical outline.

    The claims come per retrieval part as in `synthesize_shaped`, with the claim
    cap lifted (SECTION_CLAIMS). The reading phase comes first: one writer and
    check over every claim give the short answer (a reading per interpretive
    part), the counter-case and the limits, and the short answer is sent at
    once (`short_answer` event). Then one section per retrieval part (a lens, or
    a part of its own) is written in order, each with its own writer and check,
    no word cap, and an opening that says how its facts bear on the question;
    a part whose lenses are several is named as the section's group. The
    counter-case and the limits close the answer. A section that fails or finds
    nothing is left out and counted (`Answer.unwritten`); the request budget and
    the day's answer reserve stop further sections as in `answer_deep`.
    """
    spend = Spend()
    generate = spend.counted(models.synthesize)
    models = replace(models, synthesize=generate,
                     write=spend.counted(models.write) if models.write else None,
                     check=spend.counted(models.check) if models.check else None)
    day_ok = models.spend_ok
    models = replace(models, spend_ok=repair_budget(spend, day_ok, 0))
    books = dict(books or {})

    def answer(text: str, *extra: str | None, claims=(), **more) -> Answer:
        notes = "; ".join(n for n in (note, *extra) if n) or None
        return Answer(text=text, passages=evidence.passages, mode=mode, template=template,
                      note=notes, claims=tuple(claims), model=models.synthesis_model(),
                      requests=spend.n, **more)

    if not evidence.passages:
        return answer(REFUSAL)
    gathered = await shaped_claims(question, plan, retrieval, evidence, generate,
                                   book_titles=book_titles, books=books,
                                   viewpoint=models.viewpoint, max_claims=SECTION_CLAIMS)
    shape = gathered.shape
    # The reading phase: the writer sees every claim and the check judges them all.
    kept, _cut, prose, skipped, _prose_note, _unanswered, _gaps = await _finish(
        question, list(shape.parts), gathered.kept, evidence, models.check, models.write,
        book_titles, part_claims=None, books=books, shape=shape,
        spend_ok=getattr(models, "spend_ok", None))
    if not kept:
        return answer(REFUSAL)
    at = [gathered.kept.index(c) for c in kept]  # position in `gathered.kept` of each survivor
    found = [shape.claim_parts[i] for i in at]
    # Each claim is written up by one section only: the lens that found it, or the best fit.
    owner = assign_sections(kept, found, [f"{r.question} {r.label}" for r in retrieval])
    order = sorted(range(len(kept)), key=lambda k: (owner[k], k))
    numbering = {k + 1: n for n, k in enumerate(order, start=1)}  # survivor number -> final number
    final = [kept[k] for k in order]
    configured = models.write is not None and models.check is not None
    short = tuple(s for s in prose if s.short) if prose else (
        interpret.short_answer(shape) if shape.interpretive and configured else ())
    short = _renumber(short, numbering, final)
    argument = tuple(s for s in _renumber(prose, numbering, final)
                     if not s.short and s.kind in ("counter", "limit"))

    sections_of = [[n for n, k in enumerate(order, start=1) if owner[k] == ri]
                   for ri in range(len(retrieval))]
    planned = [ri for ri, mine in enumerate(sections_of) if mine]
    # A lens that found nothing (or nothing that held up) is unwritten; one whose claims all
    # went to another section is not: its facts are written up there.
    unwritten = sum(1 for ri in range(len(retrieval)) if not any(ri in f for f in found))
    several = len(plan) > 1
    group_of = {ri: (lens_heading(plan[r.part].question, getattr(plan[r.part], "label", ""))
                     if r.lens and several else "") for ri, r in enumerate(retrieval)}
    group_titles = {ri: (plan[r.part].question if group_of[ri] else "") for ri, r in enumerate(retrieval)}
    headings = {ri: lens_heading(retrieval[ri].question, retrieval[ri].label) for ri in planned}
    titles = {ri: retrieval[ri].question for ri in planned}  # the full lens question, for a tooltip
    emit(on_event, "plan", scope="", candidates=0,
         sections=[{"index": ri, "heading": headings[ri], "group": group_of[ri],
                    "group_title": group_titles[ri], "title": titles[ri], "entities": []}
                   for ri in planned])
    by_n = {p.n: p for p in evidence.passages}
    sent: set[int] = set()

    def fresh(numbers) -> list[Passage]:
        out = []
        for n in numbers:
            p = by_n[final[n - 1].passage]
            if p.n not in sent:
                sent.add(p.n)
                out.append(p)
        return out

    emit(on_event, "short_answer", prose=list(short),
         text=compose_text(short) if short else "",
         claims=[c.model_dump() for c in final], passages=[_passage_json(p) for p in fresh(range(1, len(final) + 1))])

    sections: list[AnswerSection] = []
    not_written: list[str] = []
    failures: list[BaseException] = []
    for ri in planned:
        heading = headings[ri]
        mine = sections_of[ri]
        reason = None
        if spend.n + SECTION_REQUESTS - 1 > DEEP_MAX_REQUESTS:
            reason = "budget"
        elif day_ok is not None and not day_ok():
            reason = "reserve"
        if reason:
            not_written.append(heading)
            emit(on_event, "section", index=ri, state="skipped", kind="section", heading=heading,
                 reason=reason)
            continue
        emit(on_event, "step", name="section", state="start", part=ri, detail=None)
        emit(on_event, "section", index=ri, state="start", kind="section", heading=heading,
             group=group_of[ri], group_title=group_titles[ri], title=titles[ri])
        owned = [final[n - 1] for n in mine]
        served = plan[retrieval[ri].part].question
        lens = retrieval[ri].question
        ask = lens if served == lens else f"{lens} (one aspect of the larger question: {served})"
        try:
            got = await cited_prose(ask, [lens], owned, evidence.passages, models, length="lens",
                                    book_titles=book_titles, books=books,
                                    covered=[s.heading for s in sections if s.kind == "section"])
        except Exception as err:  # noqa: BLE001 -- one section failing leaves the others
            log.exception("deep section %r failed", heading)
            failures.append(err)
            unwritten += 1
            emit(on_event, "section", index=ri, state="done", kind="section", heading=heading,
                 style="failed", text="", prose=[], claims=[], claims_range=None, passages=[],
                 note="the section could not be written")
            emit(on_event, "step", name="section", state="done", part=ri, detail="failed")
            continue
        start = mine[0]
        if got:
            prose_of = keep_own(remap_prose(got, start - 1, final), start, mine[-1], final)
            text, style = compose_text(prose_of), "prose"
        else:
            prose_of, text, style = (), _compose(owned), "claims"
        result = AnswerSection(heading=heading, prose=prose_of, text=text, claims_start=start,
                               claims_end=mine[-1], style=style, group=group_of[ri],
                               group_title=group_titles[ri], title=titles[ri],
                               note=None if got else PROSE_FALLBACK_NOTE)
        sections.append(result)
        emit(on_event, "section", **_section_event(ri, "section", result, heading, owned,
                                                   fresh(mine), style),
             group=group_of[ri], group_title=group_titles[ri], title=titles[ri])
        emit(on_event, "step", name="section", state="done", part=ri, detail=f"{len(owned)} claims")
    if not sections and not not_written:
        if failures:
            raise failures[-1]
    if not sections:  # nothing could be written section by section: the checked facts stand
        text = _compose(final)
        if short:
            text = compose_sections([], short) + "\n\n" + text
        return answer(text, skipped, claims=final, short_answer=short, unwritten=unwritten)

    if argument:
        close = AnswerSection(heading=CLOSE_HEADING, prose=argument, text=compose_text(argument),
                              claims_start=0, claims_end=-1, style="prose", kind="close")
        sections.append(close)
        emit(on_event, "section", index=len(retrieval), state="start", kind="close",
             heading=close.heading)
        emit(on_event, "section", **_section_event(len(retrieval), "close", close, close.heading,
                                                   [], fresh([n for s in argument for n in s.claims]),
                                                   close.style))
    notes = [skipped] if skipped else []
    if not_written:
        notes.append("stopped at the request limit; not written: " + "; ".join(not_written))
    return answer(compose_sections(sections, short), *notes, claims=final,
                  answer_style="sectioned", sections=tuple(sections), short_answer=short,
                  unwritten=unwritten)
