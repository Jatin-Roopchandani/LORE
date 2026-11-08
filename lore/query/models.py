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
    # "fact"; "reading": an interpretation of what the cited claims show
    # together, not a direct statement; "counter": the strongest opposing
    # reading; "limit": a line written by code (no claims) saying what the
    # evidence or the books do not settle (lore/query/interpret.py).
    kind: str = "fact"
    # Part of an interpretive answer's short answer (Answer.short_answer): the
    # reading for one interpretive part, or code's line when it has none.
    short: bool = False
    # A short answer part's short label, set on the first sentence of each
    # part when the question has several interpretive parts; "" otherwise.
    # Shown muted before the sentence, never part of it.
    label: str = ""


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
class CodexLine:
    """One codex line for the claims prompt: a statement the books establish
    about the subject entity, under an aspect, and the passage (`chunk_id`)
    that holds its quote. Like a timeline beat it is a map, never evidence."""

    chunk_id: str
    book_id: str
    chapter_seq: int
    subject: str
    aspect: str
    text: str
    # The entity the line is about ("" where unknown); a reading position uses it
    # to show the subject under the name the reader has met.
    entity_id: str = ""


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
    # Codex lines (lore/query/codexslice.py), the same way: every part's, and
    # one tuple per part. Empty when no part has an entity with an entry.
    codex: tuple[CodexLine, ...] = ()
    codexes: tuple[tuple[CodexLine, ...], ...] = ()


@dataclass(frozen=True)
class SectionSpec:
    """How a deep answer's section is asked for (lore/query/deep.py): the
    whole question it is a section of, how many claims it may keep, how its
    writer is told to size it ("section"), and the aspect of that question the
    section answers (`focus`, "" when it has none)."""

    part_of: str
    max_claims: int
    length: str = "section"
    focus: str = ""
    # Names of the larger question's main entities (a topical question): a
    # section's facts must bear on them as well as on its focus.
    subject: tuple[str, ...] = ()
    # Headings of the sections written before this one (the writer is told not to
    # re-explain them), and a filter that drops the claims an earlier section already
    # states (called with the section's claims and its passages).
    covered: tuple[str, ...] = ()
    skip: "Callable[[list, Sequence], list] | None" = None


@dataclass(frozen=True)
class AnswerSection:
    """One section of a deep answer. `prose` sentences cite the answer's own
    claims (1-based, as in Answer.claims); `claims_start`..`claims_end` are the
    section's claims in that numbering (inclusive; 0 and -1 when it has none).
    `style` is "prose" or "claims" (the written section did not pass its check,
    so `text` is the claim list). `kind` is "section", or "close" for the
    interpretive close (reading, counter-case, limits)."""

    heading: str
    prose: tuple[ProseSentence, ...]
    text: str
    claims_start: int
    claims_end: int
    style: str = "prose"
    kind: str = "section"
    entities: tuple[str, ...] = ()
    note: str | None = None
    # The interpretive part this section's lens belongs to, named only when a
    # question has several parts; "" otherwise.
    group: str = ""
    # The part's full question (`group` is its short label); "" when there is no group.
    group_title: str = ""
    # The full question a lens section answers (its heading is a short label); "" otherwise.
    title: str = ""


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
    # A deep answer (lore/query/deep.py): the sections in order; `answer_style`
    # is "sectioned". `requests` is the number of model requests the deep path
    # made (planner, sections, short answer).
    sections: tuple[AnswerSection, ...] = ()
    requests: int = 0
    # A deep answer opens with its short answer, written after its sections: an
    # interpretive question's one reading per interpretive part (or code's
    # "evidence did not support a clear position" line), any other deep
    # question's few checked fact sentences (or code's "no direct summary could
    # be verified" line), each marked `short`. `prose` and the sections are the
    # argument that follows. Empty for every other answer.
    short_answer: tuple[ProseSentence, ...] = ()
    # Sections of a sectioned answer that could not be written (failed or found
    # nothing); they are left out of `sections`, and this counts them.
    unwritten: int = 0
    # Set only when a reading position limited the answer: "Answered from what
    # you've read: up to chapter 9 of <book>". None otherwise.
    position_note: str | None = None
    # A follow-up's question rewritten to stand alone (decompose.FOLLOWUPS),
    # when it differs from the question as typed. None otherwise.
    standalone: str | None = None
