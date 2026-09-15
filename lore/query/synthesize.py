"""Turn assembled evidence into a cited answer, or refuse.

Refusing is a first-class outcome. An answer the evidence does not
support is worse than no answer, because the citation makes it look
checked.

Context size is the caller's job, not this module's. A local model with
too small a context window drops the START of an over-long prompt without
any error -- the instructions and the question vanish and the model
answers anyway. Whoever builds `generate` for synthesis must pass a
`num_ctx` measured to hold the largest real prompt plus the output budget
(see the Task 7 note in the Phase 3 plan), plus temperature 0 and seed 0.
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable

from pydantic import BaseModel

from lore.query.models import Answer, EdgeRow, Evidence

Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

REFUSAL = "The loaded books do not contain enough to answer that."

_PROMPT = """Answer the question using only the numbered passages below.

Cite every claim with the passage number in square brackets, like [1].
Use only what the passages say. If they do not contain enough to answer,
set can_answer to false rather than guessing.

Question: {question}
{relations}
Passages:
{passages}
{truncation}"""

_RELATIONS = """
Leads: relationships extracted automatically from the passages. They can be
wrong. Use them only to know where to look; a claim counts only if the
cited passage itself says it. The number in brackets is the passage each
lead came from.
{lines}
"""

# The citation forms accepted: a single number "[1]", a comma-separated
# group "[1, 3]" or "[1,3]", and adjacent groups "[1][3]" (which are just
# two single citations). Anything else in brackets -- "[1-3]", "[1; 3]",
# "[p. 2]" -- is not read as a citation at all.
_CITATION = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]")


class Synthesis(BaseModel):
    answer: str
    can_answer: bool


def cited_numbers(text: str) -> list[int]:
    """Every passage number cited in `text`, in order, in the accepted forms."""
    return [int(n) for group in _CITATION.findall(text) for n in group.split(",")]


def _title(book_id: str, book_titles: dict[str, str] | None) -> str:
    return (book_titles or {}).get(book_id) or book_id


def _relation_lines(evidence: Evidence, book_titles: dict[str, str] | None) -> list[str]:
    number = {p.chunk_id: p.n for p in evidence.passages}
    lines: list[str] = []
    for edge in evidence.edges:
        n = number.get(edge.chunk_id)
        if n is None:
            # Its passage was cut by the evidence cap: there is nothing to
            # cite it from, so showing it would invite an uncitable claim.
            continue
        line = _relation_line(edge, n, book_titles)
        if line not in lines:
            lines.append(line)
    return lines


def _relation_line(edge: EdgeRow, n: int, book_titles: dict[str, str] | None) -> str:
    # predicate_raw is the extracted verb phrase. The curated predicate
    # collapses many edges to UNCATEGORIZED and says less than the text did.
    return (f"- {edge.subject} —{edge.predicate_raw}→ {edge.object} [{n}] "
            f"({_title(edge.book_id, book_titles)}, chapter {edge.chapter_seq})")


async def synthesize(
    question: str,
    evidence: Evidence,
    generate: Generate,
    *,
    mode: str,
    template: str | None = None,
    note: str | None = None,
    book_titles: dict[str, str] | None = None,
) -> Answer:
    def answer(text: str) -> Answer:
        return Answer(text=text, passages=evidence.passages, mode=mode,
                      template=template, note=note)

    if not evidence.passages:
        return answer(REFUSAL)

    lines = _relation_lines(evidence, book_titles)
    relations = _RELATIONS.format(lines="\n".join(lines)) if lines else ""
    passages = "\n\n".join(
        f"[{p.n}] ({_title(p.book_id, book_titles)}, chapter {p.chapter_seq})\n{p.text}"
        for p in evidence.passages
    )
    truncation = (
        "\nSome evidence was truncated to fit; answer from what is shown.\n"
        if evidence.truncated else ""
    )
    result = await generate(
        _PROMPT.format(question=question, relations=relations,
                       passages=passages, truncation=truncation),
        Synthesis,
    )
    if not result.can_answer:
        return answer(REFUSAL)

    # An honoured answer must cite, and every citation must name a passage
    # the model was actually shown. An answer whose citation points nowhere
    # looks checked and is not -- the exact failure this module exists to
    # prevent -- so it is refused rather than passed on. This checks that a
    # citation EXISTS and is in range, not that the passage supports the
    # claim; that needs reading the text.
    cited = cited_numbers(result.answer)
    shown = {p.n for p in evidence.passages}
    if not cited or any(n not in shown for n in cited):
        return answer(REFUSAL)
    return answer(result.answer)
