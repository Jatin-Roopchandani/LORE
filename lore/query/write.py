"""The writer: turns checked claims into a short, coherent, cited answer.

Claims are proven facts (each quote found in its passage by code). Stacked
in order they read as a list, not an answer. The writer composes a
paragraph or two from those claims only, every sentence citing the claims
it rests on. The faithfulness check then judges each sentence against its
claims (synthesize.check_answer), and `assemble` applies the fallback
rules: prose that does not pass is never shown -- the claim list is.
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable, Iterable, Sequence

from pydantic import BaseModel

from lore.query.models import Claim, Passage, ProseSentence

Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

# On by default (spec 2026-10-12). Off gives today's claim-list answers,
# byte-identical prompts and calls.
PROSE_ANSWERS = True


class Sentence(BaseModel):
    text: str
    claims: list[int]
    new_paragraph: bool = False


class Prose(BaseModel):
    sentences: list[Sentence]
    # 1-based numbers of the question's parts the facts do not answer.
    unanswered: list[int] = []

# Rendered by code, never by the model.
UNANSWERED_LEAD = "The passages found don't answer:"


WRITER_PROMPT = """Write the answer to the question from the numbered facts below.

Each fact was checked against the book it cites. Use only what the facts
say; never add anything they do not say, including what you know about the
story. Every sentence lists the numbers of the facts it uses, in `claims`.

- Never put fact numbers, chapter numbers, book titles in brackets, or any
  citation inside the sentence text. Citations go only in `claims`.
- Write as a direct reply to the question, in natural prose, not as a
  restatement of each fact. Combine related facts into one sentence where
  the facts support it.
- If the facts do not answer a part of the question, do not pretend they
  do: put that part's number in `unanswered` and write no sentence for it.
- The first sentence answers the first part the facts do answer.
- Do not state the same fact twice.
- Name a book, in plain words ("In <book title>, ..."), only when the
  facts come from more than one book.
- Keep each fact's certainty: if a fact says someone seemed, was suspected
  or claimed, say so too.
- Length: a narrow question gets two to four sentences; a broad one gets
  up to two short paragraphs, about 200 words at most. Mark the first
  sentence of a second paragraph with new_paragraph.
- Do not use connecting words that add a cause, an order or a consequence
  ("because", "so", "later", "which led to") unless a cited fact states it.

Question: {question}
Parts:
{parts}
Facts:
{facts}
"""


def _title(book_id: str, book_titles: dict[str, str] | None) -> str:
    return (book_titles or {}).get(book_id) or book_id


def part_texts(question: str, sub_questions: Sequence[str]) -> list[str]:
    """The question's parts; a question with none listed is its own part 1."""
    return list(sub_questions) or [question]


def writer_prompt(question: str, sub_questions: Sequence[str], claims: Sequence[Claim],
                  passages: Iterable[Passage], book_titles: dict[str, str] | None) -> str:
    by_number = {p.n: p for p in passages}
    lines = []
    for number, claim in enumerate(claims, start=1):
        p = by_number.get(claim.passage)
        where = f" ({_title(p.book_id, book_titles)}, chapter {p.chapter_seq})" if p else ""
        lines.append(f"{number}. {claim.text.strip()}{where}\n   quote: {claim.quote.strip()}")
    parts = "\n".join(f"{i}. {s}" for i, s in enumerate(part_texts(question, sub_questions), start=1))
    return WRITER_PROMPT.format(question=question, parts=parts, facts="\n".join(lines))


async def write_answer(question: str, sub_questions: Sequence[str], claims: Sequence[Claim],
                       passages: Iterable[Passage], generate: Generate, *,
                       book_titles: dict[str, str] | None = None) -> Prose | None:
    """The writer's prose, or None on any failure: a failed extra step must
    never cost the user the checked claims (they are the fallback)."""
    try:
        reply = await generate(writer_prompt(question, sub_questions, claims, passages, book_titles), Prose)
    except Exception:  # noqa: BLE001 -- see the docstring
        return None
    return reply if isinstance(reply, Prose) and reply.sentences else None


_REST = r"[^\s,()\[\]]*"
# a title-like lead: capitalised words, with small connectors between them
_CAP = rf"[A-Z]{_REST}"
_TITLE = rf"{_CAP}(?:\s+(?:(?:of|the|and|in|a|an|to|for|at|on)|{_CAP}))*"
_NUMS = r"\d+(?:\s*(?:,|and|&|-|\u2013)\s*\d+)*"
_RESIDUE_PAREN = re.compile(rf"\(\s*(?:{_TITLE}\s*,\s*)?(?:(?:chapters?|ch\.?)\s*)?{_NUMS}\s*\)")
_RESIDUE_BRACKET = re.compile(r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]")
_YEAR = re.compile(r"\(\s*\d{4}\s*\)")


def strip_residue(text: str) -> str:
    """Remove citation residue the writer copied into a sentence: "(Book, 1,
    2)", "(1, 2)", "(chapter 3)", "[3]". A parenthetical with ordinary words,
    or a lone four-digit year, is kept."""
    text = _RESIDUE_PAREN.sub(lambda m: m.group(0) if _YEAR.fullmatch(m.group(0)) else "", text)
    text = _RESIDUE_BRACKET.sub("", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return text.strip()


def verify_prose(prose: Prose, n_claims: int) -> list[Sentence]:
    """Sentences that cite at least one claim, all within 1..n_claims, with
    stray citation residue stripped from the text (a sentence left with no
    words is dropped)."""
    out = []
    for s in prose.sentences:
        if not (s.claims and all(1 <= k <= n_claims for k in s.claims)):
            continue
        text = strip_residue(s.text)
        if re.search(r"\w", text):
            out.append(s.model_copy(update={"text": text}))
    return out


def valid_unanswered(prose: Prose, n_parts: int) -> list[int]:
    """In-range, deduplicated part numbers, in the order given."""
    seen: list[int] = []
    for k in prose.unanswered:
        if 1 <= k <= n_parts and k not in seen:
            seen.append(k)
    return seen


def assemble(sentences: Sequence[Sentence], written: int, claim_ok: Sequence[bool],
             sentence_ok: Sequence[bool], passage_of: Sequence[int]) -> tuple[ProseSentence, ...] | None:
    """The prose to show, or None to fall back to the claim list.

    `written`: how many sentences the writer produced, before verify_prose.
    `claim_ok`/`passage_of`: per claim given to the writer (1-based order).
    `sentence_ok`: per sentence in `sentences`.
    """
    if len(sentence_ok) != len(sentences) or len(claim_ok) != len(passage_of):
        return None
    survivors_index = {}
    for k, ok in enumerate(claim_ok, start=1):
        if ok:
            survivors_index[k] = len(survivors_index) + 1
    standing = [(s, ok) for s, ok in zip(sentences, sentence_ok)
                if all(k in survivors_index for k in s.claims)]
    if sum(not ok for _, ok in standing) > 1:
        return None
    kept = [s for s, ok in standing if ok]
    if not kept or 2 * len(kept) < written:
        return None
    return tuple(
        ProseSentence(text=s.text.strip(),
                      claims=tuple(survivors_index[k] for k in s.claims),
                      passages=tuple(sorted({passage_of[k - 1] for k in s.claims})),
                      new_paragraph=s.new_paragraph and i > 0)
        for i, s in enumerate(kept))


def compose_text(prose: Sequence[ProseSentence], unanswered: Sequence[str] = ()) -> str:
    paragraphs: list[list[str]] = [[]]
    for s in prose:
        if s.new_paragraph and paragraphs[-1]:
            paragraphs.append([])
        paragraphs[-1].append(s.text + " " + "".join(f"[{n}]" for n in s.passages))
    text = "\n\n".join(" ".join(p) for p in paragraphs)
    if unanswered:
        text += f"\n\n{UNANSWERED_LEAD} " + "; ".join(unanswered)
    return text
