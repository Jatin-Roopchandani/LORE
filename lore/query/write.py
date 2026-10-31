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
from dataclasses import replace
from typing import Awaitable, Callable, Iterable, Sequence

from pydantic import BaseModel, ConfigDict, create_model

from lore.query import interpret
from lore.query.models import BookInfo, Claim, Passage, ProseSentence
from lore.query.position import order_passages

Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

# On by default (spec 2026-10-12). Off gives today's claim-list answers,
# byte-identical prompts and calls.
PROSE_ANSWERS = True

# STORY_ORDER (2026-10-28): answers read as a narrative. The writer is shown
# the facts in story order with their positions, may use order words the
# positions support, writes in its own words, and names the aspects of the
# question the facts leave unanswered (`gaps`); the check judges meaning and
# accepts order from positions (synthesize._STORY_SENTENCES_BLOCK). Story
# position data itself (lore/query/position.py) has no switch. Off gives the
# earlier prompts, schemas and numbering byte for byte.
STORY_ORDER = True

# READINGS (2026-10-28): a story mostly shows change through actions, which
# no sentence "stated by its cited facts" can interpret. With this on, the
# writer may add up to MAX_READINGS sentences of kind "reading": labelled,
# worded as a reading, resting on at least MIN_READING_CLAIMS facts, judged by
# their own test in the check, guarded by code (`limit_readings`), and shown
# distinctly. It lives in the story writer and check, so it needs STORY_ORDER
# (`readings_on`). Off gives every prompt, schema and output as before, byte
# for byte. An answer with an interpretive part (lore/query/interpret.py,
# INTERPRETIVE) has its own shape and guards in place of this rule; this is
# what every other answer gets.
READINGS = True
MAX_READINGS = 2
MIN_READING_CLAIMS = 2


def readings_on() -> bool:
    return READINGS and STORY_ORDER


# The sentence as the model was asked for it before READINGS (and still is
# when it is off): the schema asked of the model is part of the request, so
# the name stays "Sentence".
_PlainSentence = create_model(
    "Sentence", __module__=__name__,
    text=(str, ...), claims=(list[int], ...), new_paragraph=(bool, False))


class Sentence(_PlainSentence):
    # "fact": stated by the cited facts. "reading": an interpretation of what
    # they show together (see READINGS); "counter": the strongest case for the
    # other side of a reading (interpret.py). Code adds "limit" sentences.
    kind: str = "fact"


class _PlainProse(BaseModel):
    """The writer's reply as it was before STORY_ORDER (and still is when it
    is off): the schema asked of the model is part of the request."""

    sentences: list[_PlainSentence]
    # 1-based numbers of the question's parts the facts do not answer.
    unanswered: list[int] = []


class _StoryProse(_PlainProse):
    # The reply under STORY_ORDER without READINGS: what the schema was at
    # e9471a1, shown to the model as "Prose". No docstring: it would become
    # the schema's description.
    model_config = ConfigDict(title="Prose")

    # Short phrases naming an aspect of the question the facts do not cover,
    # inside a part they do answer. Cleaned by `clean_gaps`, verified by the
    # check, and shown by code in the closing line, as `unanswered` is.
    gaps: list[str] = []


class Prose(_StoryProse):
    # The reply under STORY_ORDER with READINGS: sentences may carry a kind.
    sentences: list[Sentence]

# Rendered by code, never by the model.
UNANSWERED_LEAD = "The passages found don't answer:"


WRITER_PROMPT = """Write the answer to the question from the numbered facts below.

Each fact was checked against the book it cites. Use only what the facts
say; never add anything they do not say, including what you know about the
story. Every sentence lists the numbers of the facts it uses, in `claims`.

- Never put fact numbers, chapter numbers, book titles in brackets, or any
  citation inside the sentence text. Citations go only in `claims`.
- Use every fact, unless it only repeats another fact.
- Every part that has facts must be answered. With several parts, cover
  them in order, and never skip a part's facts to focus on another part.
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
- When a fact's quote is a character speaking or narrating their own story
  (first person, "I" or "my", or quoted speech), the fact is that
  character's own account, not the narrator's: attribute it ("X says ...",
  "according to X ...") and never state it as plain fact.
- Vary how sentences begin. Once a person is named in a paragraph, use he,
  she or they where it is clear who is meant, instead of repeating the name.
- Each item in `sentences` is exactly one sentence; a paragraph is several
  items. Set new_paragraph as the field; never write it in the text.
- Length: a narrow question gets two to four sentences. A broad, abstract
  or multi-part question gets a detailed answer of up to four short
  paragraphs, about 400 words at most. Start a new paragraph for each part
  or theme: mark its first sentence with new_paragraph.
- Do not use connecting words that add a cause, an order or a consequence
  ("because", "so", "as", "while", "later", "the result of", "which led
  to") unless a cited fact states it. When unsure, write two plain
  sentences instead of one joined one.

Question: {question}
Parts:
{parts}
Facts:
{facts}
"""


# The length rule of the story writer prompt, and its replacements for the
# parts of a deep answer (deep.py): a section has no word cap, the overview is
# short. Kept apart so the prompt reads exactly as before when none is asked for.
_LENGTH_RULE = """- Length: a narrow question gets two to four sentences. A broad, abstract
  or multi-part question gets a detailed answer of up to four short
  paragraphs, about 400 words at most. Start a new paragraph for each part
  or theme: mark its first sentence with new_paragraph.
"""
SECTION_LENGTH_RULE = """- Length: this is one section of a long, detailed answer, and its question is the
  section's subject. Use every fact given, and say as much as the facts
  support; there is no word limit. Start a new paragraph for each theme: mark
  its first sentence with new_paragraph.
"""
OVERVIEW_LENGTH_RULE = """- Length: this is the short overview that opens a long answer, and the facts are
  a few of the most important ones from each section that follows. Write two
  to five sentences that say in brief what the answer covers, in the order of
  the facts. Make no claim the facts do not support.
"""
LENGTH_RULES = {None: _LENGTH_RULE, "section": SECTION_LENGTH_RULE,
                "overview": OVERVIEW_LENGTH_RULE}


STORY_WRITER_PROMPT = """Write the answer to the question from the numbered facts below.

Each fact was checked against the book it cites. The facts are listed in
story order: books whose order is known come in that order, then each
book's facts by chapter and place in the chapter. A fact's label shows where
it comes from. {order_note}
You may only say what the facts say. Never add a name, number, detail,
reason or event they do not state, including what you know about the story.
Every sentence lists the numbers of the facts it uses, in `claims`.

- Write in your own words. Paraphrase, combine and summarise freely, as long
  as nothing is added that the facts do not say. Do not copy a fact's
  wording when plainer words say the same.
- Never put fact numbers, chapter numbers, book titles in brackets, or any
  citation inside the sentence text. Citations go only in `claims`.
- Use every fact, unless it only repeats another fact.
- Every part that has facts must be answered. With several parts, cover
  them in order, and never skip a part's facts to focus on another part.
- For a broad question, you may open with one sentence that answers the
  whole question in brief. It lists the numbers of every fact it
  summarises, and the sentences after it give the detail. Never say more
  than the facts do: no "always" or "every" unless the facts say so.
- When the question is about change, sequence, development, cause over time
  or connections across books, tell the answer as a narrative in story
  order, with order words ("at first", "earlier", "later", "by the end",
  "in the next book"). Otherwise reply directly, in natural prose.
- An order word is allowed only where the labels show that order: the
  later fact comes later in the list, in a later chapter or a later book.
  Never use an order word between facts from books whose order is unknown.
- Contrast and addition words ("also", "however", "although", "in
  contrast") add no fact and are fine. Say "meanwhile" or "at the same
  time" only when the facts themselves show the events were simultaneous.
- A word that states a cause or a consequence ("because", "so", "which led
  to", "as a result", "in order to") still needs a fact that states that
  link. When no fact does, state the two things separately.
- If the facts do not answer a whole part of the question, put that part's
  number in `unanswered` and write no sentence for it.
- If the facts do not cover one aspect that the question asks about, inside
  a part they do answer, write no sentence for it. Name it in `gaps`
  instead: at most two short phrases, each under 15 words, with no numbers.
  Never name a gap that a fact answers.
- Do not state the same fact twice.
- Name a book, in plain words ("In <book title>, ..."), only when the
  facts come from more than one book.
- Keep each fact's certainty: if a fact says someone seemed, was suspected
  or claimed, say so too.
- When a fact's quote is a character speaking or narrating their own story
  (first person, "I" or "my", or quoted speech), the fact is that
  character's own account, not the narrator's: attribute it ("X says ...",
  "according to X ...") and never state it as plain fact.
- Vary how sentences begin. Once a person is named in a paragraph, use he,
  she or they where it is clear who is meant, instead of repeating the name.
- Each item in `sentences` is exactly one sentence; a paragraph is several
  items. Set new_paragraph as the field; never write it in the text.
{length}{readings}
Question: {question}
Parts:
{parts}
Facts:
{facts}
"""

# The readings rule, added to the story writer prompt only when READINGS is on.
_READINGS_RULE = """- Each sentence has a `kind`: "fact" (the default) or "reading". A fact says
  only what its cited facts say, as above. When the question is about a
  character, a relationship, change or development, motives or themes, you
  may add up to {max_readings} sentences of kind "reading". A reading
  interprets what the cited facts show taken together: what actions, choices
  and behaviour reveal, not only what is said outright. A reading must:
  - be worded visibly as a reading, with a word such as "suggests", "shows",
    "seems", "appears", "reflects" or "indicates";
  - list at least {min_claims} facts it rests on, in `claims`;
  - never introduce an event, name, number or detail that the facts do not
    contain;
  - never state the interpretation as plain fact.
  A reading is a single sentence. Every other sentence is a fact.
""".format(max_readings=MAX_READINGS, min_claims=MIN_READING_CLAIMS)


def _title(book_id: str, book_titles: dict[str, str] | None) -> str:
    return (book_titles or {}).get(book_id) or book_id


def book_infos(book_titles: dict[str, str] | None,
               books: dict[str, BookInfo] | None) -> dict[str, BookInfo]:
    """Every known book: the rows where there are any, a title-only entry
    (no story position) for a book known only by title."""
    merged = {k: BookInfo(title=v) for k, v in (book_titles or {}).items()}
    merged.update(books or {})
    return merged


def story_plan(claims: Sequence[Claim], passages: Iterable[Passage],
               book_titles: dict[str, str] | None, books: dict[str, BookInfo] | None,
               groups: Sequence[Sequence[int]] | None = None):
    """(claims' indices in story order, each claim's position or None, the
    unknown-order line). Claims are ordered by their passage's position; one
    whose passage is not among `passages` goes last, in its given order.

    With `groups` (lists of claim indices, 0-based) the claims are listed group
    by group, each in story order: a claim goes in the first group that has it,
    and any in no group come last."""
    by_number = {p.n: p for p in passages}
    cited = [by_number[n] for n in dict.fromkeys(c.passage for c in claims) if n in by_number]
    order = order_passages(cited, book_infos(book_titles, books))
    rank = {pos.n: i for i, pos in enumerate(order.ordered)}
    indices = sorted(range(len(claims)), key=lambda i: (rank.get(claims[i].passage, len(rank)), i))
    if groups:
        placed: list[int] = []
        for group in groups:
            members = set(group)
            placed += [i for i in indices if i in members and i not in placed]
        indices = placed + [i for i in indices if i not in placed]
    return indices, [order.positions.get(c.passage) for c in claims], order.note()


def part_texts(question: str, sub_questions: Sequence[str]) -> list[str]:
    """The question's parts; a question with none listed is its own part 1."""
    return list(sub_questions) or [question]


def writer_prompt(question: str, sub_questions: Sequence[str], claims: Sequence[Claim],
                  passages: Iterable[Passage], book_titles: dict[str, str] | None,
                  books: dict[str, BookInfo] | None = None,
                  shape: "interpret.Shape | None" = None,
                  length: str | None = None) -> str:
    """`shape`: the interpretive answer's shape (interpret.py), only with
    STORY_ORDER. The facts are then listed under an "Evidence for:" heading per
    lens, and the shape's rule and the viewpoint replace the readings rule.
    `length` ("section" or "overview", only with STORY_ORDER): the part of a
    deep answer this writes, which sizes its prose (LENGTH_RULES)."""
    passages = list(passages)
    parts = "\n".join(f"{i}. {s}" for i, s in enumerate(part_texts(question, sub_questions), start=1))
    if STORY_ORDER:
        groups = shape.groups() if shape is not None else []
        indices, positions, note = story_plan(claims, passages, book_titles, books,
                                              [members for _, members in groups] or None)
        heading = {k: question_of for question_of, members in reversed(groups) for k in members}
        lines, last = [], None
        for shown, i in enumerate(indices, start=1):
            if heading and heading.get(i) != last and i in heading:
                last = heading[i]
                lines.append(f"Evidence for: {last}")
            where = f" ({positions[i].label()})" if positions[i] else ""
            lines.append(f"{shown}. {claims[i].text.strip()}{where}\n   quote: {claims[i].quote.strip()}")
        if shape is not None:
            parts = "\n".join(interpret.part_labels(shape))
            rules = interpret.writer_rule(shape)
        else:  # a deep answer's section or overview states facts: readings are the close's
            rules = _READINGS_RULE if readings_on() and length is None else ""
        return STORY_WRITER_PROMPT.format(question=question, parts=parts, facts="\n".join(lines),
                                          order_note=note, readings=rules,
                                          length=LENGTH_RULES[length])
    by_number = {p.n: p for p in passages}
    lines = []
    for number, claim in enumerate(claims, start=1):
        p = by_number.get(claim.passage)
        where = f" ({_title(p.book_id, book_titles)}, chapter {p.chapter_seq})" if p else ""
        lines.append(f"{number}. {claim.text.strip()}{where}\n   quote: {claim.quote.strip()}")
    return WRITER_PROMPT.format(question=question, parts=parts, facts="\n".join(lines))


def _kind(sentence, shape: "interpret.Shape | None" = None) -> str:
    """"reading" (a stray "judgement" is one: it folded into reading), or
    "counter" when the answer has an interpretive part, only when the writer
    said so; anything else is a fact. "limit" is never the writer's."""
    kind = str(getattr(sentence, "kind", "")).strip().lower()
    if kind in ("reading", "judgement"):
        return "reading"
    return "counter" if kind == "counter" and shape is not None and shape.interpretive else "fact"


async def write_answer(question: str, sub_questions: Sequence[str], claims: Sequence[Claim],
                       passages: Iterable[Passage], generate: Generate, *,
                       book_titles: dict[str, str] | None = None,
                       books: dict[str, BookInfo] | None = None,
                       shape: "interpret.Shape | None" = None,
                       length: str | None = None) -> Prose | None:
    """The writer's prose, or None on any failure: a failed extra step must
    never cost the user the checked claims (they are the fallback).

    With STORY_ORDER the facts reach the writer in story order, so the
    numbers it cites are positions in that list; they are converted back to
    the claims' own numbers here, before anything else sees them. A number
    outside the list becomes 0, which `verify_prose` drops with its
    sentence, as it drops any out-of-range citation."""
    passages = list(passages)
    # A deep answer's section or overview (`length`) states facts only: no readings
    # rule, no kinds in the reply, and a stray reading is dropped, not relabelled.
    part = length is not None
    kinds = (readings_on() and not part) or shape is not None
    try:
        reply = await generate(
            writer_prompt(question, sub_questions, claims, passages, book_titles, books, shape,
                          length),
            (Prose if kinds else _StoryProse) if STORY_ORDER else _PlainProse)
    except Exception:  # noqa: BLE001 -- see the docstring
        return None
    if not (isinstance(reply, _PlainProse) and reply.sentences):
        return None
    back = None
    if STORY_ORDER:
        groups = shape.groups() if shape is not None else []
        indices = story_plan(claims, passages, book_titles, books,
                             [members for _, members in groups] or None)[0]
        back = {shown: i + 1 for shown, i in enumerate(indices, start=1)}
    # Every sentence leaves here as a `Sentence` with a kind; a reply asked
    # for without readings (or a model that gave none) is all facts.
    sentences = [
        Sentence(text=s.text, new_paragraph=s.new_paragraph,
                 claims=[back.get(k, 0) for k in s.claims] if back else list(s.claims),
                 kind=_kind(s, shape) if kinds else "fact")
        for s in reply.sentences
        if not (part and str(getattr(s, "kind", "fact")).strip().lower() != "fact")]
    if not sentences:
        return None
    return Prose(sentences=sentences, unanswered=reply.unanswered, gaps=getattr(reply, "gaps", []))


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


_FLAG_IN_TEXT = re.compile(r"^\s*new_paragraph\s*:\s*", re.I)
# A sentence ends at . ! ? before a capital or an opening quote -- unless the
# word before the stop is a title or abbreviation ("Mr.", "No.", "St.").
_ABBREV = r"(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bSt\.)(?<!\bNo\.)(?<!\bCol\.)(?<!\bCapt\.)(?<!\bMaj\.)(?<!\bGen\.)(?<!\bJr\.)(?<!\bSr\.)(?<!\bvs\.)"
_SENTENCE_END = re.compile(rf"{_ABBREV}(?<=[.!?])\s+(?=[A-Z\u201c\"'])")


_STOP = frozenset("""a an the and or but of to in on at by for with from as is was were be been
his her their its he she they it him them that this which who whom what not no
had has have did do does would could should said says so than then there""".split())


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w not in _STOP}


def _claims_used(piece: str, cited: list[int], claim_texts: Sequence[str]) -> list[int]:
    """The cited claims a split-off sentence actually draws on: those sharing
    at least half as many content words with it as the best match. Sharing
    none with any keeps them all -- the check then judges it against all."""
    words = _content_words(piece)
    scores = {k: len(words & _content_words(claim_texts[k - 1])) for k in cited}
    best = max(scores.values(), default=0)
    return [k for k in cited if scores[k] * 2 >= best and scores[k] > 0] if best else list(cited)


def split_items(prose: Prose, claim_texts: Sequence[str] | None = None) -> list[Sentence]:
    """One sentence per item. Measured 2026-10-28: asked for long answers,
    the writer packed a whole paragraph into one item, so one unsupported
    clause sank the paragraph. Each piece cites the item's claims, so the
    check judges it against exactly the facts the item rested on. A
    `new_paragraph:` the writer typed into the text becomes the flag."""
    out = []
    for item in prose.sentences:
        text, flag = item.text, item.new_paragraph
        if _FLAG_IN_TEXT.match(text):
            text, flag = _FLAG_IN_TEXT.sub("", text), True
        pieces = [p.strip() for p in _SENTENCE_END.split(text.strip()) if p.strip()]
        if (readings_on() or interpret.active()) and getattr(item, "kind", "fact") == "reading":
            # Never split a reading: its pieces would lose the hedge and the
            # citations that make it one. `limit_readings` drops one that is
            # more than a single sentence.
            out.append(item.model_copy(update={"text": text.strip(), "new_paragraph": flag}))
            continue
        narrow = claim_texts is not None and len(pieces) > 1 and all(
            1 <= k <= len(claim_texts) for k in item.claims)
        for i, piece in enumerate(pieces):
            claims = _claims_used(piece, item.claims, claim_texts) if narrow else item.claims
            out.append(item.model_copy(update={"text": piece, "claims": claims,
                                               "new_paragraph": flag and i == 0}))
    return out


def verify_prose(prose: Prose, n_claims: int,
                 claim_texts: Sequence[str] | None = None,
                 shape: "interpret.Shape | None" = None) -> list[Sentence]:
    """Sentences (one per item, see split_items) that cite at least one
    claim, all within 1..n_claims, with stray citation residue stripped from
    the text (a sentence left with no words is dropped). With the shape of an
    interpretive answer, its guards apply (interpret.limit_interpretive)."""
    out = []
    for s in split_items(prose, claim_texts):
        if not (s.claims and all(1 <= k <= n_claims for k in s.claims)):
            continue
        text = strip_residue(s.text)
        if re.search(r"\w", text):
            out.append(s.model_copy(update={"text": text}))
    if shape is not None and shape.interpretive:
        return interpret.limit_interpretive(out, shape)
    return limit_readings(out) if readings_on() else out


# A reading must be worded visibly as one. A small, general list of stems:
# "suggests", "shows", "seems", "appears", "reflects", "indicates", "implies",
# "reveals", "hints" -- as whole words, so "showroom" or "seemly" do not count.
_HEDGE = re.compile(r"\b(?:suggest|show|seem|appear|reflect|reveal|hint)(?:s|ed|ing|n)?\b"
                    r"|\bindicat(?:e|es|ed|ing)\b|\bimpl(?:y|ies|ied|ying)\b", re.I)


def limit_readings(sentences: Sequence[Sentence]) -> list[Sentence]:
    """The sentences with the readings that break a rule removed: one with no
    hedge word, one citing fewer than MIN_READING_CLAIMS distinct claims, one
    that is more than a single sentence, and any past the first MAX_READINGS.
    Facts are never touched. (An interpretive answer has its own guards:
    interpret.limit_interpretive.)"""
    out, kept = [], 0
    for s in sentences:
        if getattr(s, "kind", "fact") == "reading":
            single = len([p for p in _SENTENCE_END.split(s.text.strip()) if p.strip()]) == 1
            sound = (bool(_HEDGE.search(s.text))
                     and len(set(s.claims)) >= MIN_READING_CLAIMS and single)
            if not sound or kept >= MAX_READINGS:
                continue
            kept += 1
        out.append(s)
    return out


MAX_GAPS = 2
MAX_GAP_WORDS = 15


def clean_gaps(gaps: Iterable[str]) -> list[str]:
    """The writer's gaps as the answer may carry them: citations stripped,
    cut to MAX_GAP_WORDS words, no trailing punctuation, no empty or repeated
    phrase, at most MAX_GAPS of them."""
    out: list[str] = []
    for gap in gaps:
        words = strip_residue(str(gap)).split()
        text = " ".join(words[:MAX_GAP_WORDS]).strip().rstrip(".;:,!?").strip()
        if re.search(r"\w", text) and text.casefold() not in {g.casefold() for g in out}:
            out.append(text)
    return out[:MAX_GAPS]


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

    `written`: how many sentences the writer produced (split_items), before
    verify_prose.
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
    # One unsupported sentence is removed; more falls back -- except that a
    # long answer may lose one in five (amended 2026-10-28 with the longer
    # answers: one weak clause in ten sentences should not cost the prose).
    # Only facts count: a reading or counter-case is an optional extra, and one
    # the check rejects is simply dropped (2026-10-29).
    facts = [ok for s, ok in standing if getattr(s, "kind", "fact") == "fact"]
    if sum(not ok for ok in facts) > max(1, len(facts) // 5):
        return None
    kept = [s for s, ok in standing if ok]
    if not kept or 2 * len(kept) < written:
        return None
    if all(getattr(s, "kind", "fact") != "fact" for s in kept):
        return None  # prose of interpretation alone: the facts are what it rests on
    return tuple(
        ProseSentence(text=s.text.strip(),
                      claims=tuple(survivors_index[k] for k in s.claims),
                      passages=tuple(sorted({passage_of[k - 1] for k in s.claims})),
                      new_paragraph=s.new_paragraph and i > 0, kind=getattr(s, "kind", "fact"))
        for i, s in enumerate(kept))


def coverage_gaps(prose: Sequence[ProseSentence], part_claims: Sequence[Sequence[int]]) -> list[int]:
    """0-based parts that have surviving claims but whose claims no prose
    sentence cites. `part_claims`: per part, its claims' survivor numbers
    (1-based, as in `ProseSentence.claims`)."""
    cited = {k for s in prose for k in s.claims}
    return [i for i, ks in enumerate(part_claims) if ks and not cited.intersection(ks)]


def paragraphs_by_book(prose: Sequence[ProseSentence],
                       book_of: dict[int, str]) -> tuple[ProseSentence, ...]:
    """In an answer drawn from more than one book, paragraphs follow the
    books: a sentence whose passages are all in a different book from the
    previous sentence's opens a paragraph, and no other does. Measured
    2026-10-28: the writer's own new_paragraph landed mid-book. A one-book
    answer keeps the writer's paragraphs."""
    books = [frozenset(book_of.get(n, "") for n in s.passages) for s in prose]
    if len(set().union(*books)) < 2:
        return tuple(prose)
    return tuple(replace(s, new_paragraph=i > 0 and not (books[i] & books[i - 1]))
                 for i, s in enumerate(prose))


def compose_text(prose: Sequence[ProseSentence], unanswered: Sequence[str] = (),
                 gaps: Sequence[str] = ()) -> str:
    """The prose with its citations, then one closing line naming what the
    passages do not answer: whole parts first, then gaps within a part. A
    reading is just its sentence: its own wording ("suggests", "seems") is
    what marks it in plain text."""
    paragraphs: list[list[str]] = [[]]
    for s in prose:
        if s.new_paragraph and paragraphs[-1]:
            paragraphs.append([])
        # A sentence code wrote (kind "limit") cites nothing.
        cites = "".join(f"[{n}]" for n in s.passages)
        paragraphs[-1].append(s.text + (" " + cites if cites or s.kind != "limit" else ""))
    text = "\n\n".join(" ".join(p) for p in paragraphs)
    closing = [*unanswered, *(g for g in gaps if g not in unanswered)]
    if closing:
        text += f"\n\n{UNANSWERED_LEAD} " + "; ".join(closing)
    return text


CONTENTS_HEADING = "Contents"


def compose_sections(overview: Sequence[ProseSentence], sections: Sequence) -> str:
    """A deep answer as text: the overview, a numbered contents list, then each
    section under a markdown heading (`## heading`). A section is anything with
    `heading` and `text` (models.AnswerSection); the text of each is its own
    prose with its citations, or its claim list when the prose did not pass."""
    blocks = [compose_text(overview)] if overview else []
    if len(sections) > 1:
        blocks.append(CONTENTS_HEADING + "\n" + "\n".join(
            f"{i}. {s.heading}" for i, s in enumerate(sections, start=1)))
    blocks += [f"## {s.heading}\n\n{s.text}" for s in sections]
    return "\n\n".join(blocks)
