"""Interpretive answers: the answer's shape, the code guards and the closing lines.

A question part is factual (the books state it), interpretive (the books give
evidence but no sentence settles it) or beyond (the books cannot settle it);
decompose.py classifies it and gives an interpretive part its lenses. With
INTERPRETIVE on (lore/query/decompose.py), the agent retrieves for each lens as
for a part of its own, and a question with an interpretive or beyond part is
answered in this shape (spec 2026-10-30):

  1. evidence: sentences of kind "fact", grouped by lens, in story order;
  2. a reading: 1-3 sentences of kind "reading", LORE's position, worded as
     interpretation and resting on facts from at least two lenses;
  3. a counter-case: 1-2 sentences of kind "counter" with at least one fact;
  4. limits: kind "limit", written by code and never by the model, from the
     beyond parts and the checked gaps.

The writer prompt carries the viewpoint (viewpoint.md) verbatim; the check
quotes the lines of it that apply to a reading and to a counter-case.
A reading that fails a guard or the check is dropped, and code then says the
evidence did not support a clear position; the facts stay. This replaces the
READINGS-for-judgement-questions and JUDGEMENTS paths (a judgement is a reading).

Nothing here looks at the wording of a question, and nothing is specific to
any library.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Sequence

from lore.query import decompose
from lore.query.models import ProseSentence

MAX_READINGS = 3
MAX_COUNTERS = 2
# A reading cites facts from at least this many of an interpretive part's lenses
# (fewer when the part had fewer lenses).
MIN_READING_LENSES = 2
MIN_COUNTER_CLAIMS = 1

# Said by code, never by the model.
NO_POSITION_LINE = "The evidence found doesn't support a clear position."
NO_COUNTER_LINE = "The evidence found doesn't support a strong opposing reading."
LIMITS_LEAD = "The books don't settle:"

# The viewpoint rules (the numbered lines of viewpoint.md) that apply to each test.
READING_RULES = (1, 2, 4, 5, 7)
COUNTER_RULES = (3, 4)


def active() -> bool:
    return decompose.INTERPRETIVE


def shaped(kinds: Sequence[str]) -> bool:
    """Whether a plan with these part kinds is answered in the shape above:
    some part is interpretive or beyond."""
    return active() and any(k in ("interpretive", "beyond") for k in kinds)


@dataclass(frozen=True)
class Shape:
    """What the writer, the guards and the check need to know about an
    interpretive answer.

    `parts` and `kinds`: the plan's parts. `retrieval`: per retrieval part
    (the unit claims are gathered for), its question, the index of the plan part
    it serves and whether it is a lens. `claim_parts`: per claim given to the
    writer, the retrieval parts it was found for (filled once the claims are
    known)."""

    viewpoint: str
    parts: tuple[str, ...]
    kinds: tuple[str, ...]
    retrieval: tuple[tuple[str, int, bool], ...]
    claim_parts: tuple[frozenset[int], ...] = ()

    @property
    def interpretive(self) -> bool:
        return "interpretive" in self.kinds

    @property
    def beyond(self) -> tuple[str, ...]:
        return tuple(p for p, k in zip(self.parts, self.kinds) if k == "beyond")

    def with_claims(self, claim_parts: Sequence[frozenset[int]]) -> "Shape":
        return replace(self, claim_parts=tuple(claim_parts))

    def lenses_of(self, part: int) -> list[int]:
        return [i for i, (_, p, lens) in enumerate(self.retrieval) if p == part and lens]

    def need(self, part: int) -> int:
        return min(MIN_READING_LENSES, len(self.lenses_of(part)))

    def groups(self) -> list[tuple[str, list[int]]]:
        """(heading, claim indices 0-based) per retrieval part, in order: a
        claim goes under the first retrieval part that found it. Empty when
        the claims' parts are not known."""
        if not self.claim_parts:
            return []
        out = []
        for ri, (question, _, _) in enumerate(self.retrieval):
            mine = [k for k, found in enumerate(self.claim_parts)
                    if found and min(found) == ri]
            if mine:
                out.append((question, mine))
        return out


def part_labels(shape: Shape) -> list[str]:
    """The writer's list of parts, each with how it is answered."""
    notes = {"interpretive": " (interpretive: its evidence is grouped by aspect below)",
             "beyond": " (beyond the books)"}
    return [f"{i}. {p}{notes.get(k, '')}"
            for i, (p, k) in enumerate(zip(shape.parts, shape.kinds), start=1)]


# --- prompt texts ------------------------------------------------------------

# Claims step, for a retrieval part that is a lens of an interpretive part.
LENS_CONTEXT = ("This part gathers evidence for one side of a larger interpretive question: "
                "{part_of}\n"
                "Answer only this aspect of it. Include what characters do, choose, hide and "
                "change, and how others treat them, not only how they are described. Other "
                "aspects are gathered separately: give the facts and argue no position.\n")
# Claims step, for a part the books may not settle.
BEYOND_CONTEXT = ("This part may ask for something the books cannot settle: {part_of}\n"
                  "Give claims for whatever the passages do say that bears on it. Make no "
                  "claim about what the books leave out, or about anything outside them.\n")

# Writer prompt: the shape, added in place of the readings rule.
WRITER_SHAPE_RULE = """- Part of this question is marked (interpretive): the books give evidence but
  no sentence settles it. Its facts are grouped under "Evidence for:" headings,
  each an aspect of that part. Every sentence has a `kind`: "fact" (the
  default), "reading" or "counter". Write the answer in this order.
  First, sentences of kind "fact": the evidence, in story order, each saying
  only what its cited facts say, as above.
  Then {max_readings} sentences at most of kind "reading": your position on the
  interpretive part. A reading must:
  - take the position the evidence leans towards and say how strongly, in words
    that mark it as interpretation, such as "the evidence suggests", "on
    balance", "a strong case can be made that" or "the evidence is split";
  - list in `claims` the facts it rests on, drawn from at least {min_lenses}
    different "Evidence for:" headings of that part;
  - never add an event, name, number or detail that the facts do not state, and
    never present an inference as something a book says;
  - be a single sentence.
  Then one or two sentences of kind "counter": the strongest case for the other
  side, resting on at least one fact it lists in `claims`, and a fair reading of
  those facts. If the facts give no real case for the other side, write no
  counter; code will say so. If the facts are too thin for any position, write
  no reading; code will say so, and the facts stay.
  Never write a sentence of kind "limit": code writes the closing lines.
"""
WRITER_BEYOND_RULE = """- A part marked (beyond the books) is one the books cannot settle. Say only what
  the facts show about it. Write nothing about what the books leave out, and
  nothing from outside them: code writes that line.
"""
VIEWPOINT_LEAD = "- Follow this viewpoint in everything that goes beyond what the facts state:\n\n"


def writer_rule(shape: Shape) -> str:
    """The writer prompt's rules for an interpretive or beyond part, with the
    viewpoint text included verbatim."""
    out = ""
    if shape.interpretive:
        out += WRITER_SHAPE_RULE.format(max_readings=MAX_READINGS,
                                        min_lenses=MIN_READING_LENSES)
    if shape.beyond:
        out += WRITER_BEYOND_RULE
    return out + VIEWPOINT_LEAD + shape.viewpoint + "\n"


# Check prompt, one block per kind present among the sentences.
READING_CHECK_BLOCK = """
A sentence marked "(reading)" is the answer's own position on a question the
books do not state outright; it is not a direct statement of its cited claims.
Judge it by this test instead of the one above, and give its verdict in the
same sentence_judgements entry. These editorial rules apply to it:
{rules}
"{supported}": a careful reader would agree that the cited claims, taken
together, fairly support the position at the strength it states, and it is
worded as interpretation, and it adds no event, name, number or detail beyond
the claims.
"{not_supported}": the claims do not support that position; or it states more
certainty than the claims give (a "clearly" where they are split); or it is
worded as something a book says; or it adds detail.
"""
COUNTER_CHECK_BLOCK = """
A sentence marked "(counter)" gives the strongest case for the other side of a
question the books do not state outright. Judge it by this test instead of the
one above, and give its verdict in the same sentence_judgements entry. These
editorial rules apply to it:
{rules}
"{supported}": it is a fair reading of the claims it cites, it is worded as
interpretation, and it adds no event, name, number or detail beyond them.
"{not_supported}": the cited claims do not make that case, or it is worded as
something a book says, or it adds detail.
"""


def viewpoint_rules(viewpoint: str) -> dict[int, str]:
    """The numbered rules of a viewpoint text ("3. Give ..."), each with its
    continuation lines joined; {} when the text has none."""
    rules: dict[int, str] = {}
    current = None
    for line in viewpoint.splitlines():
        match = re.match(r"\s*(\d+)\.\s+(\S.*)", line)
        if match:
            current = int(match.group(1))
            rules[current] = match.group(2).strip()
        elif current is not None and line.strip() and line[:1].isspace():
            rules[current] += " " + line.strip()
        else:
            current = None
    return rules


def quoted_rules(viewpoint: str, numbers: Sequence[int]) -> str:
    """The lines of the viewpoint that apply, quoted as they stand. A text with
    no numbered rules is quoted whole: the check never reads less than the writer."""
    rules = viewpoint_rules(viewpoint)
    chosen = [n for n in numbers if n in rules]
    if not chosen:
        return "\n".join(f"  {line}" for line in viewpoint.splitlines() if line.strip())
    return "\n".join(f"  {n}. {rules[n]}" for n in chosen)


def check_blocks(shape: Shape, kinds: set[str], supported: str, not_supported: str) -> str:
    out = ""
    if "reading" in kinds:
        out += READING_CHECK_BLOCK.format(
            rules=quoted_rules(shape.viewpoint, READING_RULES),
            supported=supported, not_supported=not_supported)
    if "counter" in kinds:
        out += COUNTER_CHECK_BLOCK.format(
            rules=quoted_rules(shape.viewpoint, COUNTER_RULES),
            supported=supported, not_supported=not_supported)
    return out


# --- guards ------------------------------------------------------------------

# A reading is worded visibly as interpretation. A short general list of stems,
# as whole words ("shown" is a plain statement, as in "as shown in", so it does
# not count): "suggests", "shows", "seems", "appears", "reflects", "reveals",
# "hints", "indicates", "implies", "arguably", "a strong case can be made",
# "on balance", "stands out", "the evidence is split", "perhaps".
INTERPRETIVE_WORDING = re.compile(
    r"\b(?:suggest|seem|appear|reflect|reveal|hint)(?:s|ed|ing)?\b|\bshow(?:s|ing)?\b"
    r"|\bindicat(?:e|es|ed|ing)\b|\bimpl(?:y|ies|ied|ying)\b"
    r"|\barguabl[ye]\b|\b(?:a|the)\s+(?:\w+\s+)?case\b.{0,30}\b(?:made|argued|for)\b"
    r"|\b(?:can|could|might|may)\s+be\s+(?:made|argued)\b|\bone\s+(?:could|might|can)\s+argue\b"
    r"|\bstands?\s+out\b|\bon\s+balance\b|\bperhaps\b"
    r"|\bevidence\s+(?:is|leans|points)\b|\bi\s+would\s+say\b", re.I)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z“\"'])")


def _single(text: str) -> bool:
    return len([p for p in _SENTENCE_END.split(text.strip()) if p.strip()]) == 1


def reading_sound(sentence, shape: Shape) -> bool:
    """A reading needs interpretive wording, one sentence, and citations from
    enough of one interpretive part's lenses."""
    if not (INTERPRETIVE_WORDING.search(sentence.text) and _single(sentence.text)):
        return False
    if not shape.claim_parts:
        return False
    found = set().union(*(shape.claim_parts[k - 1] for k in sentence.claims
                          if 1 <= k <= len(shape.claim_parts)))
    for part, kind in enumerate(shape.kinds):
        if kind == "interpretive":
            lenses = set(shape.lenses_of(part))
            if lenses and len(found & lenses) >= shape.need(part):
                return True
    return False


def limit_interpretive(sentences: Sequence, shape: Shape) -> list:
    """The sentences in the answer's order (facts, readings, counter-cases) with
    the ones that break a rule removed: a reading that fails `reading_sound`, a
    counter-case citing fewer than MIN_COUNTER_CLAIMS claims, anything past the
    first MAX_READINGS readings or MAX_COUNTERS counter-cases, and any kind the
    writer may not write (code writes "limit"). Facts are never touched. The
    first reading opens a paragraph, and the counter-cases follow it in it."""
    facts, readings, counters = [], [], []
    for s in sentences:
        kind = getattr(s, "kind", "fact")
        if kind == "reading":
            if reading_sound(s, shape) and len(readings) < MAX_READINGS:
                readings.append(s)
        elif kind == "counter":
            if len(set(s.claims)) >= MIN_COUNTER_CLAIMS and len(counters) < MAX_COUNTERS:
                counters.append(s)
        elif kind == "fact":
            facts.append(s)
    # A fact keeps its place in the paragraphs; the interpretation closes them.
    readings = [s.model_copy(update={"new_paragraph": i == 0}) for i, s in enumerate(readings)]
    counters = [s.model_copy(update={"new_paragraph": False}) for s in counters]
    return [*facts, *readings, *counters]


# --- closing lines (code) ----------------------------------------------------

def _limit(text: str, first: bool) -> ProseSentence:
    return ProseSentence(text=text, claims=(), passages=(), new_paragraph=first, kind="limit")


def close_answer(prose: Sequence[ProseSentence], shape: Shape,
                 gaps: Sequence[str] = ()) -> tuple[ProseSentence, ...]:
    """The prose with what code says about it added.

    A counter-case with no reading to oppose is dropped. An interpretive answer
    with no reading says the evidence did not support a clear position; one
    with a reading but no counter-case says it found no strong opposing
    reading. The books' limits (the beyond parts, and the checked gaps) close
    the answer in one line. The facts always stay."""
    readings = any(s.kind == "reading" for s in prose)
    out = [s for s in prose if readings or s.kind != "counter"]
    lines: list[str] = []
    if shape.interpretive:
        if not readings:
            lines.append(NO_POSITION_LINE)
        elif not any(s.kind == "counter" for s in out):
            lines.append(NO_COUNTER_LINE)
    lines += limit_text(shape, gaps)
    first = next((i for i, s in enumerate(out) if s.kind == "reading"), None)
    if first is not None:  # the interpretation opens its own paragraph
        out[first] = replace(out[first], new_paragraph=True)
    out += [_limit(line, i == 0) for i, line in enumerate(lines)]
    return tuple(out)


def limit_text(shape: Shape, gaps: Sequence[str] = ()) -> list[str]:
    """[the books' limits in one line], or []: the beyond parts first, then
    the gaps the check agreed with."""
    items = [*shape.beyond, *(g for g in gaps if g not in shape.beyond)]
    items = [i.strip().rstrip(".!;:,") for i in items if i.strip()]
    if not items:
        return []
    line = f"{LIMITS_LEAD} " + "; ".join(items)
    return [line if line.endswith("?") else line + "."]
