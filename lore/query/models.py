"""Shared types for the query agent. No logic, no cross-module imports."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel


class Claim(BaseModel):
    """One sentence of an answer, with the passage it came from and a
    word-for-word quote from that passage. The quote is verified in code,
    then judged against its passage by the faithfulness pass."""

    text: str
    passage: int
    quote: str


@dataclass(frozen=True)
class BookInfo:
    """A library book as the answer steps see it. `series` and
    `series_index` are the book's story position when someone (the file's
    metadata, or `lore book series`) said so; None otherwise."""

    title: str
    series: str | None = None
    series_index: float | None = None


@dataclass(frozen=True)
class SeedEntity:
    """A canonical entity the question's vector neighbourhood pointed at."""

    id: str
    name: str
    kind: str
    books: tuple[str, ...]
    score: float


@dataclass(frozen=True)
class Template:
    name: str
    cypher: str
    slots: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class TemplateCall:
    template: str
    slots: dict[str, str | int]


@dataclass(frozen=True)
class EdgeRow:
    subject: str
    predicate: str
    predicate_raw: str
    object: str
    evidence: str
    confidence: float
    book_id: str
    chapter_seq: int
    chunk_id: str
    # Filled by every template (Phase 5a): names are not unique, and the UI
    # opens the entity that was traversed. "" where a producer has no ids.
    subject_id: str = ""
    object_id: str = ""


@dataclass(frozen=True)
class Passage:
    n: int
    chunk_id: str
    book_id: str
    chapter_seq: int
    text: str


@dataclass(frozen=True)
class ProseSentence:
    """One sentence of a written answer. `claims` are 1-based indices into
    Answer.claims (the checked survivors); `passages` are those claims'
    passage numbers, sorted and unique, for the UI's citation chips."""

    text: str
    claims: tuple[int, ...]
    passages: tuple[int, ...]
    new_paragraph: bool = False
    # "fact", or "reading": an interpretation of what the cited claims show
    # together, not a direct statement (write.READINGS).
    kind: str = "fact"


@dataclass(frozen=True)
class TimelineBeat:
    """One story-beat line for the claims prompt: a moment of the subject
    entity, and the passage (`chunk_id`) that holds its quote."""

    chunk_id: str
    book_id: str
    chapter_seq: int
    subject: str
    text: str


@dataclass(frozen=True)
class Evidence:
    edges: tuple[EdgeRow, ...]
    passages: tuple[Passage, ...]
    truncated: bool
    # One tuple of passage numbers (`Passage.n`) per sub-question, in
    # sub-question order: the passages that part answers from (Task 10e).
    # Empty when the evidence was not assembled per part.
    shares: tuple[tuple[int, ...], ...] = ()
    # Story beats (lore/query/timeline.py): every part's, deduplicated, and
    # one tuple per part in sub-question order. Empty when no part has any,
    # which is also what every evidence built before beats holds.
    timeline: tuple[TimelineBeat, ...] = ()
    timelines: tuple[tuple[TimelineBeat, ...], ...] = ()


@dataclass(frozen=True)
class Answer:
    """`mode` keeps the spec's four outcomes distinct and testable.

    "graph"           - a template ran and returned rows
    "vector-fallback" - a template ran and returned nothing; answered from
                        chunks alone, and the user is told the graph
                        contributed nothing
    "vector-baseline" - --baseline was asked for
    "empty"           - no seed entities; a legitimate empty answer
    """

    text: str
    passages: tuple[Passage, ...]
    mode: str
    template: str | None
    note: str | None
    # The model that wrote (or refused) the answer: the Ollama model name,
    # or the Gemini id the cascade actually reached. None when no model was
    # asked, e.g. there was no evidence to show one.
    model: str | None = None
    # The model that chose the search template, named the same way. None
    # when no selection ran: --baseline, or no seed entities.
    selection_model: str | None = None
    # The parts the question was split into, in order (Task 10d). A
    # one-part question holds itself; empty when no decomposition ran
    # (--baseline), so constructions that predate it still work.
    sub_questions: tuple[str, ...] = ()
    # Task 3 (Phase 4): the structure the UI draws. Built during synthesis
    # and, before this, discarded -- _compose flattened the claims to prose
    # and the evidence edges never reached here at all.
    #
    # `claims` holds SURVIVORS only: unsupported claims are dropped before
    # composition, so a claim's presence is the verdict. When the check
    # could not run, `note` says so (CHECK_SKIPPED_NOTE).
    claims: tuple[Claim, ...] = ()
    edges: tuple[EdgeRow, ...] = ()
    seeds: tuple[SeedEntity, ...] = ()
    prose: tuple[ProseSentence, ...] = ()
    answer_style: str = "claims"
    # Sub-question texts the shown prose does not answer; empty otherwise.
    unanswered: tuple[str, ...] = ()
    # Aspects of the question, inside a part the prose does answer, that the
    # facts do not cover (checked); shown with `unanswered` in one line.
    gaps: tuple[str, ...] = ()
