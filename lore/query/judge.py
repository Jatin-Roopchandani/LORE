"""Judgement questions: "who is the most ...", "which is best", "who do you
think ...". They ask for a verdict, not a fact, and nothing in the question
names the people to weigh, so retrieval started from near-random minor
characters and the answer had nothing to compare.

With JUDGEMENTS on, an evaluative part of a question:
  - is recognised by its form alone (`is_evaluative`: superlatives and
    opinion phrases; no word of any book or genre);
  - gets candidates from the graph when it names few characters
    (`candidate_seeds`): the characters whose mentions span the most
    chapters of the part's books, minus whoever the question excludes
    ("except X and Y", `exclusions`, `is_excluded`);
  - gets 2-3 beats per candidate spread over the story
    (timeline.candidate_timeline);
  - is read by the claims step for what each candidate does, wants,
    conceals and changes (synthesize._JUDGEMENT_RULE);
  - may close the written answer with ONE labelled judgement sentence
    (write._JUDGEMENT_RULE), judged by its own test in the check.

Off, every prompt, schema and output is what it was before.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from lore.query.models import SeedEntity
from lore.query.seed import NameIndex, canonical_ids, named_entity_ids, named_seeds
from lore.resolve.normalize import normalize_name

JUDGEMENTS = True
# Candidates gathered for an evaluative part, and the number of named
# characters at or above which the question has already chosen its people.
JUDGE_CANDIDATES = 5
FEW_NAMED = 2
# Rows read before exclusions and book-title nodes are removed.
_OVERFETCH = 40

# --- detection --------------------------------------------------------------

# "most"/"least" followed by one of these is a quantity or a frequency, not
# an opinion ("most of the story", "appears most often", "the most recent").
_QUANTITY = (r"of|often|frequently|commonly|usually|typically|recent|recently|many|much|money|"
             r"time|times|part|parts|people|days|years|chapters|scenes|number|mentions|words|"
             r"pages|books|likely|probably|certainly|everyone|everybody")
_MOST = rf"\b(?:most|least)\b(?!\s+(?:{_QUANTITY})\b)\s+[a-z]"
_BEST = r"\b(?:best|worst)\b"
# "the bravest": a superlative in -est after "the". Position, time and
# ordinary nouns that end in -est are not opinions.
_EST_NOT = frozenset("""earliest latest nearest farthest furthest eldest oldest youngest forest priest
harvest request interest honest modest contest protest suggest invest arrest digest tempest behest
bequest unrest northwest southwest northeast southeast manifest""".split())
_EST = re.compile(r"\bthe\s+([a-z]{3,}est)\b")
_OPINION = re.compile(
    r"\b(?:do|would|did|will|can|could)\s+you\s+(?:think|say|consider|believe|reckon|feel|judge)\b"
    r"|\bin\s+your\s+(?:opinion|view|judg(?:e)?ment|eyes|book)\b"
    r"|\byour\s+(?:favou?rite|pick|choice)\b|\bfavou?rite\b"
    r"|\brank(?:s|ed|ing)?\b|\bdeserv(?:e|es|ed|ing)\b", re.I)
_SUPERLATIVE = re.compile(rf"{_MOST}|{_BEST}", re.I)


def is_evaluative(text: str) -> bool:
    """Whether the question (or part) asks for a judgement: a superlative
    ("the most complex", "the best", "the bravest") or an opinion phrase
    ("who do you think", "in your opinion", "your favourite", "rank")."""
    if not text or not text.strip():
        return False
    if _SUPERLATIVE.search(text) or _OPINION.search(text):
        return True
    return any(m.group(1) not in _EST_NOT for m in _EST.finditer(text.lower()))


# --- exclusions -------------------------------------------------------------

_EXCEPT = re.compile(
    r"\b(?:except(?:\s+for)?|excluding|other\s+than|apart\s+from|aside\s+from|besides|"
    r"not\s+counting|leaving\s+out|without\s+counting|but\s+not)\s+(?P<tail>[^?.;:!]+)", re.I)
# Where the list of names stops and the rest of the question resumes.
_TAIL_END = re.compile(
    r"\s+(?:in|from|among|across|within|for|when|because|as|who|which|that|if|since|during|"
    r"throughout|and\s+(?:why|how|what|who|which))\b", re.I)
_SPLIT = re.compile(r"\s*(?:,|;|&|\band\b|\bor\b|\bnor\b)\s*", re.I)


def exclusions(text: str) -> list[str]:
    """The names a question rules out ("except X and Y", "other than X, Y or
    Z", "excluding X"), in order, without repeats."""
    out: list[str] = []
    for match in _EXCEPT.finditer(text or ""):
        tail = _TAIL_END.split(match.group("tail"), maxsplit=1)[0]
        for piece in _SPLIT.split(tail):
            name = re.sub(r"^(?:the|both|either|neither)\s+", "", piece.strip(), flags=re.I).strip(" \"'")
            if name and re.search(r"\w", name) and name.casefold() not in {o.casefold() for o in out}:
                out.append(name)
    return out


def _tokens(name: str) -> frozenset[str]:
    return frozenset(normalize_name(str(name)).split())


def is_excluded(name: str, aliases: Iterable[str], phrases: Sequence[str]) -> bool:
    """Whether the entity (its name or any alias) is one of the excluded
    `phrases`: equal, or all the words of one inside the other ("Zed" rules
    out "Zed Quill", and "Zed Quill" rules out a node named "Zed")."""
    if not phrases:
        return False
    wanted = [t for t in (_tokens(p) for p in phrases) if t]
    for form in (name, *(aliases or ())):
        have = _tokens(form)
        if have and any(have <= want or want <= have for want in wanted):
            return True
    return False


# --- candidates -------------------------------------------------------------

_BOOKS_CLAUSE = " AND c.book_id IN $books"
_CANDIDATES = (
    "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) WHERE e.kind = 'Character'{books} "
    "WITH e, c.book_id AS book, c.chapter_seq AS chapter, count(c) AS n "
    "RETURN e.id, e.name, e.aliases, count(*) AS chapters, sum(n) AS mentions "
    "ORDER BY chapters DESC, mentions DESC, e.name LIMIT $limit")


async def candidate_seeds(kuzu, book_ids: Sequence[str], phrases: Sequence[str],
                          exclude_names: frozenset[str], *,
                          limit: int = JUDGE_CANDIDATES) -> list[SeedEntity]:
    """The `limit` Character entities that span the most of the story, most
    first: distinct (book, chapter) places whose chunks mention them, then
    the number of mentions. Restricted to `book_ids` when given. Anyone the
    question excludes (`phrases`, matched on name and aliases) and any node
    named like a book title is skipped. A graph that cannot answer gives []:
    this is an extra, never a reason to fail the question."""
    params: dict = {"limit": max(limit, 1) + _OVERFETCH}
    if book_ids:
        params["books"] = list(book_ids)
    try:
        rows = list(kuzu.execute(_CANDIDATES.format(books=_BOOKS_CLAUSE if book_ids else ""), params))
        ids = [str(r[0]) for r in rows
               if normalize_name(str(r[1] or "")) not in exclude_names
               and not is_excluded(str(r[1] or ""), r[2] or (), phrases)]
        seeds = await named_seeds(kuzu, canonical_ids(kuzu, ids), exclude_names=exclude_names)
    except Exception:  # noqa: BLE001 -- see the docstring
        return []
    return seeds[:limit]


@dataclass
class JudgeSeeds:
    """A judgement part's seeds. `seeds`: the named characters first, then the
    candidates, then the part's other seeds; `candidates`: the graph's picks;
    `named`: the question's own (non-excluded) named seeds."""

    seeds: list[SeedEntity]
    candidates: list[SeedEntity] = field(default_factory=list)
    named: list[SeedEntity] = field(default_factory=list)


async def judge_seeds(kuzu, names: NameIndex, part: str, question: str,
                      seeds: Sequence[SeedEntity], named: Sequence[SeedEntity],
                      book_ids: Sequence[str], exclude_names: frozenset[str], *,
                      limit: int = JUDGE_CANDIDATES) -> JudgeSeeds:
    """The seeds of an evaluative part: whoever the question (or the part, if
    it was rewritten) excludes is removed; with fewer than FEW_NAMED named
    characters left, the top candidates of the part's books are added."""
    phrases = list(dict.fromkeys([*exclusions(part), *exclusions(question)]))
    gone: set[str] = set()
    for phrase in phrases:
        gone.update(canonical_ids(kuzu, named_entity_ids(phrase, names)))

    def kept(seed: SeedEntity) -> bool:
        return seed.id not in gone and not is_excluded(seed.name, (), phrases)

    named_left = [s for s in named if kept(s)]
    others = [s for s in seeds if kept(s) and s.id not in {n.id for n in named_left}]
    candidates: list[SeedEntity] = []
    if sum(s.kind == "Character" for s in named_left) < FEW_NAMED:
        candidates = [s for s in await candidate_seeds(kuzu, book_ids, phrases, exclude_names, limit=limit)
                      if s.id not in {n.id for n in named_left}]
    ordered = list(dict.fromkeys(s.id for s in [*named_left, *candidates, *others]))
    by_id = {s.id: s for s in [*others, *candidates, *named_left]}
    return JudgeSeeds(seeds=[by_id[i] for i in ordered], candidates=candidates, named=named_left)
