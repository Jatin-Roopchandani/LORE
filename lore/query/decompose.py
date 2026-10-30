"""Question -> the self-contained parts a search must cover.

One template call from one entity is too narrow for a question with
several parts: Task 10a measured 5 of 9 weak answers losing a part that
way. So before retrieval the question is split, and each part gets its own
vector search, seeds, selection and template. A question with one part
comes back unchanged.

Determinism is the caller's job, as for selection: temperature 0 (and seed
0 on Ollama).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from pydantic import BaseModel, Field, ValidationError, field_validator

from lore.inference.base import ProviderOutputError
from lore.inference.gemini import GeminiOutputError

Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

MAX_SUB_QUESTIONS = 4
# The most retrieval parts one question may cost once its interpretive parts
# are expanded into lenses (the existing multi-part cost model). Deep
# answers budget their sections separately.
MAX_PARTS = 6
MAX_LENSES = 4

# INTERPRETIVE (2026-10-30): each part says how it can be answered
# (factual | interpretive | beyond) and an interpretive part names the
# lenses that would decide it. DEEP: the question says whether a full
# answer spans many entities or much of the story. With both off the
# prompt and the schema are byte for byte what they were before.
INTERPRETIVE = True
DEEP = True

KINDS = ("factual", "interpretive", "beyond")
DEPTHS = ("focused", "deep")

# THEMATIC_PARTS (2026-10-28): asked about a role or theme rather than a
# named person, the parts found only the most prominent character -- the
# culprit -- and missed the characters whose earlier wrongs drive the story.
# Off gives the earlier prompt byte for byte.
THEMATIC_PARTS = True
_THEMATIC_RULE = """- If the question asks about a role or a theme (a kind of character, a
  motive, a vice or a virtue) rather than a named person, make the parts
  find every character it applies to, not only the most prominent one:
  including those whose earlier actions set the events in motion.
"""
_PROMPT = """You are splitting a question about books into the parts a search must cover.
{books}
Question: {question}

Return "sub_questions": a list of 1 to 4 self-contained questions that
together ask exactly what the question asks.
- If the question has one part, return the question unchanged as the only item.
- Split per book only when the question explicitly asks about each book or
  both books ("in each novel", "in both novels", "in each book", "in both
  books"): then give one sub-question per book, naming that book.
- Otherwise keep the question's own parts. Do not split a question by book
  just because the library holds several books.
- A question that asks "who ... and why ..." is two sub-questions: one
  for who, one for why. Likewise "what ... and how ...".
{thematic}- Each sub-question must make sense on its own: repeat the names it needs
  instead of "he", "she", "it", "they" or "that".
- Do not answer, and do not add anything the question does not ask.
{extras}"""

_ITEM_RULE = """
Each item of "sub_questions" is an object with:
- "question": the self-contained question, as above.
"""
_KIND_RULE = """- "kind": how the part can be answered.
  - "factual": the books state it, in what happens and what characters do,
    say or experience.
  - "interpretive": the books give evidence but no sentence answers it. It
    asks for a judgement, an evaluation, a comparison, the meaning or
    significance of something, a reading of a character or a theme, or what
    would have happened otherwise.
  - "beyond": the books cannot settle it: the author's intent, facts about
    the real world, or events outside the text.
  When unsure, use "factual".
- "lenses": for an "interpretive" part only (otherwise an empty list), up to
  4 aspects of the story that would decide the part, each written as a
  self-contained question the text itself can answer.
  - Together the lenses must cover opposing sides of the part.
  - Each lens is concrete: what happens, what characters do, choose, hide or
    change, what is said, and how it ends.
  - No lens may assume or hint at the answer to the part.
  - Repeat the names a lens needs, as for sub-questions.
"""
_DEPTH_RULE = """
Also return "depth", either "deep" or "focused", for the question as a whole.
- "deep": a full answer must span many entities or much of the story: a whole
  world, system, institution, people or place, the whole life of a character,
  or a theme followed across a series.
- "focused": a few passages answer it: a fact, an event, a relationship, one
  choice or reason.
When unsure, use "focused".
"""


class SubQuestions(BaseModel):
    sub_questions: list[str]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class PartSpec(BaseModel):
    """One part as the model returns it. Every field is read tolerantly: a
    value of the wrong type becomes its default, so one bad field costs that
    field and not the whole decomposition."""

    question: str
    kind: str = Field(default="factual", description="factual, interpretive or beyond")
    lenses: list[str] = Field(default_factory=list)

    @field_validator("question", "kind", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> str:
        return _text(value)

    @field_validator("lenses", mode="before")
    @classmethod
    def _as_list(cls, value: Any) -> list[str]:
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


class Decomposition(BaseModel):
    """The schema when INTERPRETIVE or DEEP is on."""

    sub_questions: list[PartSpec]
    depth: str = Field(default="focused", description="focused or deep")

    @field_validator("sub_questions", mode="before")
    @classmethod
    def _items(cls, value: Any) -> list:
        # A bare string is a part with the defaults.
        return [{"question": v} if isinstance(v, str) else v
                for v in value] if isinstance(value, list) else value

    @field_validator("depth", mode="before")
    @classmethod
    def _depth_text(cls, value: Any) -> str:
        return _text(value)


@dataclass(frozen=True)
class Part:
    """A part of the question: its text, how it is answered, and (for an
    interpretive part) its lenses."""

    question: str
    kind: str = "factual"
    lenses: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    parts: tuple[Part, ...]
    depth: str = "focused"

    @property
    def questions(self) -> list[str]:
        return [part.question for part in self.parts]


def active_schema() -> type[BaseModel]:
    return Decomposition if INTERPRETIVE or DEEP else SubQuestions


def decomposition_prompt(question: str, book_titles: Sequence[str] = ()) -> str:
    # Task 10e: listed as bare context, the titles invited a split per book
    # on single-book questions (Q3). They stay, for spelling book names.
    books = ("\nBooks in the library (a reference for names only): "
             + "; ".join(book_titles) + "\n" if book_titles else "")
    extras = ((_ITEM_RULE + (_KIND_RULE if INTERPRETIVE else "")
               + (_DEPTH_RULE if DEEP else "")) if INTERPRETIVE or DEEP else "")
    return _PROMPT.format(question=question, books=books, extras=extras,
                          thematic=_THEMATIC_RULE if THEMATIC_PARTS else "")


def _clean(text: Any) -> str:
    return " ".join(str(text).split())


def _part(spec: PartSpec) -> Part | None:
    text = _clean(spec.question)
    if not text:
        return None
    kind = _clean(spec.kind).lower() if INTERPRETIVE else "factual"
    if kind not in KINDS:
        kind = "factual"
    lenses: list[str] = []
    if kind == "interpretive":
        for lens in spec.lenses:
            lens = _clean(lens)
            if lens and lens != text and lens not in lenses:
                lenses.append(lens)
        lenses = lenses[:MAX_LENSES]
        if not lenses:  # nothing to gather evidence by: answer it as today
            kind = "factual"
    return Part(text, kind, tuple(lenses))


async def plan(
    question: str, generate: Generate, *, book_titles: Sequence[str] = ()
) -> Plan | None:
    """The question's parts (1-4, each with its kind and lenses) and depth,
    or None when the model's output is unusable.

    Only malformed output gives None. A quota, transport or auth failure is
    raised: it would fail the answer's other calls too, and hiding it here
    would only move the error.
    """
    try:
        result = await generate(decomposition_prompt(question, book_titles),
                                active_schema())
    except (ProviderOutputError, GeminiOutputError, ValidationError):
        return None
    if isinstance(result, Decomposition):
        specs = result.sub_questions
        depth = _clean(result.depth).lower() if DEEP else "focused"
    elif isinstance(result, SubQuestions):
        specs = [PartSpec(question=str(p)) for p in result.sub_questions]
        depth = "focused"
    else:
        return None
    parts: list[Part] = []
    for spec in specs:
        part = _part(spec)
        if part is not None and part.question not in (p.question for p in parts):
            parts.append(part)
    if not parts:
        return None
    return Plan(tuple(parts[:MAX_SUB_QUESTIONS]), depth if depth in DEPTHS else "focused")


def resolve_depth(requested: str | None, planned: str) -> str:
    """The depth an answer runs at: the caller's `deep` or `focused` wins; `auto`
    (or none) uses the decomposer's. Always `focused` with DEEP off."""
    if not DEEP:
        return "focused"
    return requested if requested in DEPTHS else planned


@dataclass(frozen=True)
class RetrievalPart:
    """One retrieval-and-claims part. `part` is the index, in the plan, of the
    part it serves; `lens` is True when it is one of that part's lenses."""

    question: str
    part: int
    lens: bool = False


def retrieval_parts(parts: Sequence[Part], max_parts: int = MAX_PARTS) -> list[RetrievalPart]:
    """What the question will retrieve for: a factual or beyond part as itself,
    an interpretive part as its lenses (it has no retrieval of its own).

    At most `max_parts` in all. Over that, lenses are dropped from the end of
    whichever part has the most (the later one on a tie), one at a time, never below one lens each.
    Parts are never dropped. A repeated question is retrieved once.
    """
    kept = {i: list(part.lenses) for i, part in enumerate(parts) if part.kind == "interpretive"}
    plain = len(parts) - len(kept)
    while plain + sum(map(len, kept.values())) > max_parts:
        widest = max(kept, key=lambda i: (len(kept[i]), i), default=None)
        if widest is None or len(kept[widest]) <= 1:
            break
        kept[widest].pop()
    out: list[RetrievalPart] = []
    for i, part in enumerate(parts):
        for text, lens in (((lens, True) for lens in kept[i]) if i in kept
                           else [(part.question, False)]):
            if not any(r.question == text for r in out):
                out.append(RetrievalPart(text, i, lens))
    return out


async def split(
    question: str, generate: Generate, *, book_titles: Sequence[str] = ()
) -> list[str] | None:
    """1-4 sub-questions, or None when the model's output is unusable (see
    `plan`, which also carries each part's kind and lenses and the depth)."""
    planned = await plan(question, generate, book_titles=book_titles)
    return planned.questions if planned is not None else None


async def decompose(
    question: str, generate: Generate, *, book_titles: Sequence[str] = ()
) -> list[str]:
    """1-4 sub-questions, or `[question]` when the model's output is unusable
    (see `split`, which tells the two apart)."""
    return await split(question, generate, book_titles=book_titles) or [question]
