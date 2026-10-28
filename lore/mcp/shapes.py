"""The JSON every tool returns.

Structured because the Phase 5 UI is a first-class consumer: it draws the
subgraph and makes each claim clickable, so prose it would have to re-parse
is not good enough.

No tool raises through the protocol. Each returns {"error": {...}} with one
of ERROR_CODES, so a client branches on the code and a human reads the
message.
"""
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel

from lore.query.models import Answer

ERROR_CODES = frozenset(
    {"no_graph", "budget_exhausted", "cypher_rejected", "not_found", "timeout",
     "upstream_unavailable"}
)


class ToolError(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    error: ToolError


def error(code: str, message: str) -> dict:
    # A real raise, not an assert: asserts vanish under `python -O`, and an
    # undeclared code reaching a client would break the fixed code set.
    if code not in ERROR_CODES:
        raise ValueError(f"undeclared error code: {code}")
    return ErrorEnvelope(error=ToolError(code=code, message=message)).model_dump()


class PassageOut(BaseModel):
    n: int
    chunk_id: str
    book_id: str
    book_title: str
    chapter_seq: int
    text: str


class ClaimOut(BaseModel):
    # Survivors only: a claim's presence is its verdict, so no verdict field.
    text: str
    passage: int
    quote: str


class EdgeOut(BaseModel):
    subject: str
    predicate: str
    predicate_raw: str
    object: str
    evidence: str
    confidence: float
    book_id: str
    chapter_seq: int
    chunk_id: str
    # subject/object are NAMES everywhere (as in the query agent's EdgeRow).
    # Names are not unique, so graph tools also fill the ids, which a UI
    # joins to `nodes`; "" only for producers that have none.
    subject_id: str = ""
    object_id: str = ""


class NodeOut(BaseModel):
    id: str
    name: str
    kind: str
    books: list[str]


class SeedOut(BaseModel):
    id: str
    name: str
    kind: str
    books: list[str]
    # A squared-L2 distance (lower is nearer). 0.0 means the entity was
    # NAMED in the question: named seeds score 0.0 by construction, so it
    # does not mean an identical vector. None when it is not finite:
    # seed.py scores a chunk with no `_distance` as inf, and json.dumps
    # writes that as `Infinity`, which strict JSON-RPC clients reject.
    score: float | None


class ProseOut(BaseModel):
    text: str
    claims: list[int]
    passages: list[int]
    new_paragraph: bool = False


class SearchResult(BaseModel):
    answer: str
    mode: str
    template: str | None
    note: str | None
    model: str | None
    selection_model: str | None
    sub_questions: list[str]
    claims: list[ClaimOut]
    passages: list[PassageOut]
    edges: list[EdgeOut]
    seeds: list[SeedOut]
    prose: list[ProseOut] = []
    answer_style: str = "claims"
    unanswered: list[str] = []


class ChunkHit(BaseModel):
    chunk_id: str
    book_id: str
    book_title: str
    chapter_seq: int
    text: str
    # Squared-L2, lower is nearer. None when missing or not finite: `inf` and
    # `nan` are not valid JSON.
    distance: float | None


class CypherResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


class EntityCard(BaseModel):
    id: str
    name: str
    kind: str
    aliases: list[str]
    books: list[str]
    edges_out: list[EdgeOut]
    edges_in: list[EdgeOut]
    # Each direction is capped (tools.CARD_EDGE_CAP); the totals say how
    # many edges exist, so a UI can show "50 of 172" and fetch the rest
    # through `subgraph` or `cypher_query`.
    edges_out_total: int
    edges_in_total: int
    truncated: bool


class Citation(BaseModel):
    """One passage backing a link: the extracted edge's own fields."""

    predicate: str
    predicate_raw: str
    evidence: str
    confidence: float
    book_id: str
    chapter_seq: int
    chunk_id: str


class LinkOut(BaseModel):
    """Every REL edge from one entity to another, as one drawable link.

    The subgraph returns these, not raw edges: on the live graph Jefferson
    Hope's depth-1 subgraph was 31 nodes but 205 edges -- each pair repeated
    once per passage -- an 82 KB result Claude Code refused. `count` is how
    many passages back the link, `predicates` how they split, and
    `citations` the first of them in book/chapter/chunk order. Direction is
    kept: h->w and w->h are separate links. subject/object are names,
    *_id the ids, as everywhere else.
    """

    subject: str
    subject_id: str
    object: str
    object_id: str
    count: int
    predicates: dict[str, int]
    citations: list[Citation]


class SubgraphOut(BaseModel):
    nodes: list[NodeOut]
    edges: list[LinkOut]
    truncated: bool
    # The depth actually walked after clamping, so a client that asked for
    # more can see it did not get it; `note` says why.
    depth: int
    note: str | None = None


class BookOut(BaseModel):
    id: str
    title: str
    author: str | None
    status: str
    chunk_count: int
    # None when the graph could not be opened (absent, or locked by a load):
    # unknown, which is not the same as zero. `library` says why in `note`.
    entity_count: int | None


class JobOut(BaseModel):
    kind: str
    state: str
    detail: str | None  # nullable column in the jobs table
    # The failure text set_unit_state stores (migration 3); NULL unless the
    # job failed. It is the whole point of a dead letter. Defaulted so a
    # construction without it still works.
    error: str | None = None
    updated_at: str


class IngestStatus(BaseModel):
    book_id: str
    title: str
    status: str
    jobs: list[JobOut]
    dead_letters: list[JobOut]


def to_search_result(answer: Answer, titles: dict[str, str]) -> SearchResult:
    # An unknown book falls back to its id: a readable-ish label beats a
    # KeyError or an empty string in the UI.
    def title(book_id: str) -> str:
        return titles.get(book_id, book_id)

    return SearchResult(
        answer=answer.text,
        mode=answer.mode,
        template=answer.template,
        note=answer.note,
        model=answer.model,
        selection_model=answer.selection_model,
        sub_questions=list(answer.sub_questions),
        claims=[ClaimOut(text=c.text, passage=c.passage, quote=c.quote)
                for c in answer.claims],
        passages=[PassageOut(n=p.n, chunk_id=p.chunk_id, book_id=p.book_id,
                             book_title=title(p.book_id),
                             chapter_seq=p.chapter_seq, text=p.text)
                  for p in answer.passages],
        edges=[EdgeOut(subject=e.subject, predicate=e.predicate,
                       predicate_raw=e.predicate_raw, object=e.object,
                       evidence=e.evidence, confidence=e.confidence,
                       book_id=e.book_id, chapter_seq=e.chapter_seq,
                       chunk_id=e.chunk_id,
                       subject_id=e.subject_id, object_id=e.object_id)
               for e in answer.edges],
        seeds=[SeedOut(id=s.id, name=s.name, kind=s.kind, books=list(s.books),
                       score=s.score if math.isfinite(s.score) else None)
               for s in answer.seeds],
        prose=[ProseOut(text=s.text, claims=list(s.claims), passages=list(s.passages),
                        new_paragraph=s.new_paragraph) for s in answer.prose],
        answer_style=answer.answer_style,
        unanswered=list(answer.unanswered),
    )
