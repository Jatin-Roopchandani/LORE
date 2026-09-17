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

_PROMPT = """You are splitting a question about books into the parts a search must cover.
{books}
Question: {question}

Return "sub_questions": a list of 1 to 4 self-contained questions that
together ask exactly what the question asks.
- If the question has one part, return the question unchanged as the only item.
- "in each novel", "in both books" or "in each book" means one
  sub-question per book, naming that book.
- A question that asks "who ... and why ..." is two sub-questions: one
  for who, one for why. Likewise "what ... and how ...".
- Each sub-question must make sense on its own: repeat the names it needs
  instead of "he", "she", "it", "they" or "that".
- Do not answer, and do not add anything the question does not ask.
"""


class SubQuestions(BaseModel):
    sub_questions: list[str]


def decomposition_prompt(question: str, book_titles: Sequence[str] = ()) -> str:
    books = ("\nBooks in the library: " + "; ".join(book_titles) + "\n"
             if book_titles else "")
    return _PROMPT.format(question=question, books=books)


async def decompose(
    question: str, generate: Generate, *, book_titles: Sequence[str] = ()
) -> list[str]:
    """1-4 sub-questions, or `[question]` when the model's output is unusable.

    Only malformed output falls back. A quota, transport or auth failure is
    raised: it would fail the answer's other calls too, and hiding it here
    would only move the error.
    """
    try:
        result = await generate(decomposition_prompt(question, book_titles), SubQuestions)
    except (ProviderOutputError, GeminiOutputError, ValidationError):
        return [question]
    if not isinstance(result, SubQuestions):
        return [question]
    parts: list[str] = []
    for part in result.sub_questions:
        text = " ".join(str(part).split())
        if text and text not in parts:
            parts.append(text)
    return parts[:MAX_SUB_QUESTIONS] or [question]
