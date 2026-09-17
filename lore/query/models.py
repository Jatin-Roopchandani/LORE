"""Shared types for the query agent. No logic, no cross-module imports."""
from __future__ import annotations

from dataclasses import dataclass


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


@dataclass(frozen=True)
class Passage:
    n: int
    chunk_id: str
    book_id: str
    chapter_seq: int
    text: str


@dataclass(frozen=True)
class Evidence:
    edges: tuple[EdgeRow, ...]
    passages: tuple[Passage, ...]
    truncated: bool


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
