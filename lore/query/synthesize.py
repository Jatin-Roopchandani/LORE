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

That second proof is the faithfulness check (`check_claims`, Task 12): one
batched call judges every surviving claim against the passage it cites, and
a claim the passage does not state is dropped. It only ever removes claims,
and only when it ran and its answer lined up; anything else keeps them all
and says so.

Context size is the caller's job, not this module's. A local model with
too small a context window drops the START of an over-long prompt without
any error -- the instructions and the question vanish and the model
answers anyway. Whoever builds `generate` for synthesis must pass a
`num_ctx` measured to hold the largest real prompt plus the output budget
(see the Task 7 note in the Phase 3 plan), plus temperature 0 and seed 0.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable, Sequence

from pydantic import BaseModel

from lore.query import judge
from lore.query import write as write_module
from lore.query.models import Answer, BookInfo, Claim, EdgeRow, Evidence, Passage, ProseSentence
from lore.query.position import order_passages
from lore.query.write import (
    assemble, book_infos, clean_gaps, compose_text, coverage_gaps, paragraphs_by_book, part_texts, split_items,
    valid_unanswered, verify_prose, write_answer,
)

if TYPE_CHECKING:  # write.py never imports this module, so no cycle at runtime
    from lore.query.write import Sentence

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

{facts_rule}Give at most {max_claims} claims; any past that are ignored.
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

# How much to say. Traced on the dev set (evals/answer_quality, trace_report.py),
# Flash Lite proposed 1.8 claims per call and code dropped almost none: the
# model gives the shortest reply to each part and stops, leaving the
# circumstances, reasons and consequences its passages hold unsaid.
# FULL_ANSWERS asks for those. Adopted 2026-10-12 together with NO_RESTATE:
# paired rounds 4 and 5 (run_pair.py, blind-graded) raised key-fact coverage
# on both sets in both rounds (dev +11% / +9%, held-out +11% / +15%); no
# single set reached p < 0.05, but pooled the four came out better on 40
# questions and worse on 22 (sign test p = 0.03). See GRADING.md.
FULL_ANSWERS = True
_FACTS_RULE = """List every distinct fact in the passages that helps answer the question,
each as its own claim with its own quote,
not one claim that summarises several.
"""
_FULL_FACTS_RULE = """A good answer is complete, not just the shortest reply. Give every fact in
the passages that a reader asking this would want: the direct answer, then
the circumstances, reasons, methods and consequences the passages give for
it -- who, what, how and why. Read every passage, not only the first one
that answers. Each fact is its own claim with its own quote,
not one claim that summarises several.
"""
# NO_RESTATE: round 4 found FULL_ANSWERS roughly doubled restated claims
# (graded duplicates 15 -> 29 on the dev set) -- asked for circumstances and
# reasons, the model says one fact twice from two passages. With it (round
# 5) held-out duplicates stayed flat (7 -> 7) and dev rose less (9 -> 16).
NO_RESTATE = True
_NO_RESTATE_RULE = """Every claim must add a fact the earlier claims do not already state. Do not
say the same thing twice, even in other words or from a different passage;
if two passages state one fact, make one claim and quote the clearer one.
"""
# ATTRIBUTE_SPEECH (2026-10-28): a claim drawn from a character's own speech
# or first-person story was written as plain fact -- "the killing was the
# result of Tonga's instincts" from Small's confession. The claim now names
# whose account it is. Off gives the earlier prompt byte for byte.
ATTRIBUTE_SPEECH = True
_ATTRIBUTE_SPEECH_RULE = """When the quote is a character speaking, or telling their own story in the
first person, the claim is that character's account: say whose it is
("<name> says ...", "according to <name> ..."), never state it as plain fact.
"""
# STRICT_LINKS (2026-10-28): FULL_ANSWERS asks for reasons, and a claim joined
# two facts from one passage with a cause it never gave -- "Hope targeted
# Drebber because they were rivals in love" from a passage saying only that
# they were rivals. The claims rule and the check now both name the
# connecting words. Off gives the earlier prompts byte for byte.
STRICT_LINKS = True
_STRICT_LINKS_RULE = """Give a reason, motive or consequence only when the quote itself states it.
Two facts from one passage stay two claims: never join them with "because",
"so", "since", "in order to" or "which led to" unless the passage says so.
"""
_STRICT_LINKS_CHECK = """Connecting words count: a "because", "so", "since", "in order to" or
"which led to" in a claim that the cited passage does not state makes the
claim "{not_supported}", even when both facts it joins are true.
"""
# ROLE_COVERAGE (2026-10-28): asked about a kind of character (a role such as
# opponents, allies or victims), the claims named only the most prominent one
# although the passages showed others in that role. General by design: no
# genre, book or role is named. Off gives the earlier prompt byte for byte.
ROLE_COVERAGE = True
_ROLE_COVERAGE_RULE = """If the question asks about a kind of character or a role rather than a named
person, make claims for every character the passages show in that role,
not only the most prominent one, including anyone whose earlier actions
set the events in motion.
"""
_PART_CONTEXT = "This is one part of the larger question: {part_of}\nAnswer only this part.\n"
_FULL_PART_CONTEXT = ("This is one part of the larger question: {part_of}\n"
                      "Answer this part fully; the other parts are answered separately.\n")

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


# Near-duplicate claims (Task 11b). Per-part synthesis lets two sub-questions
# that saw one passage restate the same fact in slightly different words.
# Word overlap of the claim texts is Jaccard over their content words: lower
# case, letters and digits only, minus the function words below and single
# letters (the "s" of "Holmes's"). A number is kept at any length.
NEAR_DUPLICATE_OVERLAP = 0.6
_TOKEN = re.compile(r"[^\W_]+")
_FUNCTION_WORDS = frozenset("""
a an the and or but nor of to in on at by for from with as into onto upon about
is are was were be been being am has have had having do does did this that these
those it its he him his she her they them their i me my we us our you your
not no so than then there here which who whom whose what when where why how
""".split())


def _content_words(text: str) -> frozenset[str]:
    return frozenset(w for w in _TOKEN.findall(text.lower())
                     if (len(w) > 1 or w.isdigit()) and w not in _FUNCTION_WORDS)


def _overlap(a: str, b: str) -> float:
    left, right = _content_words(a), _content_words(b)
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _numbers(text: str) -> frozenset[str]:
    return frozenset(w for w in _TOKEN.findall(text) if w.isdigit())


def _runs(quote: str, size: int) -> set[tuple[str, ...]]:
    """Every run of `size` consecutive words inside one ellipsis segment."""
    runs: set[tuple[str, ...]] = set()
    for segment in _segments(_normalise(quote)):
        words = _TOKEN.findall(segment)
        runs.update(tuple(words[i:i + size]) for i in range(len(words) - size + 1))
    return runs


def _same_evidence(a: Claim, b: Claim) -> bool:
    """Same passage, and quotes that are identical or share a run of
    MIN_QUOTE_WORDS words -- which a quote contained in the other does."""
    if a.passage != b.passage:
        return False
    if _normalise(a.quote) == _normalise(b.quote):
        return True
    return bool(_runs(a.quote, MIN_QUOTE_WORDS) & _runs(b.quote, MIN_QUOTE_WORDS))


def _near_duplicate(a: Claim, b: Claim) -> bool:
    """Both conditions, so distinct facts drawn from one quote survive: the
    same evidence is not enough without claim texts that say the same. Claims
    that state different numbers are different facts however alike the rest."""
    return (_same_evidence(a, b)
            and _numbers(a.text) == _numbers(b.text)
            and _overlap(a.text, b.text) >= NEAR_DUPLICATE_OVERLAP)


def _sentence(claim: Claim) -> str:
    return f"{claim.text.strip()} [{claim.passage}]"


def _collapse(claims: list[Claim]) -> list[Claim]:
    """Kept claims in order; an exact or near duplicate of an earlier kept
    claim is left out, so the first wording wins.

    Split out from `_compose` in Task 12: the faithfulness check runs over
    the claims that reach the answer, so it must see this list and not the
    duplicates it removed.
    """
    shown: list[Claim] = []
    for claim in claims:
        if any(_sentence(earlier) == _sentence(claim) or _near_duplicate(earlier, claim)
               for earlier in shown):
            continue
        shown.append(claim)
    return shown


def _compose(claims: list[Claim]) -> str:
    return " ".join(_sentence(claim) for claim in claims)


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


# STORY_ORDER, claims step (2026-10-28): a question about change got claims
# only from the latest passages -- the step read its passages in rank order
# with no positions, so it had no early and late to draw from. With
# write.STORY_ORDER it reads them in story order, labelled, under this rule.
_CHANGE_RULE = """If the question asks how something changed, developed or progressed, give
claims that show the state at different points in the story, from the
earliest passages to the latest, including what characters show or do and
not only what is said outright.
"""


# READINGS, claims step: a reading rests on facts about what characters do, so
# the claims must include them, not only what is said outright.
_ACTIONS_RULE = """Include claims about what characters do, choose and how they behave toward
each other, not only what they say or what the narrator states.
"""


# JUDGEMENTS, claims step (lore/query/judge.py): a question that asks for a
# judgement is answered by comparing people, and a comparison needs what each
# of them does, wants, conceals and changes, not only how they are described.
_JUDGEMENT_RULE = """This question asks for a judgement about people in the story. For each person
the passages show who bears on the quality asked about, give claims about what
they do, want, conceal and change, and the contradictions between these, not
only how they are described. Cover more than one such person when the passages
allow it, so that they can be compared.
"""


# TIMELINE (lore/query/timeline.py): beats are short notes on moments from
# across the whole story for the question's entities. They are a map of where
# the passages are, never evidence: a claim still quotes a numbered passage.
_TIMELINE_BLOCK = """
Story timeline: short notes on moments from across the whole story, for the
people and things the question is about, in story order. Each line gives the
number of the passage that holds the moment, where it falls in the story and
who it is about. The timeline is a map, not evidence. Use it to see the whole
span of the story, from the earliest moment to the latest and what lies
between, then read those numbered passages. A claim still needs a quote copied
word for word from a numbered passage; never quote a timeline line, and never
make a claim from a timeline line alone.
{lines}
"""


def _timeline_block(evidence: Evidence, book_titles: dict[str, str] | None,
                    books: dict[str, BookInfo] | None) -> str:
    """The "Story timeline" block, or "" when no beat's passage is shown. A
    beat whose passage the evidence cap cut is left out: it could not be cited."""
    if not evidence.timeline:
        return ""
    order = order_passages(evidence.passages, write_module.book_infos(book_titles, books))
    place = {pos.n: (index, pos) for index, pos in enumerate(order.ordered)}
    number = {p.chunk_id: p.n for p in evidence.passages}
    shown = [(place[number[b.chunk_id]], b) for b in evidence.timeline if b.chunk_id in number]
    shown.sort(key=lambda item: item[0][0])  # stable: a passage's beats stay in order
    lines = [f"- [{pos.n}] ({pos.label(detail=True)}) {b.subject}: {b.text}"
             for (_, pos), b in shown]
    return _TIMELINE_BLOCK.format(lines="\n".join(lines)) if lines else ""


def _passage_block(evidence: Evidence, book_titles: dict[str, str] | None,
                   books: dict[str, BookInfo] | None) -> str:
    if not write_module.STORY_ORDER:
        return "\n\n".join(
            f"[{p.n}] ({_title(p.book_id, book_titles)}, chapter {p.chapter_seq})\n{p.text}"
            for p in evidence.passages)
    order = order_passages(evidence.passages, write_module.book_infos(book_titles, books))
    by_n = {p.n: p for p in evidence.passages}
    block = "\n\n".join(f"[{pos.n}] ({pos.label(detail=True)})\n{by_n[pos.n].text}"
                         for pos in order.ordered)
    note = order.note()
    return f"Passages are in story order.{' ' + note if note else ''}\n\n{block}"


def _build_prompt(
    question: str, evidence: Evidence, book_titles: dict[str, str] | None = None,
    checklist: Sequence[str] = (), part_of: str | None = None,
    books: dict[str, BookInfo] | None = None,
) -> str:
    lines = _relation_lines(evidence, book_titles)
    relations = _RELATIONS.format(lines="\n".join(lines)) if lines else ""
    passages = _passage_block(evidence, book_titles, books)
    truncation = (
        "\nSome evidence was truncated to fit; answer from what is shown.\n"
        if evidence.truncated else ""
    )
    covers = (
        "Make sure the answer covers:\n" + "\n".join(f"- {c}" for c in checklist) + "\n"
        if len(checklist) > 1 else ""
    )
    context = (
        (_FULL_PART_CONTEXT if FULL_ANSWERS else _PART_CONTEXT).format(part_of=part_of)
        if part_of else ""
    )
    return _PROMPT.format(min_words=MIN_QUOTE_WORDS, min_segment=MIN_SEGMENT_WORDS,
                          facts_rule=(_FULL_FACTS_RULE if FULL_ANSWERS else _FACTS_RULE)
                          + (_NO_RESTATE_RULE if NO_RESTATE else "")
                          + (_ATTRIBUTE_SPEECH_RULE if ATTRIBUTE_SPEECH else "")
                          + (_STRICT_LINKS_RULE if STRICT_LINKS else "")
                          + (_ROLE_COVERAGE_RULE if ROLE_COVERAGE else "")
                          + (_CHANGE_RULE if write_module.STORY_ORDER else "")
                          + (_ACTIONS_RULE if write_module.readings_on() else "")
                          + (_JUDGEMENT_RULE if judge.JUDGEMENTS and judge.is_evaluative(question)
                             else ""),
                          max_claims=MAX_CLAIMS,
                          question=question, context=context, checklist=covers,
                          relations=relations + _timeline_block(evidence, book_titles, books),
                          passages=passages,
                          truncation=truncation)


# --- Task 12: the faithfulness check ---------------------------------------
#
# The quote check proves a quote is in the passage it cites. It cannot see
# that the claim says MORE than the quote does -- a cause, a motive, a
# consequence -- or that the claim names a book the passage is not from.
# Three such false claims appeared in the Task 11 regression (Q5's wrong
# novel, C7's invented reason, C10's two scenes merged into one).
#
# So every claim that survives the quote check AND the near-duplicate
# collapse is judged, once, against the one passage it cites, in a single
# batched call per answer. `not_supported` drops the claim.
#
# The failure direction is deliberate and one-sided: dropping a TRUE claim
# is worse than the disease, so anything that goes wrong here -- the call
# raising, an answer that does not line up, a verdict that is neither word
# -- keeps every claim and says so in the answer's note. The check never
# silently removes anything.

SUPPORTED = "supported"
NOT_SUPPORTED = "not_supported"
# Asked for, not enforced: the reason is shown to a human, never parsed.
MAX_REASON_WORDS = 12
CHECK_SKIPPED_NOTE = "the faithfulness check did not run"


class Judgement(BaseModel):
    claim: int
    verdict: str
    reason: str = ""


class _PlainFaithfulness(BaseModel):
    """The check's reply as it was before STORY_ORDER (and still is when it
    is off): the schema asked of the model is part of the request."""

    judgements: list[Judgement]
    sentence_judgements: list[Judgement] = []


class Faithfulness(_PlainFaithfulness):
    gap_judgements: list[Judgement] = []


_CHECK_PROMPT = """Check each numbered claim against the one passage it cites.

Passages:
{passages}

Claims:
{claims}

For each claim, decide whether the passage it cites states that claim.
- "{supported}": the cited passage says what the claim says.
- "{not_supported}": it does not, or it says less than the claim says.

A claim that names a book, a person or a place the cited passage does not
name is "{not_supported}". A claim that adds a cause, a motive or a
consequence the cited passage does not state is "{not_supported}". Judge
each claim against its own cited passage alone: what another passage says,
or what you already know about the story, cannot make a claim supported.
{strict_links}
Return one entry per claim, in the same order, each with its claim number:
- verdict: "{supported}" or "{not_supported}"
- reason: at most {max_words} words.

Answer every claim: give exactly {count} entries, numbered 1 to {count}, in order.
"""

_SENTENCES_BLOCK = """
Sentences:
{sentences}

Then check each numbered sentence of the written answer against the claims
it uses (their text and quotes above). "{supported}": those claims,
together, state everything the sentence says. "{not_supported}": the
sentence says more than they do. Connecting words count: a "because",
"so", "later" or "which led to" that no cited claim states makes the
sentence "{not_supported}". Return these in sentence_judgements, one per
sentence, numbered 1 to {count} in the claim field, with a verdict and a
reason of at most {max_words} words.
"""

# STORY_ORDER (write.STORY_ORDER): the sentence rule judges meaning, not
# wording, and accepts an order word when the cited claims' positions show
# that order. What it still refuses is what adds something: a fact, a
# cause, an order the positions do not show, or a summary that overstates.
_STORY_SENTENCES_BLOCK = """
Sentences:
{sentences}

Then check each numbered sentence of the written answer against the claims
it uses (their text, quotes and positions above). Judge meaning, not
wording.
"{supported}": those claims, together, support what the sentence means.
- Paraphrase, summary and combining claims are supported when they add
  nothing.
- An order word ("at first", "earlier", "later", "by the end", "in the next
  book") is supported when the positions of the cited claims show that
  order.
- Contrast and addition words ("also", "however", "although", "in
  contrast") never make a sentence "{not_supported}".
"{not_supported}": the sentence
- adds a fact, name, number or detail the cited claims do not state;
- states a cause or consequence ("because", "so", "which led to", "as a
  result") that no cited claim states;
- uses an order word that the positions contradict, or that links claims
  from books whose relative order is unknown;
- or summarises with more than the claims show, such as "always" or
  "every" when they show one case or some.
Return these in sentence_judgements, one per sentence, numbered 1 to
{count} in the claim field, with a verdict and a reason of at most
{max_words} words.
"""

# READINGS: a sentence marked "(reading)" interprets its claims instead of
# restating them, so it gets this test in place of the one above.
_READINGS_BLOCK = """
A sentence marked "(reading)" interprets what its cited claims show
together; it is not a direct statement of them. Judge a reading by this test
instead of the one above, and give its verdict in the same
sentence_judgements entry.
"{supported}": a careful reader would agree that the cited claims, together,
fairly support this reading; it is worded visibly as a reading ("suggests",
"seems", "appears"); and it adds no event, name, number or detail beyond
the claims.
"{not_supported}": it overreaches beyond the claims, is stated as plain
fact, or adds detail.
"""

# JUDGEMENTS: a sentence marked "(judgement)" weighs the people the claims
# cover and names the strongest case. It is not a fact and not an
# interpretation of one thing; it gets this test.
_JUDGEMENTS_BLOCK = """
A sentence marked "(judgement)" compares the people its cited claims cover and
names the strongest case among them. Judge it by this test instead of the one
above, and give its verdict in the same sentence_judgements entry.
"{supported}": a careful reader would agree that the cited claims, together,
make it a fair, arguable case; it is worded visibly as a judgement ("a strong
case can be made", "arguably", "of these ... stands out"), not as plain fact
and not as the book's own view; and it adds no event, name, number or detail
beyond the claims.
"{not_supported}": the claims do not make that a fair case, it is stated as
plain fact, it says the book itself describes someone this way when no claim
does, or it adds detail.
"""

# Gaps are statements that the facts leave something out; code shows them
# only if the check agrees. "supported" means the statement is true.
_GAPS_BLOCK = """
Gaps:
{gaps}

Each gap says the claims above do not cover something the question asks
about. Judge each against all of the claims above.
"{supported}": the claims say nothing that answers the gap, so it is a true
gap.
"{not_supported}": some claim, or the claims together, do answer it.
Return these in gap_judgements, one per gap, numbered 1 to {count} in the
claim field, with a verdict and a reason of at most {max_words} words.
"""


def faithfulness_prompt(
    claims: Sequence[Claim],
    passages: Iterable[Passage],
    book_titles: dict[str, str] | None = None,
    sentences: Sequence["Sentence"] | None = None,
    books: dict[str, BookInfo] | None = None,
    gaps: Sequence[str] | None = None,
) -> str:
    """The one batched prompt: the cited passages, then the numbered claims.

    PASSAGE BUDGET: every passage some claim cites, once, in full -- and no
    other. Deduplicating matters (several claims usually cite one passage);
    trimming does not. The cited set is a subset of the evidence, which the
    caller already cut to its budget (`evidence_budget`: 36,000 characters
    on Gemini, 12,000 on Ollama), so this prompt's passages can never exceed
    that. Truncating a passage HERE would be actively harmful: it could cut
    a verified quote out of the text it was verified against and turn a true
    claim into `not_supported`, which is the one failure this task says is
    worse than the disease.
    """
    by_number = {p.n: p for p in passages}
    cited = [by_number[n] for n in sorted({c.passage for c in claims})
             if n in by_number]
    story = write_module.STORY_ORDER
    order = order_passages(cited, book_infos(book_titles, books)) if story else None

    def head(p: Passage) -> str:
        return (order.positions[p.n].label(detail=True) if order
                else f"{_title(p.book_id, book_titles)}, chapter {p.chapter_seq}")

    shown = "\n\n".join(f"[{p.n}] ({head(p)})\n{p.text}" for p in cited)
    items = []
    for number, claim in enumerate(claims, start=1):
        passage = by_number.get(claim.passage)
        where = f"[{claim.passage}] ({head(passage)})" if passage else f"[{claim.passage}]"
        items.append(f"{number}. claim: {claim.text.strip()}\n"
                     f"   cites passage {where}\n"
                     f"   quote: {claim.quote.strip()}")
    prompt = _CHECK_PROMPT.format(
        passages=shown, claims="\n\n".join(items), count=len(claims),
        supported=SUPPORTED, not_supported=NOT_SUPPORTED, max_words=MAX_REASON_WORDS,
        strict_links=(_STRICT_LINKS_CHECK.format(not_supported=NOT_SUPPORTED)
                      if STRICT_LINKS else ""))
    if order and order.note():
        prompt += "\n" + order.note() + "\n"
    if sentences:
        # Kinds are shown, and a reading's or judgement's own test given, only
        # when one is among the sentences: an answer of facts alone is checked
        # as before.
        kinds = {getattr(s, "kind", "fact") for s in sentences}
        readings = write_module.readings_on() and "reading" in kinds
        judgements = write_module.readings_on() and judge.JUDGEMENTS and "judgement" in kinds
        labelled = readings or judgements

        def label(s) -> str:
            return f" ({getattr(s, 'kind', 'fact')})" if labelled else ""

        lines = "\n".join(
            f"{i}. sentence{label(s)}: {s.text.strip()}\n"
            f"   uses claims {', '.join(str(k) for k in s.claims)}"
            for i, s in enumerate(sentences, start=1))
        prompt += (_STORY_SENTENCES_BLOCK if story else _SENTENCES_BLOCK).format(
            sentences=lines, count=len(sentences), supported=SUPPORTED,
            not_supported=NOT_SUPPORTED, max_words=MAX_REASON_WORDS)
        if readings:
            prompt += _READINGS_BLOCK.format(supported=SUPPORTED, not_supported=NOT_SUPPORTED)
        if judgements:
            prompt += _JUDGEMENTS_BLOCK.format(supported=SUPPORTED, not_supported=NOT_SUPPORTED)
        if story and gaps:
            prompt += _GAPS_BLOCK.format(
                gaps="\n".join(f"{i}. gap: {g}" for i, g in enumerate(gaps, start=1)),
                count=len(gaps), supported=SUPPORTED, not_supported=NOT_SUPPORTED,
                max_words=MAX_REASON_WORDS)
    return prompt


def validated_verdicts(
    result: Faithfulness, expected: int
) -> list[tuple[bool, str]] | None:
    """One (supported, reason) per claim asked, or None if it does not align.

    The mapping must be verifiable, as in the disambiguator and in
    `validated_speakers`: exactly `expected` entries numbered 1..expected in
    order. A count check alone would accept a rotated answer set, which is
    exactly how a numbered batch attaches the wrong verdict to the wrong
    claim -- and here that would DELETE a true claim.

    A verdict that is neither word rejects the whole answer rather than that
    one entry: an answer the model wrote in some other shape is not evidence
    about any of its entries, and keeping every claim is the safe direction.
    """
    items = getattr(result, "judgements", None)
    if not isinstance(items, list):
        return None
    if [getattr(item, "claim", None) for item in items] != list(range(1, expected + 1)):
        return None
    verdicts: list[tuple[bool, str]] = []
    for item in items:
        word = _SPACE.sub("_", str(item.verdict).strip().lower().strip(".")).strip("_")
        if word not in (SUPPORTED, NOT_SUPPORTED):
            return None
        verdicts.append((word == SUPPORTED, " ".join(str(item.reason).split())))
    return verdicts


def validated_sentence_verdicts(result, expected: int,
                                field: str = "sentence_judgements") -> list[bool] | None:
    """One supported-flag per sentence, or None if the answer does not line
    up -- the same verifiable-mapping rule as `validated_verdicts`."""
    items = getattr(result, field, None)
    if not isinstance(items, list) or not items:
        return None
    if [getattr(item, "claim", None) for item in items] != list(range(1, expected + 1)):
        return None
    flags: list[bool] = []
    for item in items:
        word = _SPACE.sub("_", str(item.verdict).strip().lower().strip(".")).strip("_")
        if word not in (SUPPORTED, NOT_SUPPORTED):
            return None
        flags.append(word == SUPPORTED)
    return flags


def validated_gap_verdicts(result, expected: int) -> list[bool] | None:
    """One flag per gap (True: the gap is real), under the same rule."""
    return validated_sentence_verdicts(result, expected, "gap_judgements")


@dataclass
class CheckOutcome:
    kept: list[Claim]
    dropped: list[tuple[Claim, str]]
    ran: bool
    claim_ok: list[bool]
    sentence_ok: list[bool] | None
    # Per gap asked: True when the check agrees the facts leave it open.
    # None when no gaps were asked or the verdicts did not line up.
    gap_ok: list[bool] | None = None


async def check_answer(
    claims: Sequence[Claim],
    passages: Iterable[Passage],
    check: Generate,
    *,
    book_titles: dict[str, str] | None = None,
    sentences: Sequence["Sentence"] | None = None,
    books: dict[str, BookInfo] | None = None,
    gaps: Sequence[str] | None = None,
) -> CheckOutcome:
    """`check_claims`, plus one verdict per prose sentence in the same call.

    Claim and sentence verdicts are validated separately: garbled sentence
    verdicts cost only the prose (`sentence_ok=None`), never the claim check.
    Gap verdicts likewise: garbled ones cost only the gaps (`gap_ok=None`).
    """
    claims = list(claims)
    if not claims:
        return CheckOutcome([], [], True, [], None)
    passages = list(passages)
    gap_ok = None
    try:
        result = await check(
            faithfulness_prompt(claims, passages, book_titles, sentences, books, gaps),
            Faithfulness if write_module.STORY_ORDER else _PlainFaithfulness)
        verdicts = validated_verdicts(result, len(claims))
        sentence_ok = (validated_sentence_verdicts(result, len(sentences))
                       if sentences and verdicts is not None else None)
        if gaps and sentences and verdicts is not None and write_module.STORY_ORDER:
            gap_ok = validated_gap_verdicts(result, len(gaps))
    except Exception:  # noqa: BLE001 -- see check_claims
        verdicts, sentence_ok = None, None
    if verdicts is None:
        return CheckOutcome(claims, [], False, [True] * len(claims), None)
    kept: list[Claim] = []
    dropped: list[tuple[Claim, str]] = []
    for claim, (supported, reason) in zip(claims, verdicts):
        (kept.append(claim) if supported else dropped.append((claim, reason)))
    return CheckOutcome(kept, dropped, True, [ok for ok, _ in verdicts], sentence_ok, gap_ok)


async def check_claims(
    claims: Sequence[Claim],
    passages: Iterable[Passage],
    check: Generate,
    *,
    book_titles: dict[str, str] | None = None,
) -> tuple[list[Claim], list[tuple[Claim, str]], bool]:
    """(kept, dropped with the model's reason, whether the check ran).

    One call for every claim. `ran=False` means nothing was judged and every
    claim was kept; the caller must say so rather than pass it off as a
    checked answer.

    Every exception is caught, deliberately. This pass runs after the answer
    is already written and quote-checked; a quota, transport or schema
    failure in an extra safety check must not destroy an answer that is
    correct without it, and must never be read as `not_supported`.
    """
    outcome = await check_answer(claims, passages, check, book_titles=book_titles)
    return outcome.kept, outcome.dropped, outcome.ran


PROSE_FALLBACK_NOTE = "the written answer did not pass verification; showing the checked facts"


async def _finish(question, sub_questions, kept, evidence, check, write, book_titles,
                  part_claims=None, books=None):
    """Writer (when it applies) and the faithfulness check, in one place for
    both synthesize paths. Returns (kept, cut, prose, skipped_note, prose_note,
    unanswered part texts, gaps -- both set only when the prose is shown)."""
    try_prose = (write_module.PROSE_ANSWERS and write is not None and check is not None
                 and len(kept) >= 2)
    sentences, written, prose_note, missing, gaps = None, 0, None, [], []
    if try_prose:
        drafted = await write_answer(question, sub_questions, kept, evidence.passages, write,
                                     book_titles=book_titles, books=books)
        if drafted is None:
            prose_note = PROSE_FALLBACK_NOTE
        else:
            texts = [c.text for c in kept]
            sentences, written = verify_prose(drafted, len(kept), texts), len(split_items(drafted))
            parts = part_texts(question, sub_questions)
            missing = [parts[k - 1] for k in valid_unanswered(drafted, len(parts))]
            if write_module.STORY_ORDER:
                gaps = [g for g in clean_gaps(drafted.gaps)
                        if g.casefold() not in {m.casefold() for m in missing}]
    if check is None or not kept:
        return kept, [], (), None, prose_note, (), ()
    passage_of = [c.passage for c in kept]
    outcome = await check_answer(kept, evidence.passages, check, book_titles=book_titles,
                                 sentences=sentences or None, books=books,
                                 gaps=gaps if sentences else None)
    skipped = None if outcome.ran else CHECK_SKIPPED_NOTE
    prose: tuple[ProseSentence, ...] = ()
    if sentences is not None:
        built = (assemble(sentences, written, outcome.claim_ok, outcome.sentence_ok, passage_of)
                 if sentences and outcome.ran and outcome.sentence_ok is not None else None)
        if built and part_claims is not None:
            numbered = [[outcome.kept.index(c) + 1 for c in cs if c in outcome.kept]
                        for cs in part_claims]
            if coverage_gaps(built, numbered):
                built = None  # one-sided prose: the claim list covers every part
        if built:
            prose = paragraphs_by_book(built, {p.n: p.book_id for p in evidence.passages})
        else:
            prose_note = PROSE_FALLBACK_NOTE
    # A gap is shown only if the check ran over it and agreed: an unchecked
    # "the passages don't say" is a claim like any other.
    shown_gaps = (tuple(g for g, ok in zip(gaps, outcome.gap_ok) if ok)
                  if prose and outcome.gap_ok is not None and len(outcome.gap_ok) == len(gaps) else ())
    return (outcome.kept, [c for c, _ in outcome.dropped], prose, skipped, prose_note,
            tuple(missing) if prose else (), shown_gaps)


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
    check: Generate | None = None,
    write: Generate | None = None,
    books: dict[str, BookInfo] | None = None,
) -> Answer:
    """`checklist`: the question's sub-questions; shown only when there are
    several, so a partial answer is visible as partial.

    `check`: the faithfulness judge (`check_claims`). None means the pass is
    not part of this answer at all, which is what every caller that predates
    Task 12 gets.
    """
    def answer(text: str, *extra_notes: str | None,
              claims: tuple[Claim, ...] = ()) -> Answer:
        notes = "; ".join(n for n in (note, *extra_notes) if n) or None
        return Answer(text=text, passages=evidence.passages, mode=mode,
                      template=template, note=notes, claims=claims)

    if not evidence.passages:
        return answer(REFUSAL)

    result = await generate(
        _build_prompt(question, evidence, book_titles, checklist, books=books), Synthesis)

    # The answer is written by code, from claims whose quotes were found in
    # the passage they cite. Nothing the model wrote outside a kept claim
    # reaches the user, so there is no free text to carry a stray claim.
    result = _capped(result)
    verified, _ = verify_claims(result, evidence.passages)
    kept = _collapse(verified)
    kept, _cut, prose, skipped, prose_note, unanswered, gaps = await _finish(
        question, list(checklist) or [question], kept, evidence, check, write, book_titles,
        books=books)
    if not kept:
        return answer(REFUSAL)
    # can_answer=false with verified claims is a partial answer, not a
    # refusal: Task 10a measured H6 and H4 discarding verified gold claims.
    # `claims` carries the SAME post-check list `_compose` just flattened --
    # survivors only, so a claim's presence in Answer is its verdict.
    partial = None if result.can_answer else PARTIAL_NOTE
    if prose:
        return replace(answer(compose_text(prose, unanswered, gaps), partial, skipped, claims=tuple(kept)),
                       prose=prose, answer_style="prose", unanswered=unanswered, gaps=gaps)
    return answer(_compose(kept), partial, skipped, prose_note, claims=tuple(kept))


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
    check: Generate | None = None,
    write: Generate | None = None,
    books: dict[str, BookInfo] | None = None,
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

    Kept claims are composed in sub-question order, exact and near
    duplicates once (see `_near_duplicate`).
    A part with no kept claim (or no passages, which is not asked) is named
    in the partial note; only when no part keeps a claim is it a refusal.
    `can_answer` does not decide partiality here: whether a part kept a
    verified claim does.

    ONE faithfulness call covers the whole answer, not one per part: the
    claims of every part are judged together, after the near-duplicate
    collapse. A part whose every verified claim the check DROPPED joins the
    "not found" list; a part that lost a claim only to the collapse did not
    lose it -- the same fact is in the answer under another part's wording.

    With one sub-question, or evidence that carries no shares, this is
    `synthesize` exactly: one call over everything.
    """
    if len(sub_questions) < 2 or len(evidence.shares) != len(sub_questions):
        return await synthesize(question, evidence, generate, mode=mode, template=template,
                                note=note, book_titles=book_titles,
                                checklist=sub_questions, check=check, write=write,
                                books=books)

    def answer(text: str, *extra_notes: str | None,
              claims: tuple[Claim, ...] = ()) -> Answer:
        notes = "; ".join(n for n in (note, *extra_notes) if n) or None
        return Answer(text=text, passages=evidence.passages, mode=mode,
                      template=template, note=notes, claims=claims)

    if not evidence.passages:
        return answer(REFUSAL)

    kept: list[Claim] = []
    per_part: list[list[Claim]] = []
    for index, (part, share) in enumerate(zip(sub_questions, evidence.shares)):
        numbers = set(share)
        passages = tuple(p for p in evidence.passages if p.n in numbers)
        if not passages:
            per_part.append([])
            continue
        own = Evidence(edges=evidence.edges, passages=passages,
                       truncated=evidence.truncated,
                       timeline=(evidence.timelines[index]
                                 if len(evidence.timelines) == len(sub_questions) else ()))
        result = await generate(
            _build_prompt(part, own, book_titles, part_of=question, books=books), Synthesis)
        verified, _ = verify_claims(_capped(result), passages)
        per_part.append(verified)
        kept.extend(c for c in verified if c not in kept)

    kept = _collapse(kept)
    kept, cut, prose, skipped, prose_note, unanswered, gaps = await _finish(
        question, sub_questions, kept, evidence, check, write, book_titles,
        part_claims=per_part, books=books)
    missing = [part for part, claims in zip(sub_questions, per_part)
               if not claims or all(c in cut for c in claims)]

    if not kept:
        return answer(REFUSAL)
    partial = (PARTIAL_NOTE + "; " + "; ".join(NOT_FOUND + m for m in missing)
               if missing else None)
    # `claims` carries the SAME post-check list `_compose` just flattened --
    # survivors only, so a claim's presence in Answer is its verdict.
    if prose:
        return replace(answer(compose_text(prose, unanswered, gaps), partial, skipped, claims=tuple(kept)),
                       prose=prose, answer_style="prose", unanswered=unanswered, gaps=gaps)
    return answer(_compose(kept), partial, skipped, prose_note, claims=tuple(kept))
