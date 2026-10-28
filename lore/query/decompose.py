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

from typing import Awaitable, Callable, Sequence

from pydantic import BaseModel, ValidationError

from lore.inference.base import ProviderOutputError
from lore.inference.gemini import GeminiOutputError

Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

MAX_SUB_QUESTIONS = 4

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
"""


class SubQuestions(BaseModel):
    sub_questions: list[str]


def decomposition_prompt(question: str, book_titles: Sequence[str] = ()) -> str:
    # Task 10e: listed as bare context, the titles invited a split per book
    # on single-book questions (Q3). They stay, for spelling book names.
    books = ("\nBooks in the library (a reference for names only): "
             + "; ".join(book_titles) + "\n" if book_titles else "")
    return _PROMPT.format(question=question, books=books,
                          thematic=_THEMATIC_RULE if THEMATIC_PARTS else "")


async def split(
    question: str, generate: Generate, *, book_titles: Sequence[str] = ()
) -> list[str] | None:
    """1-4 sub-questions, or None when the model's output is unusable.

    Only malformed output gives None. A quota, transport or auth failure is
    raised: it would fail the answer's other calls too, and hiding it here
    would only move the error.
    """
    try:
        result = await generate(decomposition_prompt(question, book_titles), SubQuestions)
    except (ProviderOutputError, GeminiOutputError, ValidationError):
        return None
    if not isinstance(result, SubQuestions):
        return None
    parts: list[str] = []
    for part in result.sub_questions:
        text = " ".join(str(part).split())
        if text and text not in parts:
            parts.append(text)
    return parts[:MAX_SUB_QUESTIONS] or None


async def decompose(
    question: str, generate: Generate, *, book_titles: Sequence[str] = ()
) -> list[str]:
    """1-4 sub-questions, or `[question]` when the model's output is unusable
    (see `split`, which tells the two apart)."""
    return await split(question, generate, book_titles=book_titles) or [question]
