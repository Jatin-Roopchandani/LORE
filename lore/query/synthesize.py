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

from pydantic import BaseModel, create_model

from lore.query import interpret
from lore.query import write as write_module
from lore.query.models import (
    Answer, BookInfo, Claim, EdgeRow, Evidence, Passage, ProseSentence, SectionSpec,
)
from lore.query.position import order_passages
from lore.query.write import (
    SHORT_ANSWER_HEADING, Repair, assemble, book_infos, cite_restated, clean_gaps, compose_answer,
    compose_text, coverage_gaps, paragraphs_by_book, part_texts, place_uncited, split_items,
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


def _capped(synthesis: Synthesis, limit: int | None = None) -> Synthesis:
    """The first MAX_CLAIMS claims the model wrote (`limit` of them for a deep
    answer's section); the rest are ignored.

    Applied before verification, so the bound is on what the model proposed
    and the choice never depends on which quotes happen to verify.
    """
    return synthesis.model_copy(update={"claims": synthesis.claims[:limit or MAX_CLAIMS]})


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


# Across sub-questions, a claim citing the very quote an earlier part's claim
# cites (same passage, same words, same numbers) restates that fact, however
# differently it is worded: Claude rewords far more than the 0.6 overlap above
# allows ("offered him a choice" vs "told him he had to choose"). Inside one
# part two claims may still draw two facts from one quote. Off: an eval arm.
CROSS_PART_SAME_QUOTE = True
# A list answer (no prose) shows its claims in reading order (passages are
# numbered in story order), not part by part, so a second part that goes back
# to the start of a scene does not make the story jump backwards. Off: an arm.
STORY_ORDER_LIST = True


def _restates(earlier: Claim, claim: Claim) -> bool:
    return (CROSS_PART_SAME_QUOTE and earlier.passage == claim.passage
            and _normalise(earlier.quote) == _normalise(claim.quote)
            and _numbers(earlier.text) == _numbers(claim.text))


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


# A recap's section (lore/query/recap.py) is shown every beat of its stretch: the
# same map, introduced as what it is there.
_RECAP_TIMELINE_BLOCK = """
Story beats: short notes on the main moments of this stretch of the story, in
story order. Each line gives the number of the passage that holds the moment,
where it falls in the story and who it is about. The beats are a map, not
evidence. Use them to see every event of the stretch, from the first to the
last, then read those numbered passages. A claim still needs a quote copied
word for word from a numbered passage; never quote a beat line, and never make
a claim from a beat line alone.
{lines}
"""


def _timeline_block(evidence: Evidence, book_titles: dict[str, str] | None,
                    books: dict[str, BookInfo] | None, template: str = _TIMELINE_BLOCK) -> str:
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
    return template.format(lines="\n".join(lines)) if lines else ""


# CODEX (lore/query/codexslice.py): the same kind of map as the timeline, from
# the codex entries of the question's entities, grouped by aspect.
_CODEX_BLOCK = """
Codex: short notes on what the books establish about the people and things the
question is about, each under the aspect it belongs to, in story order. Each
line gives the number of the passage that holds the statement, where it falls
in the story, who it is about and the aspect. The codex is a map, not
evidence. Use it to see which aspects exist and where the passages are, then
read those numbered passages. A claim still needs a quote copied word for word
from a numbered passage; never quote a codex line, and never make a claim from
a codex line alone.
{lines}
"""
MAX_CODEX_LINES = 40


def _codex_block(evidence: Evidence, book_titles: dict[str, str] | None,
                 books: dict[str, BookInfo] | None) -> str:
    """The "Codex" block, or "" when no line's passage is shown. A line whose
    passage the evidence cap cut is left out: it could not be cited."""
    if not evidence.codex:
        return ""
    order = order_passages(evidence.passages, write_module.book_infos(book_titles, books))
    place = {pos.n: (index, pos) for index, pos in enumerate(order.ordered)}
    number = {p.chunk_id: p.n for p in evidence.passages}
    shown = [(place[number[l.chunk_id]], l) for l in evidence.codex if l.chunk_id in number]
    shown.sort(key=lambda item: item[0][0])
    lines = [f"- [{pos.n}] ({pos.label(detail=True)}) {l.subject}, {l.aspect}: {l.text}"
             for (_, pos), l in shown[:MAX_CODEX_LINES]]
    return _CODEX_BLOCK.format(lines="\n".join(lines)) if lines else ""


# A deep answer's section (lore/query/deep.py): one part of a long answer, asked
# for in full.
_SECTION_CONTEXT = ("This is one section of a long, detailed answer to the larger question: "
                    "{part_of}\n"
                    "Cover this section fully, with every distinct fact the passages establish "
                    "about it, in as many claims as they support; the other sections are "
                    "written separately.\n")
# The same, for a section that answers one aspect (`focus`) of the larger question:
# only the facts that bear on that aspect and on the question are kept.
_SECTION_FOCUS_CONTEXT = ("This is one section of a long, detailed answer to the larger question: "
                          "{part_of}\n"
                          "This section answers one aspect of it: {focus}\n"
                          "Keep only facts that bear on this aspect and on the larger question; "
                          "skip incidental plot detail and anything that only describes the "
                          "setting, however vivid. Cover the aspect fully, with every distinct "
                          "fact the passages establish about it, in as many claims as they "
                          "support; the other sections are written separately.\n")
# A section of a recap (lore/query/recap.py): the events of one stretch, in the
# order they happen; a recap narrates, so nothing is skipped as plot detail.
RECAP_CONTEXT = ("This is one section of a recap, told in story order, that answers: {part_of}\n"
                 "This section covers {focus}. Give the events of this stretch in the order they "
                 "happen: what the characters do, decide, discover and say that moves the story "
                 "on, and how one scene leads to the next. Cover the whole stretch, from its first "
                 "passage to its last, not only its opening, and prefer events to descriptions of "
                 "places or people. State each event once, even when several passages tell it. "
                 "Give as many claims as the passages support; the other stretches are written "
                 "separately.\n")
# Added to a section's claims rule when the larger question is about named entities:
# a fact about someone or something else is off the question, however well it fits the aspect.
SUBJECT_RULE = ("Every fact kept must also bear on {subject}, whom or which the larger question is "
                "about: skip facts that are only about other people or things, even when they fit "
                "the aspect.\n")


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
    role: str = "part",
    section: SectionSpec | None = None,
) -> str:
    """`role`: what the part is for an interpretive answer (interpret.py): a
    "lens" of an interpretive part, a "beyond" part, or an ordinary "part".
    `section`: the part is a section of a deep answer (deep.py)."""
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
    if part_of and role == "lens":
        context = interpret.LENS_CONTEXT.format(part_of=part_of)
    elif part_of and role == "beyond":
        context = interpret.BEYOND_CONTEXT.format(part_of=part_of)
    elif section is not None and section.length == "recap":
        context = RECAP_CONTEXT.format(part_of=section.part_of, focus=section.focus)
    elif section is not None:
        context = (_SECTION_FOCUS_CONTEXT.format(part_of=section.part_of, focus=section.focus)
                   if section.focus else _SECTION_CONTEXT.format(part_of=section.part_of))
        if section.subject:
            context += SUBJECT_RULE.format(subject=" and ".join(section.subject))
    return _PROMPT.format(min_words=MIN_QUOTE_WORDS, min_segment=MIN_SEGMENT_WORDS,
                          facts_rule=(_FULL_FACTS_RULE if FULL_ANSWERS else _FACTS_RULE)
                          + (_NO_RESTATE_RULE if NO_RESTATE else "")
                          + (_ATTRIBUTE_SPEECH_RULE if ATTRIBUTE_SPEECH else "")
                          + (_STRICT_LINKS_RULE if STRICT_LINKS else "")
                          + (_ROLE_COVERAGE_RULE if ROLE_COVERAGE else "")
                          + (_CHANGE_RULE if write_module.STORY_ORDER else "")
                          + (_ACTIONS_RULE if write_module.readings_on() else ""),
                          max_claims=section.max_claims if section is not None else MAX_CLAIMS,
                          question=question, context=context, checklist=covers,
                          relations=(relations + _timeline_block(
                              evidence, book_titles, books,
                              _RECAP_TIMELINE_BLOCK if section is not None
                              and section.length == "recap" else _TIMELINE_BLOCK)
                                     + _codex_block(evidence, book_titles, books)),
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


class RestatedJudgement(Judgement):
    # COVERAGE: the 1-based number of a sentence that already says everything
    # this claim says, or 0.
    restated_by: int = 0


class FlaggedJudgement(Judgement):
    # FACT_FIDELITY: the sentence is a fact but states an interpretation its
    # cited claims do not.
    inference_in_fact: bool = False


class OpenerJudgement(Judgement):
    # OPENER_CITES: for the opening sentence, the claims (1-based) that state what
    # it says, judged against every claim; empty for any other sentence. A reply
    # without it reads as empty (the verdict then stands on the sentence's own claims).
    supported_by: list[int] = []


class FlaggedOpenerJudgement(FlaggedJudgement):
    supported_by: list[int] = []


def _fidelity_schema(restated: bool, inference: bool, opener: bool = False) -> type[BaseModel]:
    """The check's reply when it also judges coverage (write.COVERAGE),
    interpretation in facts (write.FACT_FIDELITY) or an opening against every
    claim (write.OPENER_CITES). Shown to the model as "Faithfulness", like the
    schema it extends."""
    fields = {}
    if restated:
        fields["judgements"] = (list[RestatedJudgement], ...)
    if inference or opener:
        item = ((FlaggedOpenerJudgement if inference else OpenerJudgement) if opener
                else FlaggedJudgement)
        fields["sentence_judgements"] = (list[item], [])
    return create_model("Faithfulness", __base__=Faithfulness, __module__=__name__, **fields)


_FIDELITY_SCHEMAS = {(r, i, o): _fidelity_schema(r, i, o)
                     for r in (True, False) for i in (True, False) for o in (True, False)
                     if r or i or o}


def check_schema(with_sentences: bool, opener: bool = False) -> type[BaseModel]:
    """The schema the check is asked for: the plain one without STORY_ORDER,
    and with sentences to judge, the one carrying the verdicts that are
    switched on (`opener`: the first sentence is judged as an opening)."""
    if not write_module.STORY_ORDER:
        return _PlainFaithfulness
    key = (with_sentences and write_module.coverage_on(),
           with_sentences and write_module.fidelity_on(),
           with_sentences and opener and write_module.opener_cites_on())
    return _FIDELITY_SCHEMAS[key] if any(key) else Faithfulness


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
{summary}- An order word ("at first", "earlier", "later", "by the end", "in the next
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

# SUMMARY_CHECK (2026-11-08, spec orphan-repair): an introduction or summary
# sentence cites several claims and says in general words what they show
# together. Measured on a deep answer: the check rejected such openings ("adds
# ordering", "beyond"), so sections began with a stray detail. With this on,
# the sentence rule says outright that such a sentence is judged against the
# union of its claims; what it still refuses is a real addition. Needs
# STORY_ORDER (it lives in the story block). Off gives the prompt before it.
SUMMARY_CHECK = True
_SUMMARY_RULE = """- A sentence that introduces or sums up, citing several claims, is judged
  against all of them together, as one body of evidence. It is supported when
  everything it says is stated by at least one of its cited claims, or is
  what they show together put in general words (several claims describing
  the same place, person or event may be summed up in one phrase). It is
  "{not_supported}" only when it adds a fact, cause, order or evaluation that
  none of its cited claims states.
"""


# OPENER_CITES (write.OPENER_CITES) extends the summary rule above for the
# opening sentence of a gist, section or lens: it introduces its subject, often
# in words several claims share, so it is judged as a summary against EVERY
# claim of the unit, not only those it lists, and the check names the claims
# that support it, which code then attaches as its citations (`with_opener`).
# What no claim states still fails it.
_SUMMARY_OPENER = """- {which} opens the text: it introduces or sums up the subject. It is
  judged as such a sentence, but against ALL the claims above together, not
  only the ones it lists. It is still "{not_supported}" when it adds a fact,
  name, trait, role, detail, cause, order or evaluation that no claim above
  states. Give {it} supported_by: the numbers of EVERY claim above that states
  something it says, listed or not (empty when it is "{not_supported}").{rest}
"""


def summary_rule(opener: bool = False, every_sentence: bool = False) -> str:
    """The summary rule of the sentence check (SUMMARY_CHECK), extended for the
    opening sentence under OPENER_CITES (`opener`). `every_sentence`: the
    introduction check (INTRO_REPAIR), where every sentence is an opening."""
    rule = _SUMMARY_RULE.format(not_supported=NOT_SUPPORTED) if SUMMARY_CHECK else ""
    if opener and (every_sentence or write_module.opener_cites_on()):
        rule += _SUMMARY_OPENER.format(
            which="Each sentence" if every_sentence else "Sentence 1",
            it="each" if every_sentence else "it", not_supported=NOT_SUPPORTED,
            rest="" if every_sentence else "\n  For every other sentence leave supported_by empty.")
    return rule


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

# COVERAGE: which claims a sentence already says, so code adds no duplicate of them.
_RESTATED_BLOCK = """
For each claim, also give restated_by in its judgements entry: the number of a
sentence above that already says everything the claim says, whether or not
that sentence lists the claim; 0 when no sentence does.
"""

# FACT_FIDELITY: a fact sentence that says more than its claims in the way of
# interpretation, judged apart from the verdict.
_INFERENCE_BLOCK = """
Also set inference_in_fact in each sentence_judgements entry. For a sentence
marked with a kind other than fact, set it false. For any other sentence, set
it true when the sentence states an interpretation that its cited claims do
not state (a motive, an intention, a judgement or an evaluation, such as "to
protect her", "cruelly", "out of jealousy" or "a sign of his guilt"), even if
the rest of it is supported; false otherwise.
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
    shape: "interpret.Shape | None" = None,
    opener: bool = False,
) -> str:
    """The one batched prompt: the cited passages, then the numbered claims.

    `opener` (write.OPENER_CITES): sentence 1 is the unit's opening, judged
    against every claim (`summary_rule`).

    `shape`: the interpretive answer's shape (interpret.py). A reading or a
    counter-case among the sentences is then labelled and given its own test,
    quoting the viewpoint lines that apply.

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
        # Kinds are shown, and a reading's or counter-case's own test given,
        # only when one is among the sentences: an answer of facts alone is
        # checked as before.
        kinds = {getattr(s, "kind", "fact") for s in sentences}
        if shape is not None:
            readings = False
            labelled = bool(kinds & {"reading", "counter"})
        else:
            readings = write_module.readings_on() and "reading" in kinds
            labelled = readings

        def label(s) -> str:
            return f" ({getattr(s, 'kind', 'fact')})" if labelled else ""

        lines = "\n".join(
            f"{i}. sentence{label(s)}: {s.text.strip()}\n"
            f"   uses claims {', '.join(str(k) for k in s.claims)}"
            for i, s in enumerate(sentences, start=1))
        summary = summary_rule(opener) if story else ""
        prompt += (_STORY_SENTENCES_BLOCK.format(summary=summary, sentences=lines,
                                                 count=len(sentences), supported=SUPPORTED,
                                                 not_supported=NOT_SUPPORTED,
                                                 max_words=MAX_REASON_WORDS)
                   if story else
                   _SENTENCES_BLOCK.format(sentences=lines, count=len(sentences),
                                           supported=SUPPORTED, not_supported=NOT_SUPPORTED,
                                           max_words=MAX_REASON_WORDS))
        if readings:
            prompt += _READINGS_BLOCK.format(supported=SUPPORTED, not_supported=NOT_SUPPORTED)
        if shape is not None:
            prompt += interpret.check_blocks(shape, kinds, SUPPORTED, NOT_SUPPORTED)
        if write_module.coverage_on():
            prompt += _RESTATED_BLOCK
        if write_module.fidelity_on():
            prompt += _INFERENCE_BLOCK
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
    # COVERAGE, per claim: the 1-based number of the sentence that restates it,
    # or 0. None when not asked.
    restated: list[int] | None = None
    # FACT_FIDELITY, per sentence: True when the check says it is a fact
    # stating an interpretation its claims do not. None when not asked.
    inference: list[bool] | None = None
    # Per sentence, the check's reason (shown to the writer in a repair, never
    # parsed). None when the sentence verdicts are None.
    sentence_reasons: list[str] | None = None
    # OPENER_CITES: the claims (1-based, in range) the check says support the
    # opening sentence, judged against every claim. None when not asked.
    opener_support: list[int] | None = None


async def check_answer(
    claims: Sequence[Claim],
    passages: Iterable[Passage],
    check: Generate,
    *,
    book_titles: dict[str, str] | None = None,
    sentences: Sequence["Sentence"] | None = None,
    books: dict[str, BookInfo] | None = None,
    gaps: Sequence[str] | None = None,
    shape: "interpret.Shape | None" = None,
    opener: bool = False,
) -> CheckOutcome:
    """`check_claims`, plus one verdict per prose sentence in the same call.

    Claim and sentence verdicts are validated separately: garbled sentence
    verdicts cost only the prose (`sentence_ok=None`), never the claim check.
    Gap verdicts likewise: garbled ones cost only the gaps (`gap_ok=None`).

    `opener` (write.OPENER_CITES): the first sentence, when it is a fact, is the
    unit's opening, judged against every claim; the claims the check says support
    it come back in `opener_support` (`with_opener` attaches them).
    """
    claims = list(claims)
    if not claims:
        return CheckOutcome([], [], True, [], None)
    passages = list(passages)
    opener = bool(opener and sentences and write_module.opener_cites_on()
                  and getattr(sentences[0], "kind", "fact") == "fact")
    gap_ok = restated = inference = reasons = support = None
    try:
        result = await check(
            faithfulness_prompt(claims, passages, book_titles, sentences, books, gaps, shape,
                                opener=opener),
            check_schema(bool(sentences), opener))
        verdicts = validated_verdicts(result, len(claims))
        sentence_ok = (validated_sentence_verdicts(result, len(sentences))
                       if sentences and verdicts is not None else None)
        if gaps and sentences and verdicts is not None and write_module.STORY_ORDER:
            gap_ok = validated_gap_verdicts(result, len(gaps))
        if sentence_ok is not None and write_module.coverage_on():
            restated = [_sentence_number(getattr(j, "restated_by", 0), len(sentences))
                        for j in result.judgements]
        if sentence_ok is not None and write_module.fidelity_on():
            inference = [getattr(j, "inference_in_fact", False) is True
                         for j in result.sentence_judgements]
        if sentence_ok is not None:
            reasons = [" ".join(str(getattr(j, "reason", "") or "").split())
                       for j in result.sentence_judgements]
        if sentence_ok is not None and opener:
            support = supporting_claims(getattr(result.sentence_judgements[0], "supported_by", None),
                                        len(claims))
    except Exception:  # noqa: BLE001 -- see check_claims
        verdicts, sentence_ok = None, None
    if verdicts is None:
        return CheckOutcome(claims, [], False, [True] * len(claims), None)
    kept: list[Claim] = []
    dropped: list[tuple[Claim, str]] = []
    for claim, (supported, reason) in zip(claims, verdicts):
        (kept.append(claim) if supported else dropped.append((claim, reason)))
    return CheckOutcome(kept, dropped, True, [ok for ok, _ in verdicts], sentence_ok, gap_ok,
                        restated, inference, reasons, support)


def supporting_claims(value, count: int) -> list[int]:
    """The claim numbers in `value` that are among 1..count, in order, once each;
    [] for anything that is not a list of numbers."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[int] = []
    for v in value:
        n = _sentence_number(v, count)
        if n and n not in out:
            out.append(n)
    return out


# INTRO_REPAIR: the check of an introduction written by its own call. The claims
# were checked against their passages already, so only the sentences are judged,
# each against every claim, as an opening is under OPENER_CITES.
_INTRO_CHECK_PROMPT = """Check an introduction against the numbered claims below. Each claim was already
checked against the book; judge only the sentences.

Claims:
{claims}
{order_note}
Sentences:
{sentences}

Judge each sentence against ALL the claims above, together. Judge meaning, not
wording.
{summary}- A sentence that sums up with more than the claims show, such as "always" or
  "every" when they show one case or some, is "{not_supported}".
- Sentence 1 must introduce the subject: say what or who it is, and how it
  figures in the story. A sentence 1 that gives only one detail or event of
  the subject instead is "{not_supported}" (reason: "not an introduction").
Return these in sentence_judgements, one per sentence, numbered 1 to {count} in
the claim field, with a verdict ("{supported}" or "{not_supported}"), a reason
of at most {max_words} words, and supported_by.
"""


class IntroCheck(BaseModel):
    sentence_judgements: list[OpenerJudgement]


def intro_check_prompt(claims: Sequence[Claim], passages: Iterable[Passage],
                       sentences: Sequence["Sentence"], book_titles: dict[str, str] | None = None,
                       books: dict[str, BookInfo] | None = None) -> str:
    by_number = {p.n: p for p in passages}
    cited = [by_number[n] for n in sorted({c.passage for c in claims}) if n in by_number]
    order = order_passages(cited, book_infos(book_titles, books))
    items = []
    for number, claim in enumerate(claims, start=1):
        where = order.positions.get(claim.passage)
        items.append(f"{number}. claim: {claim.text.strip()}"
                     + (f" ({where.label(detail=True)})" if where else "")
                     + f"\n   quote: {claim.quote.strip()}")
    lines = "\n".join(f"{i}. sentence: {s.text.strip()}\n   uses claims {', '.join(map(str, s.claims))}"
                      for i, s in enumerate(sentences, start=1))
    return _INTRO_CHECK_PROMPT.format(
        claims="\n".join(items), order_note=("\n" + order.note() + "\n") if order.note() else "",
        sentences=lines, summary=summary_rule(opener=True, every_sentence=True), count=len(sentences),
        supported=SUPPORTED, not_supported=NOT_SUPPORTED, max_words=MAX_REASON_WORDS)


async def check_intro(claims: Sequence[Claim], passages: Iterable[Passage],
                      sentences: Sequence["Sentence"], check: Generate, *,
                      book_titles: dict[str, str] | None = None,
                      books: dict[str, BookInfo] | None = None) -> list[tuple[bool, list[int]]] | None:
    """INTRO_REPAIR's check: per sentence, (supported, the claims supporting it),
    or None when the call failed or its verdicts do not line up."""
    try:
        result = await check(intro_check_prompt(claims, list(passages), sentences, book_titles, books),
                             IntroCheck)
        flags = validated_sentence_verdicts(result, len(sentences))
    except Exception:  # noqa: BLE001 -- a failed extra check shows no introduction
        return None
    if flags is None:
        return None
    return [(ok, supporting_claims(getattr(j, "supported_by", None), len(claims)))
            for ok, j in zip(flags, result.sentence_judgements)]


def with_opener(sentences: Sequence["Sentence"], outcome: CheckOutcome) -> list["Sentence"]:
    """OPENER_CITES: the sentences with the opening's citations replaced by what
    the check found supporting it against every claim (`outcome.opener_support`)
    plus those of its own claims the check kept, when the check passed it. An
    opening the check rejected, or one it gave no supporting claims for, keeps the
    citations the writer gave (and its verdict). A supporting claim the check cut
    is still cited, so the opening falls with it, as any sentence would."""
    out = list(sentences)
    ok = outcome.sentence_ok or []
    if not (out and outcome.opener_support and ok and ok[0]):
        return out
    own = [k for k in out[0].claims if 1 <= k <= len(outcome.claim_ok) and outcome.claim_ok[k - 1]]
    out[0] = out[0].model_copy(update={"claims": sorted(set(outcome.opener_support) | set(own))})
    return out


def _sentence_number(value, count: int) -> int:
    """A sentence number the check gave, or 0 when it is not one of 1..count."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if 1 <= n <= count else 0


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
                  part_claims=None, books=None, shape=None, length=None, covered=(),
                  spend_ok=None):
    """Writer (when it applies) and the faithfulness check, in one place for
    all synthesize paths. Returns (kept, cut, prose, skipped_note, prose_note,
    unanswered part texts, gaps -- both set only when the prose is shown).

    `shape` (interpret.Shape): the answer has an interpretive or beyond part.
    The writer and the check then follow it, and code closes the prose with
    its lines (interpret.close_answer); the shown gaps join the limits line
    and so are not returned separately.

    `length` ("section", "short", "lens" or "recap"): the writer sizes the prose for a deep
    answer's section or its short answer (write.py) instead of the default cap.

    `spend_ok`: asked before an ORPHAN_REPAIR round (False: no round, and an
    orphaned prose falls back to its claim list). None: always allowed."""
    needs_writer = (write_module.PROSE_FACTUAL or length is not None
                    or (shape is not None and (shape.interpretive or bool(shape.beyond))))
    try_prose = (write_module.PROSE_ANSWERS and needs_writer and write is not None
                 and check is not None and len(kept) >= 2)
    sentences, written, prose_note, missing, gaps = None, 0, None, [], []
    if try_prose:
        drafted = await write_answer(question, sub_questions, kept, evidence.passages, write,
                                     book_titles=book_titles, books=books, shape=shape,
                                     length=length, covered=covered)
        if drafted is None:
            prose_note = PROSE_FALLBACK_NOTE
        else:
            texts = [c.text for c in kept]
            sentences, written = (verify_prose(drafted, len(kept), texts, shape),
                                  len(split_items(drafted)))
            parts = part_texts(question, sub_questions)
            missing = [parts[k - 1] for k in valid_unanswered(drafted, len(parts))]
            if shape is not None:  # a part the books cannot settle is a limit, said by code
                missing = [m for m in missing if m not in shape.beyond]
            if write_module.STORY_ORDER:
                gaps = [g for g in clean_gaps(drafted.gaps)
                        if g.casefold() not in {m.casefold() for m in missing}]
    if check is None or not kept:
        return kept, [], (), None, prose_note, (), ()
    passage_of = [c.passage for c in kept]
    opener = write_module.opener_on(length, shape)
    outcome = await check_answer(kept, evidence.passages, check, book_titles=book_titles,
                                 sentences=sentences or None, books=books,
                                 gaps=gaps if sentences else None, shape=shape, opener=opener)
    if sentences:
        sentences = with_opener(sentences, outcome)
    skipped = None if outcome.ran else CHECK_SKIPPED_NOTE
    prose: tuple[ProseSentence, ...] = ()
    # COVERAGE: every kept claim the check did not cut reaches the shown prose.
    # A short answer summarises the sections after it, so it is exempt.
    enforce = write_module.coverage_on() and length not in ("short", "recap_short")
    # FACT_FIDELITY: a fact sentence stating an interpretation is repaired, or not shown.
    fix_facts = write_module.fidelity_on()

    def prepare(sents, n_written, outc):
        sents, ok = list(sents), list(outc.sentence_ok or [])
        if enforce:
            sents = cite_restated(sents, outc.claim_ok, ok, outc.restated)
        if fix_facts and outc.inference and len(outc.inference) == len(sents) == len(ok):
            # Not shown, and not counted against the prose: its claims are
            # true, and COVERAGE places them in their own words. For
            # ORPHAN_REPAIR it is a removed sentence like any other.
            flagged = [flag and getattr(s, "kind", "fact") == "fact"
                       for s, flag in zip(sents, outc.inference)]
            n_written -= sum(flagged)
            ok = [g and not f for g, f in zip(ok, flagged)]
            sents = [s if not f else s.model_copy(update={"kind": "removed"})
                     for s, f in zip(sents, flagged)]
        return sents, ok, n_written

    def build(sents, n_written, outc, lenient=False):
        sents, ok, n_written = prepare(sents, n_written, outc)
        shown = [(s, g) for s, g in zip(sents, ok) if getattr(s, "kind", "fact") != "removed"]
        return assemble([s for s, _ in shown], n_written, outc.claim_ok, [g for _, g in shown],
                        passage_of, lenient=lenient)

    def kept_mask(sents, n_written, outc):
        sents, ok, _ = prepare(sents, n_written, outc)
        return sents, write_module.kept_flags(sents, outc.claim_ok, ok)

    if sentences is not None:
        ready = bool(sentences) and outcome.ran and outcome.sentence_ok is not None
        built = build(sentences, written, outcome) if ready else None
        if built and (enforce or fix_facts):
            # The repair round serves a draft that passed: a draft that did not
            # takes the fallback (or a section's rewrite) as before.
            repaired = await _repair(question, sub_questions, kept, evidence, check, write,
                                     book_titles, books, shape, length, covered, sentences,
                                     outcome, gaps, enforce, opener)
            better = build(*repaired) if repaired is not None else None
            if better:  # used only if it passes too; else the first draft stands
                built, (sentences, written, outcome) = better, repaired
        rejected = ([s.text for s, ok in zip(sentences, outcome.sentence_ok or [])
                     if not ok and getattr(s, "kind", "fact") == "fact"]
                    if sentences and len(outcome.sentence_ok or []) == len(sentences) else [])
        if built is None and length in ("section", "recap") and outcome.ran and rejected:
            # A deep (or recap) section gets one rewrite before it falls back to its claim
            # list: the writer is told which sentences the check rejected.
            again = await write_answer(question, sub_questions, kept, evidence.passages, write,
                                       book_titles=book_titles, books=books, shape=shape,
                                       length=length, avoid=rejected, covered=covered)
            second = (verify_prose(again, len(kept), [c.text for c in kept], shape)
                      if again is not None else [])
            if second:
                outcome2 = await check_answer(kept, evidence.passages, check,
                                              book_titles=book_titles, sentences=second,
                                              books=books, shape=shape, opener=opener)
                second = with_opener(second, outcome2)
                if outcome2.ran and outcome2.sentence_ok is not None:
                    built = build(second, len(split_items(again)), outcome2)
                    if built:
                        sentences, written = second, len(split_items(again))
                        outcome, gaps = outcome2, []
        if built and write_module.orphan_repair_on():
            # ORPHAN_REPAIR: no shown sentence leans on one the check removed,
            # and a section whose opening was removed gets a new one
            # (GENTLE_FALLBACK, INTRO_REPAIR: `_mend`).
            built, sentences, written, outcome, _opened = await _mend(
                question, sub_questions, kept, evidence.passages, check, write, book_titles,
                books, shape, length, covered, sentences, written, outcome, gaps, built, build,
                kept_mask, spend_ok)
        if built and enforce:
            built = place_uncited(built, [c.text for c in outcome.kept],
                                  [c.passage for c in outcome.kept])
        if built and part_claims is not None:
            numbered = [[outcome.kept.index(c) + 1 for c in cs if c in outcome.kept]
                        for cs in part_claims]
            if coverage_gaps(built, numbered):
                built = None  # one-sided prose: the claim list covers every part
        if built:
            book_of = {p.n: p.book_id for p in evidence.passages}
            if shape is None:
                prose = paragraphs_by_book(built, book_of)
            else:  # the interpretation keeps the paragraphs the guards gave it
                prose = (paragraphs_by_book([s for s in built if s.kind == "fact"], book_of)
                         + tuple(s for s in built if s.kind != "fact"))
        else:
            prose_note = PROSE_FALLBACK_NOTE
    # A gap is shown only if the check ran over it and agreed: an unchecked
    # "the passages don't say" is a claim like any other.
    shown_gaps = (tuple(g for g, ok in zip(gaps, outcome.gap_ok) if ok)
                  if prose and outcome.gap_ok is not None and len(outcome.gap_ok) == len(gaps) else ())
    if shape is not None and prose:
        survivors = [k for k, ok in enumerate(outcome.claim_ok, start=1) if ok]
        prose, shown_gaps = interpret.close_answer(prose, shape, shown_gaps, survivors), ()
    return (outcome.kept, [c for c, _ in outcome.dropped], prose, skipped, prose_note,
            tuple(missing) if prose else (), shown_gaps)


def uncited_claims(sentences: Sequence["Sentence"], outcome: CheckOutcome) -> list[int]:
    """The kept claims (1-based) that no sentence the check passed cites, and
    that the check does not say a passing fact sentence restates."""
    ok = outcome.sentence_ok or []
    claim_ok = outcome.claim_ok
    passing = [good and all(1 <= k <= len(claim_ok) and claim_ok[k - 1] for k in s.claims)
               for s, good in zip(sentences, ok)]
    cited = {k for s, good in zip(sentences, passing) if good for k in s.claims}
    restated = outcome.restated or []
    covered = {k for k, at in enumerate(restated, start=1)
               if 1 <= at <= len(sentences) and passing[at - 1]
               and getattr(sentences[at - 1], "kind", "fact") == "fact"}
    return [k for k, good in enumerate(claim_ok, start=1)
            if good and k not in cited and k not in covered]


async def _repair(question, sub_questions, kept, evidence, check, write, book_titles, books,
                  shape, length, covered, sentences, outcome, gaps, enforce=True, opener=False):
    """The repair round: (sentences, written, outcome) of the writer's revised
    draft, checked again, or None when nothing needs repair or the round
    failed (the first draft then stands, and code places what it left out).
    The writer sees the sentences that passed, the claims they leave out
    (COVERAGE, when `enforce`) and the fact sentences the check found stating
    an interpretation (FACT_FIDELITY), and is asked to fix those without
    changing the rest."""
    missing = uncited_claims(sentences, outcome) if enforce else []
    ok = outcome.sentence_ok or []
    flags = (outcome.inference if write_module.fidelity_on() and outcome.inference
             and len(outcome.inference) == len(sentences) else [False] * len(sentences))
    passing = [(s, flag) for s, good, flag in zip(sentences, ok, flags)
               if good and all(outcome.claim_ok[k - 1] for k in s.claims)]
    shown = [s for s, _ in passing]
    reduce = [i for i, (s, flag) in enumerate(passing, start=1)
              if flag and getattr(s, "kind", "fact") == "fact"]
    if not missing and not reduce:
        return None
    repair = Repair(draft=tuple(shown), missing=tuple(missing), reduce=tuple(reduce),
                    as_reading=write_module.readings_allowed(shape, length))
    again = await write_answer(question, sub_questions, kept, evidence.passages, write,
                               book_titles=book_titles, books=books, shape=shape, length=length,
                               covered=covered, repair=repair)
    second = (verify_prose(again, len(kept), [c.text for c in kept], shape)
              if again is not None else [])
    if not second:
        return None
    outcome2 = await check_answer(kept, evidence.passages, check, book_titles=book_titles,
                                  sentences=second, books=books, gaps=gaps or None, shape=shape,
                                  opener=opener)
    if not (outcome2.ran and outcome2.sentence_ok is not None):
        return None
    return with_opener(second, outcome2), len(split_items(again)), outcome2


# ORPHAN_REPAIR: the parts whose opening sentence, removed by the check, is
# written again (an introduction). A short answer, a recap section (which opens
# with its first event) and an interpretive answer (whose opening is its
# readings) are left as they are; their orphans are still repaired.
INTRO_LENGTHS = (None, "section", "lens")


async def _orphan_repair(question, sub_questions, kept, passages, check, write, book_titles,
                         books, shape, length, covered, sentences, keep, outcome, gaps, intro,
                         spend_ok=None):
    """ORPHAN_REPAIR's one round: (sentences, written, outcome) of the writer's
    revised draft, checked once, in the numbering of `kept`; or None when the
    spend check says no or the round failed.

    The writer sees only the claims the check kept (renumbered for it), the
    sentences that were shown (the orphans among them marked), the sentences
    the check removed with its reasons, and, with `intro`, that the opening was
    removed. `sentences`/`keep`: the draft as shown, and which were kept."""
    if write is None or check is None or (spend_ok is not None and not spend_ok()):
        return None
    idx = [i for i, ok in enumerate(outcome.claim_ok) if ok]
    if not idx:
        return None
    sub_claims = [kept[i] for i in idx]
    to_sub = {i + 1: j + 1 for j, i in enumerate(idx)}
    orphaned = set(write_module.orphans(sentences, keep))
    shown = [i for i, k in enumerate(keep) if k]
    draft = tuple(sentences[i].model_copy(update={"claims": [to_sub[k] for k in sentences[i].claims]})
                  for i in shown)
    reasons, verdicts = outcome.sentence_reasons or [], outcome.sentence_ok or []

    def why(i: int) -> str:
        if getattr(sentences[i], "kind", "fact") == "removed":
            return "states an interpretation its facts do not"
        if i < len(verdicts) and not verdicts[i]:
            return reasons[i] if i < len(reasons) else ""
        return "rests on a fact that did not check out"

    removed = tuple((s.text, why(i)) for i, s in enumerate(sentences)
                    if not keep[i] and getattr(s, "kind", "fact") in ("fact", "removed"))
    sub_shape = (shape.with_claims([shape.claim_parts[i] for i in idx])
                 if shape is not None and len(shape.claim_parts) == len(kept) else shape)
    repair = Repair(draft=draft, orphans=tuple(n for n, i in enumerate(shown, start=1)
                                               if i in orphaned),
                    intro=intro, removed=removed)
    again = await write_answer(question, sub_questions, sub_claims, passages, write,
                               book_titles=book_titles, books=books, shape=sub_shape,
                               length=length, covered=covered, repair=repair)
    second = (verify_prose(again, len(sub_claims), [c.text for c in sub_claims], sub_shape)
              if again is not None else [])
    if not second:
        return None
    checked = await check_answer(sub_claims, passages, check, book_titles=book_titles,
                                 sentences=second, books=books, gaps=gaps or None, shape=sub_shape,
                                 opener=write_module.opener_on(length, sub_shape))
    if not (checked.ran and checked.sentence_ok is not None):
        return None
    second = with_opener(second, checked)
    # Back to the numbering of `kept`: a claim cut before the round stays cut.
    lifted = [s.model_copy(update={"claims": [idx[k - 1] + 1 for k in s.claims]}) for s in second]
    claim_ok = [False] * len(kept)
    for j, i in enumerate(idx):
        claim_ok[i] = checked.claim_ok[j]
    restated = None
    if checked.restated is not None:
        restated = [0] * len(kept)
        for j, i in enumerate(idx):
            restated[i] = checked.restated[j]
    merged = CheckOutcome(
        kept=[c for c, ok in zip(kept, claim_ok) if ok],
        dropped=[*outcome.dropped, *checked.dropped], ran=True, claim_ok=claim_ok,
        sentence_ok=checked.sentence_ok, gap_ok=checked.gap_ok, restated=restated,
        inference=checked.inference, sentence_reasons=checked.sentence_reasons,
        opener_support=([idx[k - 1] + 1 for k in checked.opener_support]
                        if checked.opener_support is not None else None))
    return lifted, len(split_items(again)), merged


def _intro_from(fixed, kept_mask, first_texts, kept, claim_ok) -> ProseSentence | None:
    """GENTLE_FALLBACK: the opening the repair round wrote when it was asked for
    one, as a shown sentence numbered among the survivors of `claim_ok` (the first
    draft's check): kept by the repair's check, new (not a sentence of the first
    draft), standing on its own, and resting only on claims the first check kept."""
    if fixed is None:
        return None
    sents, keep = kept_mask(*fixed)
    if not (sents and keep and keep[0]):
        return None
    s = sents[0]
    if (getattr(s, "kind", "fact") != "fact" or s.text in first_texts
            or write_module.leans_back(s.text)
            or not all(1 <= k <= len(claim_ok) and claim_ok[k - 1] for k in s.claims)):
        return None
    survivor = {k: n for n, k in enumerate((k for k, ok in enumerate(claim_ok, start=1) if ok),
                                           start=1)}
    return ProseSentence(text=s.text.strip(), claims=tuple(survivor[k] for k in s.claims),
                         passages=tuple(sorted({kept[k - 1].passage for k in s.claims})))


def _gentler(built, prepared, keep, intro: ProseSentence | None):
    """GENTLE_FALLBACK's second choice: the first draft as assembled (`built`, its
    rejected sentences already out), with every orphaned sentence dropped on its
    own and in turn (`write.drop_orphans`), and `intro` put first. (prose or None
    when nothing stands, whether its own opening stands)."""
    rows = [i for i, k in enumerate(keep) if k]
    if len(rows) != len(built):
        return None, False
    left = write_module.drop_orphans(prepared, keep)
    shown: list[ProseSentence] = []
    carry = False
    for b, i in zip(built, rows):
        if left[i]:
            shown.append(replace(b, new_paragraph=bool(shown) and (b.new_paragraph or carry)))
            carry = False
        else:  # the paragraph it opened goes on from the next sentence shown
            carry = carry or b.new_paragraph
    if not any(s.kind == "fact" for s in shown):
        return None, False
    opened = bool(left) and left[0]
    if intro is not None:
        shown = [intro, *write_module.drop_restated([intro], shown)]
        opened = True
    return tuple(shown), opened


async def _intro_repair(subject, claims, passages, check, write, book_titles, books, length,
                        spend_ok=None) -> tuple[ProseSentence, ...]:
    """INTRO_REPAIR's one round: an introduction over `claims` (the claims the
    check kept, so numbered as the shown prose numbers them), checked once, each
    sentence against every claim and citing what the check says supports it; ()
    when the spend check says no, the call fails, or its first sentence fails."""
    if write is None or check is None or not claims or (spend_ok is not None and not spend_ok()):
        return ()
    intro = await write_module.write_intro(subject, claims, passages, write,
                                           book_titles=book_titles, books=books, length=length)
    if not intro:
        return ()
    verdicts = await check_intro(claims, passages, intro, check, book_titles=book_titles, books=books)
    if not verdicts:
        return ()
    out: list[ProseSentence] = []
    for s, (ok, support) in zip(intro, verdicts):
        if not ok:
            break  # a second sentence without the first is no introduction
        numbers = sorted(set(s.claims) | set(support))
        out.append(ProseSentence(text=s.text.strip(), claims=tuple(numbers),
                                 passages=tuple(sorted({claims[k - 1].passage for k in numbers}))))
    return tuple(out)


async def _mend(question, sub_questions, kept, passages, check, write, book_titles, books, shape,
                length, covered, sentences, written, outcome, gaps, built, build, kept_mask,
                spend_ok=None):
    """After a draft assembled (`built`): ORPHAN_REPAIR's round for orphans and a
    removed opening, the fallback when it fails (GENTLE_FALLBACK), and an
    introduction of its own when the opening is still rejected (INTRO_REPAIR).
    Returns (built or None for the claim list, sentences, written, outcome, whether
    the shown prose begins with an opening). `build`/`kept_mask` turn (sentences,
    written, outcome) into the shown prose and into (sentences, kept flags)."""
    prepared, keep = kept_mask(sentences, written, outcome)
    opened = bool(keep) and keep[0]
    first_texts = [s.text for s in prepared]
    orphaned = write_module.orphans(prepared, keep)
    intro = length in INTRO_LENGTHS and shape is None and write_module.opener_cut(keep)
    if orphaned or intro:
        fixed = await _orphan_repair(
            question, sub_questions, kept, passages, check, write, book_titles,
            books, shape, length, covered, prepared, keep, outcome, gaps, intro, spend_ok)
        mask = kept_mask(*fixed) if fixed is not None else None
        clean = mask is not None and not write_module.orphans(*mask)
        better = build(*fixed) if clean else None
        if not better and clean and write_module.gentle_fallback_on():
            # GENTLE_FALLBACK, first choice: the repaired draft minus its rejected
            # sentences, however many, when none of the rest is orphaned.
            better = build(*fixed, lenient=True)
        if better:
            built, (sentences, written, outcome) = better, fixed
            # its first sentence is an opening unless it is a later sentence of the first draft
            opened = bool(mask[1]) and mask[1][0] and mask[0][0].text not in first_texts[1:]
        elif write_module.gentle_fallback_on():
            # Second choice: the first draft minus its rejected sentences and, one by
            # one, its orphans, after the repair's new opening when that was supported.
            new = (_intro_from(fixed, kept_mask, first_texts, kept, outcome.claim_ok)
                   if intro else None)
            built, opened = _gentler(built, prepared, keep, new)
        elif orphaned:
            built = None  # never shown with an orphaned opening: the claim list is
    if built and not opened and write_module.intro_wanted(length, shape):
        got = await _intro_repair(question, outcome.kept, passages, check, write, book_titles,
                                  books, length, spend_ok)
        if got:
            built, opened = (*got, *write_module.drop_restated(got, built)), True
    return built, sentences, written, outcome, opened


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
    section: SectionSpec | None = None,
    spend_ok=None,
) -> Answer:
    """`checklist`: the question's sub-questions; shown only when there are
    several, so a partial answer is visible as partial.

    `section`: the answer is one section of a deep answer (deep.py): its claims
    step may keep more claims and its writer has no word cap.

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
        _build_prompt(question, evidence, book_titles, checklist, books=books,
                      section=section), Synthesis)

    # The answer is written by code, from claims whose quotes were found in
    # the passage they cite. Nothing the model wrote outside a kept claim
    # reaches the user, so there is no free text to carry a stray claim.
    result = _capped(result, section.max_claims if section is not None else None)
    verified, _ = verify_claims(result, evidence.passages)
    kept = _collapse(verified)
    if section is not None and section.skip is not None:
        kept = section.skip(kept, evidence.passages)
    # The writer of a section that answers one aspect is told which larger question it serves.
    written_for = (f"{question} (one aspect of the larger question: {section.part_of})"
                   if section is not None and section.focus else question)
    kept, _cut, prose, skipped, prose_note, unanswered, gaps = await _finish(
        written_for, list(checklist) or [question], kept, evidence, check, write, book_titles,
        books=books, length=section.length if section is not None else None,
        covered=section.covered if section is not None else (), spend_ok=spend_ok)
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
    plan: Sequence | None = None,
    retrieval: Sequence | None = None,
    viewpoint: str | None = None,
    spend_ok=None,
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

    `plan` (decompose.Part per part of the question), `retrieval`
    (decompose.RetrievalPart per entry of `sub_questions`, which are then the
    retrieval parts: parts and the lenses of interpretive parts) and
    `viewpoint`: an answer with an interpretive or beyond part takes the
    interpretive shape (`synthesize_shaped`). Without them, or without such a
    part, nothing here differs.
    """
    if (plan is not None and retrieval is not None and len(retrieval) == len(sub_questions)
            and interpret.shaped([part.kind for part in plan])):
        return await synthesize_shaped(
            question, plan, retrieval, evidence, generate, mode=mode, template=template,
            note=note, book_titles=book_titles, check=check, write=write, books=books,
            viewpoint=viewpoint, spend_ok=spend_ok)
    if len(sub_questions) < 2 or len(evidence.shares) != len(sub_questions):
        return await synthesize(question, evidence, generate, mode=mode, template=template,
                                note=note, book_titles=book_titles,
                                checklist=sub_questions, check=check, write=write,
                                books=books, spend_ok=spend_ok)

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
                                 if len(evidence.timelines) == len(sub_questions) else ()),
                       codex=(evidence.codexes[index]
                              if len(evidence.codexes) == len(sub_questions) else ()))
        result = await generate(
            _build_prompt(part, own, book_titles, part_of=question, books=books), Synthesis)
        verified, _ = verify_claims(_capped(result), passages)
        per_part.append(verified)
        earlier = [c for claims in per_part[:-1] for c in claims]
        kept.extend(c for c in verified
                    if c not in kept and not any(_restates(e, c) for e in earlier))

    kept = _collapse(kept)
    kept, cut, prose, skipped, prose_note, unanswered, gaps = await _finish(
        question, sub_questions, kept, evidence, check, write, book_titles,
        part_claims=per_part, books=books, spend_ok=spend_ok)
    if STORY_ORDER_LIST and not prose:
        kept = sorted(kept, key=lambda c: c.passage)  # stable: one passage keeps its order
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


def _representative(claim: Claim, kept: Sequence[Claim]) -> int | None:
    """The index in `kept` of the claim that stands for `claim` after the
    near-duplicate collapse: itself, or the earlier claim it duplicates."""
    for i, other in enumerate(kept):
        if other == claim or _sentence(other) == _sentence(claim) or _near_duplicate(other, claim):
            return i
    return None


@dataclass
class ShapedClaims:
    """The claims of an interpretive answer, gathered per retrieval part: the
    `kept` list (duplicates collapsed), the claims each retrieval part found
    (`per_part`) and each plan part's own (`by_part`), and the answer's
    `shape` (which retrieval parts found each kept claim)."""

    kept: list[Claim]
    per_part: list[list[Claim]]
    by_part: list[list[Claim]]
    shape: "interpret.Shape"


async def shaped_claims(question: str, plan: Sequence, retrieval: Sequence, evidence: Evidence,
                        generate: Generate, *, book_titles: dict[str, str] | None = None,
                        books: dict[str, BookInfo] | None = None, viewpoint: str | None = None,
                        max_claims: int | None = None) -> ShapedClaims:
    """One claims call per retrieval part, over its own share of the evidence: a
    lens gathers one aspect of its interpretive part, a beyond part gathers
    what the books do say. `max_claims` lifts the claim cap of each call (a deep
    answer's sections)."""
    from lore.query.viewpoint import load_viewpoint

    n = len(retrieval)
    everything = tuple(p.n for p in evidence.passages)
    shares = evidence.shares if len(evidence.shares) == n else (everything,) * n
    timelines = (evidence.timelines if len(evidence.timelines) == n
                 else (evidence.timeline,) * n if n == 1 else ((),) * n)
    codexes = (evidence.codexes if len(evidence.codexes) == n
               else (evidence.codex,) * n if n == 1 else ((),) * n)
    kept: list[Claim] = []
    per_part: list[list[Claim]] = []
    for index, (part, share) in enumerate(zip(retrieval, shares)):
        numbers = set(share)
        passages = tuple(p for p in evidence.passages if p.n in numbers)
        if not passages:
            per_part.append([])
            continue
        own = Evidence(edges=evidence.edges, passages=passages, truncated=evidence.truncated,
                       timeline=timelines[index], codex=codexes[index])
        served = plan[part.part]
        role = ("lens" if part.lens else "beyond" if served.kind == "beyond" else "part")
        spec = (SectionSpec(part_of=question, max_claims=max_claims, length="section")
                if max_claims else None)
        result = await generate(
            _build_prompt(part.question, own, book_titles,
                          part_of=served.question if part.lens else question,
                          books=books, role=role, section=spec), Synthesis)
        verified, _ = verify_claims(_capped(result, max_claims), passages)
        per_part.append(verified)
        kept.extend(c for c in verified if c not in kept)

    kept = _collapse(kept)
    shape = interpret.Shape(
        viewpoint=viewpoint if viewpoint is not None else load_viewpoint(),
        parts=tuple(p.question for p in plan), kinds=tuple(p.kind for p in plan),
        retrieval=tuple((r.question, r.part, r.lens) for r in retrieval),
        stances=tuple(getattr(p, "stance", "analysis") for p in plan),
        labels=tuple(getattr(p, "label", "") for p in plan))
    found: list[set[int]] = [set() for _ in kept]
    for ri, claims in enumerate(per_part):
        for claim in claims:
            at = _representative(claim, kept)
            if at is not None:
                found[at].add(ri)
    shape = shape.with_claims([frozenset(f) for f in found])
    # Per part of the plan, the claims its retrieval parts found.
    by_part: list[list[Claim]] = [[] for _ in plan]
    for ri, claims in enumerate(per_part):
        by_part[retrieval[ri].part].extend(c for c in claims if c not in by_part[retrieval[ri].part])
    return ShapedClaims(kept, per_part, by_part, shape)


async def synthesize_shaped(
    question: str,
    plan: Sequence,
    retrieval: Sequence,
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
    viewpoint: str | None = None,
    spend_ok=None,
) -> Answer:
    """An answer with an interpretive or beyond part (interpret.py).

    The claims come per retrieval part (`shaped_claims`). The writer then
    answers the plan's parts (not the lenses): the evidence grouped by lens, a
    reading for each interpretive part, a counter-case; code adds the limits.
    The answer opens with its short answer (`Answer.short_answer`): one reading
    per interpretive part, or code's line when a part's reading did not stand;
    the rest is the argument. Without prose (no writer, or the prose failed) the
    checked claims are the answer, and the closing lines are added to its text.
    """
    def answer(text: str, *extra_notes: str | None,
               claims: tuple[Claim, ...] = ()) -> Answer:
        notes = "; ".join(n for n in (note, *extra_notes) if n) or None
        return Answer(text=text, passages=evidence.passages, mode=mode,
                      template=template, note=notes, claims=claims)

    if not evidence.passages:
        return answer(REFUSAL)

    gathered = await shaped_claims(question, plan, retrieval, evidence, generate,
                                   book_titles=book_titles, books=books, viewpoint=viewpoint)
    shape, by_part = gathered.shape, gathered.by_part
    kept, cut, prose, skipped, prose_note, unanswered, gaps = await _finish(
        question, list(shape.parts), gathered.kept, evidence, check, write, book_titles,
        part_claims=by_part, books=books, shape=shape, spend_ok=spend_ok)
    missing = [part.question for part, claims in zip(plan, by_part)
               if part.kind != "beyond" and (not claims or all(c in cut for c in claims))]
    if not kept:
        return answer(REFUSAL)
    partial = (PARTIAL_NOTE + "; " + "; ".join(NOT_FOUND + m for m in missing)
               if missing else None)
    if prose:
        short = tuple(s for s in prose if s.short)
        body = tuple(s for s in prose if not s.short)
        return replace(answer(compose_answer(short, body, unanswered, gaps), partial, skipped,
                              claims=tuple(kept)),
                       prose=body, short_answer=short, answer_style="prose",
                       unanswered=unanswered, gaps=gaps)
    text = _compose(kept)
    # The writer was never asked, or its prose did not pass: the facts are the
    # answer, and it still says what the evidence and the books do not settle.
    configured = write_module.PROSE_ANSWERS and write is not None and check is not None
    short = interpret.short_answer(shape) if shape.interpretive and configured else ()
    lines = interpret.limit_text(shape)
    if short:
        text = (SHORT_ANSWER_HEADING + "\n" + compose_text(short)) + "\n\n" + text
    if lines:
        text += "\n\n" + " ".join(lines)
    return replace(answer(text, partial, skipped, prose_note, claims=tuple(kept)),
                   short_answer=short)
