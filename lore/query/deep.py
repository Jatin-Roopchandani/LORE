"""Deep answers: an outline built from the graph and the codex, then one
section at a time (spec 2026-10-30, sections 10-12).

A broad question ("tell me about ...") deserves a detailed, sourced chapter, not
a paragraph. For one, the planner gathers the most significant entities in the
question's scope (a world, some books, or one named entity's facets), makes ONE
model call that groups them into sections, and code validates the result
(unknown entities dropped, empty sections dropped, at most DEEP_MAX_SECTIONS).

Each section is then answered as a part of its own: its entities are the seeds,
with their relationships, their timeline and a capped slice of their codex
entries as evidence; it has its own claims step, its own check and its own
writer, which has no word cap (`synthesize.synthesize(section=...)`). A section
that passes streams to the UI on its own; one that fails falls back to its
claim list without touching the others. The answer is a short overview built
from a few checked claims of each section, a contents list, the sections, and,
when the question is interpretive, a closing section with LORE's reading, the
counter-case and the limits (interpret.py).

Faithfulness is as everywhere else: every sentence cites checked claims and
every claim quotes a numbered passage. The planner, the codex and the timeline
only say where to look.

Gated by `decompose.DEEP`. Nothing here knows any library or any question.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

from pydantic import BaseModel

from lore.beats.store import beats_for_entities
from lore.codex import store as codex_store
from lore.extract.embed import QUERY_PREFIX
from lore.query import codexslice, interpret
from lore.query import timeline as timeline_module
from lore.query import write as write_module
from lore.query.events import emit
from lore.query.models import (
    Answer, AnswerSection, BookInfo, Claim, EdgeRow, Evidence, Passage, ProseSentence,
    SectionSpec, SeedEntity, TemplateCall,
)
from lore.query.retrieval import search_chunks
from lore.query.scope import Scope
from lore.query.seed import _entities, named_seeds
from lore.query.subgraph import assemble_ranked, relevance, run_template
from lore.query.synthesize import (
    CHECK_SKIPPED_NOTE, PROSE_FALLBACK_NOTE, REFUSAL, _compose, check_answer, synthesize,
)
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
SECTION_CLAIMS = 16
SECTION_CHARS = 18_000
SECTION_EDGES_PER_BOOK = 10
SECTION_NEAR_K = 8
SECTION_SPREAD = 12
SECTION_RELEVANT = 12
# Requests: a section costs its claims call, its writer and its check; the
# overview and the interpretive close a writer and a check each. The deep path
# stops starting sections when the next would pass DEEP_MAX_REQUESTS (leaving
# room for the tail), or when the models' `spend_ok` says the day's answer
# reserve is reached.
DEEP_MAX_REQUESTS = 45
SECTION_REQUESTS = 3
TAIL_REQUESTS = 2
# Claims of each section the overview and the close read.
OVERVIEW_CLAIMS_PER_SECTION = 2
CLOSE_CLAIMS_PER_SECTION = 4
CLOSE_HEADING = "Reading, counter-case and limits"
# Facets of one named entity.
MAX_FACET_RELATIONS = 8
PERIODS = 3
MIN_PERIOD_BEATS = 6
MAX_SUMMARY_CHARS = 160
MAX_HEADING_CHARS = 80

# Candidate kinds, in the order the planner sees them.
KINDS = ("Concept", "Faction", "Place", "Object", "Character", "Event")


# --- the planner -----------------------------------------------------------------

class PlanSection(BaseModel):
    heading: str = ""
    entities: list[str] = []


class DeepPlan(BaseModel):
    overview: str = ""
    sections: list[PlanSection] = []


PLANNER_PROMPT = """You are planning the table of contents of a long, detailed reference answer, built from
the books of a story world.

Question: {question}

Candidates: the people, groups, places, things and ideas that the books establish
within the scope of the question, grouped by kind and most significant first. Each
line gives an id, a name and, where known, one line saying what it is.
{candidates}

Group the candidates into sections that together answer the question in full.
Rules:
- Give between {min_sections} and {max_sections} sections. Each has a short, plain
  "heading" and "entities": the ids of the candidates it covers.
- Cover every major aspect of the question that the candidates show. Leave a
  candidate out only when it is minor.
- Put foundations before details, so that a reader meets an idea before the
  things built on it. Keep related candidates in one section.
- Use only ids from the list above. Never add a section for something that is not
  among the candidates.
- A candidate may be in two sections when it matters to both, but no section
  should repeat another.
- "overview" is one plain sentence saying what the whole answer covers.
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

    @property
    def entity_ids(self) -> list[str]:
        return list(dict.fromkeys(e for c in self.candidates for e in c.entity_ids))[:SECTION_ENTITIES]


def _batches(items: Sequence[str], size: int = 500):
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def _one_line(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _counts(conn: sqlite3.Connection, sql: str, ids: Sequence[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for batch in _batches(list(ids)):
        try:
            for key, n in conn.execute(sql.format(marks=",".join("?" * len(batch))), batch):
                out[key] = int(n)
        except sqlite3.OperationalError:  # a library not migrated to beats or the codex yet
            return {}
    return out


def _richness(conn: sqlite3.Connection, ids: Sequence[str]) -> tuple[dict[str, int], dict[str, int]]:
    """(beats, codex statements) per entity id."""
    beats = _counts(conn, "SELECT entity_id, COUNT(DISTINCT beat_id) FROM beat_entities"
                          " WHERE entity_id IN ({marks}) GROUP BY entity_id", ids)
    cx = _counts(conn, "SELECT entity_id, COUNT(*) FROM codex_entries"
                       " WHERE entity_id IN ({marks}) GROUP BY entity_id", ids)
    return beats, cx


def _summaries(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for entity_id in ids:
            found = codex_store.summary_for(conn, entity_id)
            if found and found.summary:
                out[entity_id] = _one_line(found.summary, MAX_SUMMARY_CHARS)
    except sqlite3.OperationalError:
        return {}
    return out


def _span(kuzu, book_ids: frozenset[str] | None) -> dict[str, set[tuple[str, int]]]:
    """Per live entity, the (book, chapter) places its relationships fall in."""
    where = "WHERE r.book_id IN $books " if book_ids else ""
    params = {"books": sorted(book_ids)} if book_ids else {}
    places: dict[str, set[tuple[str, int]]] = {}
    for end in ("a", "b"):
        for entity_id, book, chapter, _ in kuzu.execute(
                f"MATCH (a:Entity)-[r:REL]->(b:Entity) {where}"
                f"RETURN {end}.id, r.book_id, r.chapter_seq, count(r)", params):
            places.setdefault(entity_id, set()).add((str(book), int(chapter)))
    return places


def _scoped_entities(kuzu, book_ids: frozenset[str] | None, book_names: frozenset[str]) -> dict[str, tuple]:
    rows = kuzu.execute("MATCH (e:Entity) WHERE e.kind IN $kinds RETURN e.id, e.name, e.kind, e.books",
                        {"kinds": list(KINDS)})
    return {r[0]: (str(r[1]), str(r[2]), tuple(r[3] or ())) for r in rows
            if (book_ids is None or any(b in book_ids for b in r[3] or ()))
            and normalize_name(str(r[1] or "")) not in book_names}


def gather_candidates(conn: sqlite3.Connection, kuzu, *, book_ids: frozenset[str] | None,
                      book_names: frozenset[str] = frozenset(),
                      focus: Sequence[str] = ()) -> list[Candidate]:
    """The most significant entities in scope, at most PER_KIND of each kind,
    ordered by kind (KINDS) and within a kind by significance: how many places in
    the story its relationships fall in, plus its beats and its codex
    statements. Entities the question names (`focus`) always stay. Book titles
    and entities with no relationships are left out."""
    entities = _scoped_entities(kuzu, book_ids, book_names)
    places = _span(kuzu, book_ids)
    live = [i for i in entities if i in places]
    beats, statements = _richness(conn, live)
    summaries = _summaries(conn, live)
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
    summary = _one_line(found[0].summary, MAX_SUMMARY_CHARS) if found else ""
    out = [Candidate(id=focal.id, name=focal.name, kind=focal.kind, entity_ids=(focal.id,),
                     summary=summary)]
    if found:
        for aspect in codex_store.ASPECTS:
            texts = [s.text for s in found[0].statements if s.aspect == aspect]
            if texts:
                out.append(Candidate(
                    id=f"{focal.id}#{aspect}", name=f"{focal.name}: {aspect}", kind="Aspect",
                    entity_ids=(focal.id,), summary=_one_line(texts[0], MAX_SUMMARY_CHARS),
                    score=len(texts), aspect=aspect))
    beats = _beats_of([focal.id], conn, book_ids)
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
    where = "AND r.book_id IN $books " if book_ids else ""
    params = {"id": focal.id, "limit": MAX_FACET_RELATIONS * 3}
    if book_ids:
        params["books"] = sorted(book_ids)
    neighbours = kuzu.execute(
        f"MATCH (a:Entity)-[r:REL]-(b:Entity) WHERE a.id = $id AND b.id <> $id {where}"
        "RETURN b.id AS other, count(r) AS n ORDER BY n DESC, other LIMIT $limit", params)
    ranked = [(str(r[0]), int(r[1])) for r in neighbours]
    known = _scoped_entities(kuzu, book_ids, book_names)
    summaries = _summaries(conn, [i for i, _ in ranked])
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
    return [b for b in beats if not book_ids or b.book_id in book_ids]


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
    no entity is dropped; a section whose heading repeats an earlier one joins
    it; a blank heading becomes the name of the section's first candidate; and
    the plan is cut to `max_sections` (DEEP_MAX_SECTIONS)."""
    cap = DEEP_MAX_SECTIONS if max_sections is None else max_sections
    known = {c.id: c for c in candidates}
    sections: dict[str, tuple[str, list[Candidate]]] = {}  # casefolded heading -> (heading, members)
    for item in getattr(raw, "sections", None) or []:
        ids = [i for i in dict.fromkeys(str(x).strip() for x in (getattr(item, "entities", None) or []))
               if i in known]
        if not ids:
            continue
        members = [known[i] for i in ids]
        heading = _one_line(getattr(item, "heading", ""), MAX_HEADING_CHARS).strip(" #*:.") or members[0].name
        mine = sections.setdefault(heading.casefold(), (heading, []))[1]
        mine += [c for c in members if c not in mine]
    return [PlannedSection(h, tuple(cs)) for h, cs in list(sections.values())[:max(cap, 0)]]


# --- one section -----------------------------------------------------------------

@dataclass
class Spend:
    """Counts the model requests of one deep answer."""

    n: int = 0

    def counted(self, generate):
        async def call(prompt, schema):
            self.n += 1
            return await generate(prompt, schema)
        return call


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
        before = len(self.passages)
        offset = len(self.claims)
        mapped = [c.model_copy(update={"passage": self.passage(local[c.passage])})
                  for c in claims if c.passage in local]
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


def section_question(heading: str, names: Sequence[str]) -> str:
    return f"{heading} ({', '.join(names)})" if names else heading


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
                      book_ids: frozenset[str] | None) -> SectionResult:
    """Answer one section from its own evidence: its entities' relationships,
    timeline and codex slice, and the passages nearest it. The section's claims
    and prose come back in the answer's numbering (the registry's)."""
    seeds = _entities(kuzu, section.entity_ids, {i: 0.0 for i in section.entity_ids},
                      frozenset(), scope.books)
    names = [s.name for s in seeds]
    ask = section_question(section.heading, names)
    rows: list[EdgeRow] = []
    for seed in seeds:
        found = run_template(kuzu, TemplateCall("across_books", {
            "entity_id": seed.id, "limit": SECTION_EDGES_PER_BOOK}))
        rows += [r for r in found if scope.allows(r.book_id)]
    aspects, beat_ids = _restrictions(section)
    shown = book_ids if book_ids else None
    seed_ids = {s.id for s in seeds}

    def wanted(b) -> bool:  # a beat of an entity the section narrows is kept only if it is in its stretch
        return any(e not in beat_ids or b.id in beat_ids[e] for e in b.entity_ids if e in seed_ids)

    beats = [b for b in _beats_of([s.id for s in seeds if s.kind in timeline_module.TIMELINE_KINDS],
                                  conn, shown) if wanted(b)]
    beats = timeline_module.select_beats(beats, vector, lance, books) if beats else []
    lines = codexslice.lines_for(conn, [s.id for s in seeds], book_ids=shown, aspects=aspects)
    lines = (codexslice.select_lines(conn, lines, [s.id for s in seeds], vector, lance, books,
                                     SECTION_SPREAD, SECTION_RELEVANT) if lines else [])
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
                               score_lists=[scores], **extra)
    if not evidence.passages:
        return SectionResult(None, [], [], [], seeds)
    spec = SectionSpec(part_of=question, max_claims=SECTION_CLAIMS, length="section")
    answer = await synthesize(ask, evidence, models.synthesize, mode="graph", check=models.check,
                              write=models.write, book_titles=titles, books=dict(books), section=spec)
    if not answer.claims:
        return SectionResult(None, [], [], list(evidence.edges), seeds)
    local = {p.n: p for p in evidence.passages}
    claims, offset, added = registry.add(answer.claims, local)
    prose = remap_prose(answer.prose, offset, registry.claims) if answer.prose else ()
    if prose:
        text, style = compose_text(prose, answer.unanswered, answer.gaps), "prose"
    else:
        text, style = _compose(claims), "claims"
    note = answer.note
    result = AnswerSection(heading=section.heading, prose=prose, text=text,
                           claims_start=offset + 1, claims_end=offset + len(claims), style=style,
                           entities=tuple(names), note=note)
    return SectionResult(result, claims, added, list(evidence.edges), seeds)


# --- cited prose over the answer's claims (overview, close) ---------------------------

async def cited_prose(question: str, parts: Sequence[str], claims: Sequence[Claim],
                      passages: Sequence[Passage], models, *, length: str | None,
                      shape: "interpret.Shape | None" = None,
                      book_titles: dict[str, str] | None = None,
                      books: dict[str, BookInfo] | None = None,
                      ) -> tuple[ProseSentence, ...] | None:
    """Prose over `claims` that passed the writer, the code guards and the
    check, or None. The sentences' claim numbers are 1-based into `claims`
    (already mapped back from the check's survivors); their passages are the
    claims' own."""
    if models.write is None or models.check is None or not claims:
        return None
    drafted = await write_answer(question, list(parts), claims, passages, models.write,
                                 book_titles=book_titles, books=books, shape=shape, length=length)
    if drafted is None:
        return None
    sentences = verify_prose(drafted, len(claims), [c.text for c in claims], shape)
    if not sentences:
        return None
    outcome = await check_answer(claims, passages, models.check, book_titles=book_titles,
                                 sentences=sentences, books=books, shape=shape)
    if not outcome.ran or outcome.sentence_ok is None:
        return None
    built = assemble(sentences, len(split_items(drafted)), outcome.claim_ok, outcome.sentence_ok,
                     [c.passage for c in claims])
    if not built:
        return None
    survivors = [k for k, ok in enumerate(outcome.claim_ok, start=1) if ok]
    return tuple(replace(s, claims=tuple(survivors[k - 1] for k in s.claims)) for s in built)


def _digest(sections: Sequence[AnswerSection], per: int) -> list[tuple[int, int]]:
    """(section index, answer claim number) of the first `per` claims of each
    section."""
    out = []
    for i, s in enumerate(sections):
        end = min(s.claims_end, s.claims_start + per - 1)
        out += [(i, k) for k in range(s.claims_start, end + 1)]
    return out


def interpretive_shape(plan, sections: Sequence[AnswerSection], digest, viewpoint: str):
    """The Task 3 shape for the close: each section is a lens of the first
    interpretive part, so a reading must rest on facts from at least two
    sections; beyond parts give the limits line."""
    kept = [(p.question, p.kind) for p in plan.parts if p.kind in ("interpretive", "beyond")]
    kinds = tuple(k for _, k in kept)
    at = kinds.index("interpretive") if "interpretive" in kinds else 0
    shape = interpret.Shape(viewpoint=viewpoint, parts=tuple(q for q, _ in kept), kinds=kinds,
                            retrieval=tuple((s.heading, at, True) for s in sections))
    return shape.with_claims([frozenset({i}) for i, _ in digest])


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
    for each section in order, `overview`, and a closing `section` of kind
    "close" for an interpretive question."""
    spend = Spend()
    models = replace(models, synthesize=spend.counted(models.synthesize),
                     write=spend.counted(models.write) if models.write else None,
                     check=spend.counted(models.check) if models.check else None)
    scope_books = book_ids or scope.books
    focal = None
    if len(named_ids) >= 1:
        seeds = await named_seeds(kuzu, list(named_ids), exclude_names=book_names,
                                  book_ids=scope.books)
        focal = seeds[0] if len(seeds) == 1 else None
    emit(on_event, "step", name="plan", state="start", part=None, detail=None)
    candidates = (facet_candidates(conn, kuzu, focal, book_ids=scope_books, books=books,
                                   book_names=book_names)
                  if focal is not None else
                  gather_candidates(conn, kuzu, book_ids=scope_books, book_names=book_names,
                                    focus=list(named_ids)))
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
         sections=[{"index": i, "heading": s.heading,
                    "entities": [{"id": c.id, "name": c.name, "kind": c.kind}
                                 for c in s.candidates]} for i, s in enumerate(planned)])

    closes = (plan is not None and interpret.active()
              and interpret.shaped([p.kind for p in plan.parts]))
    # The overview's writer and check, and the close's when there is a close.
    tail = TAIL_REQUESTS * (2 if closes else 1)
    vectors = await models.embed([QUERY_PREFIX + section_question(
        s.heading, [c.name for c in s.candidates]) for s in planned])
    registry = Registry()
    done: list[AnswerSection] = []
    edges: list[EdgeRow] = []
    seeds_seen: dict[str, SeedEntity] = {}
    skipped: list[str] = []
    failures: list[BaseException] = []
    for index, (section, vec) in enumerate(zip(planned, vectors)):
        reason = None
        if spend.n + SECTION_REQUESTS + tail > DEEP_MAX_REQUESTS:
            reason = "budget"
        elif models.spend_ok is not None and not models.spend_ok():
            reason = "reserve"
        if reason:
            skipped.append(section.heading)
            emit(on_event, "section", index=index, state="skipped", kind="section",
                 heading=section.heading, reason=reason)
            continue
        emit(on_event, "step", name="section", state="start", part=index, detail=None)
        emit(on_event, "section", index=index, state="start", kind="section", heading=section.heading)
        try:
            result = await run_section(index, section, vec, question=question, conn=conn,
                                       kuzu=kuzu, lance=lance, models=models, scope=scope,
                                       titles=titles, books=books, registry=registry,
                                       book_ids=scope_books)
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
            emit(on_event, "section", index=index, state="done", kind="section",
                 heading=section.heading, style="empty", text="", prose=[], claims=[],
                 claims_range=None, passages=[], note=None)
            emit(on_event, "step", name="section", state="done", part=index, detail="nothing found")
            continue
        done.append(result.section)
        emit(on_event, "section",
             **_section_event(index, "section", result.section, section.heading, result.claims,
                              result.new_passages, result.section.style))
        emit(on_event, "step", name="section", state="done", part=index,
             detail=f"{len(result.claims)} claims")
    if not done:
        if failures and len(failures) + len(skipped) == len(planned):
            raise failures[-1]  # nothing worked, and not for want of budget: say why
        return None

    claims, passages = registry.claims, registry.passages
    book_infos = write_module.book_infos(titles, dict(books))
    # The overview: a few of the most important checked claims of each section.
    digest = _digest(done, OVERVIEW_CLAIMS_PER_SECTION)
    overview: tuple[ProseSentence, ...] = ()
    numbers = [k for _, k in digest]
    got = await cited_prose(question, [question], [claims[k - 1] for k in numbers], passages,
                            models, length="overview", book_titles=titles, books=dict(books))
    if got is not None:
        overview = tuple(replace(s, claims=tuple(numbers[k - 1] for k in s.claims)) for s in got)
    emit(on_event, "overview", prose=list(overview),
         text=compose_text(overview) if overview else "")

    sections = list(done)
    if closes:
        close = await interpretive_close(plan, done, claims, passages, models, question,
                                         titles, dict(books), len(planned))
        sections.append(close)
        emit(on_event, "section", index=len(planned), state="start", kind="close",
             heading=close.heading)
        emit(on_event, "section", **_section_event(len(planned), "close", close, close.heading,
                                                   [], [], close.style))

    note_parts = [CHECK_SKIPPED_NOTE] if any(
        s.note and CHECK_SKIPPED_NOTE in s.note for s in sections) else []
    if skipped:
        note_parts.append("stopped at the request limit; not written: " + "; ".join(skipped))
    return Answer(
        text=compose_sections(overview, sections), passages=tuple(passages), mode="graph",
        template="deep", note="; ".join(note_parts) or None,
        model=models.synthesis_model(), sub_questions=tuple(s.heading for s in sections),
        claims=tuple(claims), edges=tuple(dict.fromkeys(edges)), seeds=tuple(seeds_seen.values()),
        answer_style="sectioned", sections=tuple(sections), overview=overview,
        requests=spend.n)


async def interpretive_close(plan, sections: Sequence[AnswerSection], claims: Sequence[Claim],
                             passages: Sequence[Passage], models, question: str,
                             titles: dict[str, str], books: dict[str, BookInfo],
                             index: int) -> AnswerSection:
    """The closing section of an interpretive question: LORE's reading, the
    counter-case and the limits, over a few claims of each section. Sentences
    that fail a guard or the check are dropped, and code says what the evidence
    or the books do not settle (interpret.close_answer), so it is never silent."""
    from lore.query.viewpoint import load_viewpoint
    digest = _digest(sections, CLOSE_CLAIMS_PER_SECTION)
    shape = interpretive_shape(plan, sections, digest,
                               models.viewpoint if models.viewpoint is not None else load_viewpoint())
    kept: tuple[ProseSentence, ...] = ()
    if shape.interpretive:
        got = await cited_prose(question, list(shape.parts), [claims[k - 1] for _, k in digest],
                                passages, models, length=None, shape=shape,
                                book_titles=titles, books=books)
        if got is not None:
            numbers = [k for _, k in digest]
            kept = tuple(replace(s, claims=tuple(numbers[k - 1] for k in s.claims))
                         for s in got if s.kind in ("reading", "counter"))
    prose = interpret.close_answer(kept, shape, ())
    return AnswerSection(heading=CLOSE_HEADING, prose=prose, text=compose_text(prose),
                         claims_start=0, claims_end=-1, style="prose", kind="close")
