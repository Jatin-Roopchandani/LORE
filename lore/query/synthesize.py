"""Turn assembled evidence into quote-checked claims, or refuse.

Refusing is a first-class outcome. An answer the evidence does not
support is worse than no answer, because the citation makes it look
checked.

So the model does not write the answer. It proposes claims, each naming
the passage it comes from and a quote copied word for word from that
passage. Code keeps a claim only if the quote is really there
(`verify_claims`), and composes the answer from the kept claims alone.
This proves the quote exists where it is cited. It does not prove the
claim reads the quote correctly -- a model can still misread a real line.

Context size is the caller's job, not this module's. A local model with
too small a context window drops the START of an over-long prompt without
any error -- the instructions and the question vanish and the model
answers anyway. Whoever builds `generate` for synthesis must pass a
`num_ctx` measured to hold the largest real prompt plus the output budget
(see the Task 7 note in the Phase 3 plan), plus temperature 0 and seed 0.
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable, Iterable, Sequence

from pydantic import BaseModel

from lore.query.models import Answer, EdgeRow, Evidence, Passage

Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

REFUSAL = "The loaded books do not contain enough to answer that."

MIN_QUOTE_WORDS = 4
# Claims read per synthesis call (per part, under synthesize_parts). The
# prompt asks for every supported fact (Task 10f: Flash Lite proposed too few
# even with the answer in its evidence), so the output needs a bound. Eight
# covers a multi-fact part with room to spare while keeping a JSON reply
# short; claims past it are ignored in the order the model wrote them.
MAX_CLAIMS = 8
# A quote with an ellipsis is checked segment by segment; each segment must
# carry this many words on its own, so "... the ring ..." proves nothing.
MIN_SEGMENT_WORDS = 3

# Said when the model could not answer the whole question but some of its
# claims verified: those claims are the answer, and the user is told so.
PARTIAL_NOTE = "the passages only partly answer the question"

BAD_PASSAGE = "bad passage number"
TOO_SHORT = "quote too short"
NOT_IN_PASSAGE = "quote not in passage"

_PROMPT = """Answer the question using only the numbered passages below.

Answer as a list of claims. Each claim has:
- text: one claim, in your own words
- passage: the number of the passage the claim comes from
- quote: at least {min_words} words copied word for word from that passage

List every distinct fact in the passages that helps answer the question,
each as its own claim with its own quote,
not one claim that summarises several.
Give at most {max_claims} claims; any past that are ignored.
Prefer passages that state the answer directly
over passages that only mention its subject.

Quotes are checked by code. Any claim whose quote is not found, exactly as
written, in the passage it names is discarded. A claim must say only what
its quote says; do not combine passages into something neither one states.
Use only what the passages say; never guess. Set can_answer to false only
when no passage bears on the question, or when they answer only part of it
-- then still give the claims they do support. A claim
states something the passages say; never make a claim about what the
passages leave out.
To quote words that are not next to each other in the passage, join the
pieces with "..."; every piece must be copied word for word, in the
passage's order, and be at least {min_segment} words long.

Question: {question}
{context}{checklist}{relations}
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

# Light normalisation only: what a faithful copy may legitimately differ in.
# No fuzzy matching and no dropped punctuation -- a near-miss is a miss.
_CHARS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-"})
# Quotation marks are the one exception (Task 10e). In dialogue a copied
# line often starts or stops inside a speech, and Q5's only claim -- a
# correct one -- was dropped for leaving out one closing mark. They are
# removed from both sides before matching: every `"`, and every `'` (already
# folded from ‘ ’) that is not between two letters. An apostrophe inside a
# word ("Hope's", "don't") is spelling, not quotation, and stays.
_QUOTE_MARKS = re.compile(r"\"|(?<![^\W\d_])'|'(?![^\W\d_])")
_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"\w")
_ELLIPSIS = re.compile(r"\.\.\.|…")


class Claim(BaseModel):
    text: str
    passage: int
    quote: str


class Synthesis(BaseModel):
    claims: list[Claim]
    can_answer: bool


def _normalise(text: str) -> str:
    unquoted = _QUOTE_MARKS.sub("", text.lower().translate(_CHARS))
    return _SPACE.sub(" ", unquoted).strip()


def _find(quote: str, text: str, start: int = 0) -> re.Match | None:
    # Whole words at both ends: "olmes spoke" is not a quote of "Holmes spoke".
    pattern = r"(?<!\w)" + re.escape(quote) + r"(?!\w)"
    return re.compile(pattern).search(text, start)


def _words(text: str) -> int:
    return sum(1 for w in text.split(" ") if _WORD.search(w))


def _segments(quote: str) -> list[str]:
    """The quote split at each "..." / "…", blank ends dropped."""
    return [part.strip() for part in _ELLIPSIS.split(quote) if part.strip()]


def _segments_in(segments: list[str], text: str) -> bool:
    """Every segment found in `text`, each after the previous one ends.

    The earliest match of each segment leaves the most room for the rest,
    so this greedy scan finds an in-order placement whenever one exists.
    """
    position = 0
    for segment in segments:
        found = _find(segment, text, position)
        if found is None:
            return False
        position = found.end()
    return True


def verify_claims(
    synthesis: Synthesis, passages: Iterable[Passage]
) -> tuple[list[Claim], list[tuple[Claim, str]]]:
    """Split the model's claims into kept and dropped-with-a-reason.

    A claim is kept only if its passage number was shown, its quote has at
    least MIN_QUOTE_WORDS words (a short quote matches almost anywhere and
    proves nothing), and the quote appears in THAT passage after
    normalisation. Order is preserved; nothing is deduplicated here.

    A quote with an ellipsis ("..." or "…") is split there. Each segment
    needs MIN_SEGMENT_WORDS words, the segments together MIN_QUOTE_WORDS,
    and every segment must be found in that same passage, in order, under
    the same whole-word rule.
    """
    texts = {p.n: _normalise(p.text) for p in passages}
    kept: list[Claim] = []
    dropped: list[tuple[Claim, str]] = []
    for claim in synthesis.claims:
        segments = _segments(_normalise(claim.quote))
        if claim.passage not in texts:
            dropped.append((claim, BAD_PASSAGE))
        elif (sum(_words(s) for s in segments) < MIN_QUOTE_WORDS
              or (len(segments) > 1 and min(_words(s) for s in segments)
                  < MIN_SEGMENT_WORDS)):
            dropped.append((claim, TOO_SHORT))
        elif not _segments_in(segments, texts[claim.passage]):
            dropped.append((claim, NOT_IN_PASSAGE))
        else:
            kept.append(claim)
    return kept, dropped


def _capped(synthesis: Synthesis) -> Synthesis:
    """The first MAX_CLAIMS claims the model wrote; the rest are ignored.

    Applied before verification, so the bound is on what the model proposed
    and the choice never depends on which quotes happen to verify.
    """
    return synthesis.model_copy(update={"claims": synthesis.claims[:MAX_CLAIMS]})


def _compose(claims: list[Claim]) -> str:
    parts: list[str] = []
    for claim in claims:
        part = f"{claim.text.strip()} [{claim.passage}]"
        if part not in parts:
            parts.append(part)
    return " ".join(parts)


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


def _build_prompt(
    question: str, evidence: Evidence, book_titles: dict[str, str] | None = None,
    checklist: Sequence[str] = (), part_of: str | None = None,
) -> str:
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
    covers = (
        "Make sure the answer covers:\n" + "\n".join(f"- {c}" for c in checklist) + "\n"
        if len(checklist) > 1 else ""
    )
    context = (
        f"This is one part of the larger question: {part_of}\n"
        "Answer only this part.\n" if part_of else ""
    )
    return _PROMPT.format(min_words=MIN_QUOTE_WORDS, min_segment=MIN_SEGMENT_WORDS,
                          max_claims=MAX_CLAIMS,
                          question=question, context=context, checklist=covers,
                          relations=relations, passages=passages,
                          truncation=truncation)


async def synthesize(
    question: str,
    evidence: Evidence,
    generate: Generate,
    *,
    mode: str,
    template: str | None = None,
    note: str | None = None,
    book_titles: dict[str, str] | None = None,
    checklist: Sequence[str] = (),
) -> Answer:
    """`checklist`: the question's sub-questions; shown only when there are
    several, so a partial answer is visible as partial."""
    def answer(text: str, extra_note: str | None = None) -> Answer:
        notes = "; ".join(n for n in (note, extra_note) if n) or None
        return Answer(text=text, passages=evidence.passages, mode=mode,
                      template=template, note=notes)

    if not evidence.passages:
        return answer(REFUSAL)

    result = await generate(
        _build_prompt(question, evidence, book_titles, checklist), Synthesis)

    # The answer is written by code, from claims whose quotes were found in
    # the passage they cite. Nothing the model wrote outside a kept claim
    # reaches the user, so there is no free text to carry a stray claim.
    result = _capped(result)
    kept, _ = verify_claims(result, evidence.passages)
    if not kept:
        return answer(REFUSAL)
    # can_answer=false with verified claims is a partial answer, not a
    # refusal: Task 10a measured H6 and H4 discarding verified gold claims.
    return answer(_compose(kept), None if result.can_answer else PARTIAL_NOTE)


NOT_FOUND = "not found: "


async def synthesize_parts(
    question: str,
    sub_questions: Sequence[str],
    evidence: Evidence,
    generate: Generate,
    *,
    mode: str,
    template: str | None = None,
    note: str | None = None,
    book_titles: dict[str, str] | None = None,
) -> Answer:
    """One synthesis call per sub-question, over that part's share, merged.

    Task 10d's single call over ~24 passages for a multi-part question was
    too diffuse: Q2 had every fact in its evidence and cited one. So each
    part gets its own call over `evidence.shares[i]` -- the passages the
    assembly gave it, plus those it ranked but another part took -- with the
    original question as context. Numbering stays global: a part is shown
    its passages under the numbers the whole answer uses, and verifies
    claims against only those, so every `[n]` resolves to the one passage
    list the CLI prints.

    Kept claims are composed in sub-question order, exact duplicates once.
    A part with no kept claim (or no passages, which is not asked) is named
    in the partial note; only when no part keeps a claim is it a refusal.
    `can_answer` does not decide partiality here: whether a part kept a
    verified claim does.

    With one sub-question, or evidence that carries no shares, this is
    `synthesize` exactly: one call over everything.
    """
    if len(sub_questions) < 2 or len(evidence.shares) != len(sub_questions):
        return await synthesize(question, evidence, generate, mode=mode, template=template,
                                note=note, book_titles=book_titles,
                                checklist=sub_questions)

    def answer(text: str, extra_note: str | None = None) -> Answer:
        notes = "; ".join(n for n in (note, extra_note) if n) or None
        return Answer(text=text, passages=evidence.passages, mode=mode,
                      template=template, note=notes)

    if not evidence.passages:
        return answer(REFUSAL)

    kept: list[Claim] = []
    missing: list[str] = []
    for part, share in zip(sub_questions, evidence.shares):
        numbers = set(share)
        passages = tuple(p for p in evidence.passages if p.n in numbers)
        if not passages:
            missing.append(part)
            continue
        own = Evidence(edges=evidence.edges, passages=passages,
                       truncated=evidence.truncated)
        result = await generate(
            _build_prompt(part, own, book_titles, part_of=question), Synthesis)
        verified, _ = verify_claims(_capped(result), passages)
        if not verified:
            missing.append(part)
        kept.extend(c for c in verified if c not in kept)

    if not kept:
        return answer(REFUSAL)
    partial = (PARTIAL_NOTE + "; " + "; ".join(NOT_FOUND + m for m in missing)
               if missing else None)
    return answer(_compose(kept), partial)
