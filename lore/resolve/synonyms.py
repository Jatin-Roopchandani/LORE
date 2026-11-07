"""Synonyms the text never states as "also called": candidate pairs, asked.

Names for one thing that share no word ("Myrddraal", "Fade", "Halfman", "the
Eyeless"; "Shai'tan", "the Dark One", "the Father of Lies") are invisible to
the resolver's passes, which join names by spelling, nesting or a short
context sentence. This module finds CANDIDATE pairs among one book's resolved
entities and turns each into a question for the existing disambiguation path
(lore/resolve/disambiguation.py: recorded on load, asked in one batched
request by `lore disambiguate` / the disambiguate stage, every answer cached).
Nothing is merged on a signal alone: a pair is merged only when the answer
is "same"; "claimed" (a character believes or says one is the other) becomes
an IDENTIFIED_AS edge, never a merge; anything else keeps them apart.

Signals, all from what a load already has, for any book:

- **cues in the text** (`text_cues`): a glossary cross-reference ("Halfman:
  See Myrddraal."), a glossary entry naming its other names ("Myrddraal
  (MUHRD-draal): ... known by many names, among them Halfmen, the Eyeless
  ..."), a narrated naming ("The Father of Lies is a good name for the Dark
  One", "... the name depends on the land, but they all mean Myrddraal"),
  and the weak appositive "A, the B,";
- **stated by the extraction** (`stated_pairs`): an extracted edge whose
  predicate says identity (SAME_AS, ALIAS_OF, KNOWN_AS ...), or IS_A, weaker;
- **context** (`Contexts`): each entity's passages, as the mean of their
  chunk embeddings (the local vectors extraction already made, no new embed
  call), less the book's mean so the shared genre does not make everything
  similar, compared on the passages each has WITHOUT the other;
- **distribution** (`feature_vectors`): the predicates and neighbours an
  entity has (both "serve" one master, both "hunt" the same people), tf-idf
  weighted.

Similarity alone (the mean of the two) proposes a pair only among entities
with a few edges, when the two are each other's near neighbours (mutual
top-k) and neither is listed with the other as two things ("Trollocs and
Myrddraal") nor acts on it (`acts_on`). Measured on a 1,235-passage novel
with a glossary, the text cues found every labelled synonym and similarity
added typos and forms of address ("Fal Data"/"Fal Dara", "My Lord Captain");
alone, without cues, similarity still found Myrddraal/Fade and three other
labelled pairs in its top 60, at about one true pair in six. That is why
nothing merges without an answer.

Candidates are ranked (glossary cross-references, glossary entries, stated
edges, narrated namings, then appositives, IS_A and similarity) and capped
per book (`MAX_CANDIDATES`), so a book costs a handful of batched requests.
Names the resolver already rules on (one inside the other, two of a kind,
a bare title) are never candidates, and kinds must be compatible.

Pure functions; nothing here knows a title.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

import numpy as np  # installed with lancedb

from lore.resolve.normalize import HONORIFICS, normalize_name

# Candidate pairs asked per book: three requests of 20.
MAX_CANDIDATES = 60
# Similarity-only candidates any one entity may take part in, so one hub
# (the protagonist, near everything) cannot fill the list.
MAX_PER_ENTITY = 2
# An entity needs this many edges before its context or neighbours say
# anything; a name seen once or twice has no distribution to compare.
MIN_EDGES = 3
# Mutual top-k on the combined similarity.
TOP_K = 3
# Combined similarity a similarity-only pair must reach.
SIMILARITY_FLOOR = 0.35
# Evidence sentences shown per side of a question.
SNIPPETS = 3
SNIPPET_CHARS = 280
# Questions per request (each carries ~7 evidence sentences).
SYNONYM_BATCH = 20

# Kinds that can name one thing: the extractor calls a creature type a
# Faction in one sentence and a Character in the next, and anything a Concept.
_COMPATIBLE = frozenset({
    frozenset({"Character", "Faction"}),
    frozenset({"Character", "Concept"}),
    frozenset({"Faction", "Concept"}),
    frozenset({"Object", "Concept"}),
    frozenset({"Event", "Concept"}),
})


def compatible(kind_a: str, kind_b: str) -> bool:
    return kind_a == kind_b or frozenset({kind_a, kind_b}) in _COMPATIBLE


@dataclass(frozen=True)
class Entity:
    """One resolved entity of the book."""

    id: str
    name: str
    kind: str
    names: tuple[str, ...] = ()   # surface forms and aliases it answers to


@dataclass(frozen=True)
class Edge:
    """One extracted edge between two resolved entities."""

    subject: str
    predicate: str
    object: str
    evidence: str
    chunk_id: str


@dataclass(frozen=True)
class Cue:
    """Text or extraction evidence that `a` and `b` may name one thing."""

    a: str
    b: str
    shape: str        # see | entry | naming | stated | appositive | is_a
    strength: float
    sentence: str
    chunk_id: str


@dataclass(frozen=True)
class Pair:
    a: str
    b: str
    score: float
    cue: Cue | None = None
    context: float = 0.0
    distribution: float = 0.0


# --------------------------------------------------------------------------
# cues in the text

_SENTENCE = re.compile(r"[^.!?]*[.!?]+[\"”’]?|[^.!?]+$")
# A naming word: what follows it is another name for something before it
# ("a good name for", "known as", "they all mean"), never a bare "called"
# ("Rand called to Mat").
_NAMING = re.compile(
    r"\b(?:also\s+)?known\s+(?:as|by)\b|\bnames?\s+(?:for|by\s+which|among\s+them)\b|"
    r"\bnames?,\s+among\s+them\b|\beuphemisms?\b(?:[^.!?]{0,40}?\bamong\s+them\b)?|"
    r"\b(?:all|both)\s+mean\b|\b(?:proper|true|real|other)\s+names?\s+(?:is|was|are)\b|"
    r"\brefers?\s+to\b|\brefer\s+to\b|"
    r"\b(?:is|are|was|were|be|been|also|once|sometimes|often|they|men|some|folk|people)"
    r"\s+called\b|\bcall\s+(?:them|it|him|her)\b|\banother\s+(?:name|word)\s+for\b",
    re.IGNORECASE)
# Between two names of one list after a naming word ("among them Halfmen, the
# Eyeless, Shadowmen, Lurk, and Fade"; "the Tinkers and as the Traveling People").
_LIST_GAP = re.compile(r"\s*(?:,|;)?\s*(?:and\s+|or\s+)?(?:as\s+)?(?:the\s+)?", re.IGNORECASE)
_NAMES_BEFORE = 4
_SEE = re.compile(r"^\s*(?:,\s*[a-z][a-z ]*)?\s*(?:\([^)]{0,60}\))?\s*:\s*see\s+(?:also\s+)?",
                  re.IGNORECASE)
_ENTRY_HEAD = re.compile(r"^\s*(?:,\s*[a-z][a-z ]*)?\s*(?:\([^)]{0,60}\))?\s*:\s+")
_APPOSITIVE = re.compile(r"\s*,\s*the\s+")
_LISTED = re.compile(r"\s+(?:and|or|nor)\s+(?:the\s+|a\s+|an\s+)?", re.IGNORECASE)
_LISTED_LAST = re.compile(r"\s*,\s*(?:and|or|nor)\s+(?:the\s+|a\s+|an\s+)?", re.IGNORECASE)
_LISTED_COMMA = re.compile(r"\s*,\s*(?:the\s+)?", re.IGNORECASE)
_MAX_NAMES_PER_SENTENCE = 6

_STATED = re.compile(
    r"(?:^|_)(?:SAME_AS|IS_SAME_AS|ALIAS|ALIAS_OF|ALIASES|KNOWN_AS|IS_KNOWN_AS|"
    r"ALSO_KNOWN_AS|IS_ALSO_KNOWN_AS|AKA|CALLED|IS_CALLED|ALSO_CALLED|NAMED|"
    r"IDENTIFIED_AS|IDENTICAL_TO|ANOTHER_NAME_FOR|NAME_FOR|EQUIVALENT_TO)(?:$|_)")
_IS_A = re.compile(r"^(?:IS_A|IS_AN|IS)$")


def _predicate_key(predicate: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", predicate.upper()).strip("_")


def is_identity_predicate(predicate: str) -> bool:
    return bool(_STATED.search(_predicate_key(predicate)))


def _name_index(entities: Iterable[Entity]) -> tuple[re.Pattern | None, dict[str, str]]:
    """A pattern over every name and the entity each normalized name is."""
    owner: dict[str, str] = {}
    forms: set[str] = set()
    for entity in sorted(entities, key=lambda e: e.id):
        for raw in (entity.name, *entity.names):
            normalized = normalize_name(raw)
            if not normalized or len(normalized) < 3:
                continue
            if all(t in HONORIFICS for t in normalized.split()):
                continue  # a bare title names a role, not one thing
            owner.setdefault(normalized, entity.id)
            stripped = re.sub(r"^(?:the|a|an)\s+", "", raw.strip(), flags=re.IGNORECASE)
            if len(stripped) >= 3:
                forms.add(stripped)
    if not forms:
        return None, owner
    alternation = "|".join(re.escape(f) for f in sorted(forms, key=lambda f: (-len(f), f)))
    return re.compile(rf"(?<![\w'’])(?:(?:the|a|an)\s+)?(?P<n>{alternation})(?![\w'’])",
                      re.IGNORECASE), owner


def _hits(pattern: re.Pattern, owner: Mapping[str, str], text: str):
    """(match, entity id) for each name in `text`, in order."""
    out = []
    for m in pattern.finditer(text):
        eid = owner.get(normalize_name(m.group("n")))
        if eid is not None:
            out.append((m, eid))
    return out


def _list_after(pattern: re.Pattern, owner: Mapping[str, str], sentence: str,
                start: int) -> list[str]:
    """The names listed right after `start`: the first within a few words,
    then each one separated from the last only by a list gap."""
    out: list[str] = []
    position = start
    for m, eid in _hits(pattern, owner, sentence[start:]):
        gap = sentence[position:start + m.start()]
        if not out and (len(gap) > 12 or re.search(r"[,.;:!?]", gap)):
            break
        if out and not _LIST_GAP.fullmatch(gap):
            break
        out.append(eid)
        position = start + m.end()
        if len(out) >= _MAX_NAMES_PER_SENTENCE:
            break
    return out


def text_cues(chunks: Iterable[tuple[str, str]], entities: Iterable[Entity]) -> list[Cue]:
    """Cues in the book's text (glossary included) that two entities are one.

    Shapes, strongest first: `see` ("Halfman: See Myrddraal."), `entry` (a
    glossary entry "Head (pron): ..." whose sentence with a naming word lists
    other names: each is a cue with the head), `naming` (a sentence with a
    naming word between two names: "the Father of Lies is a good name for the
    Dark One"), `appositive` ("A, the B," with nothing else between)."""
    entities = list(entities)
    pattern, owner = _name_index(entities)
    if pattern is None:
        return []
    found: dict[tuple, Cue] = {}

    def add(a: str, b: str, shape: str, strength: float, sentence: str, chunk_id: str):
        if a == b:
            return
        key = (*sorted((a, b)), shape)
        cue = Cue(*sorted((a, b)), shape, strength, sentence.strip()[:SNIPPET_CHARS], chunk_id)
        if key not in found or (cue.chunk_id, cue.sentence) < (found[key].chunk_id,
                                                                 found[key].sentence):
            found[key] = cue

    for chunk_id, text in sorted(chunks):
        text = text.replace("\n", " ")
        hits = _hits(pattern, owner, text)
        # glossary: "Head (pron): See Other." and "Head (pron): ... names ..."
        heads = []
        for m, eid in hits:
            before = text[:m.start()].rstrip()
            if before and before[-1] not in ".!?:”\"":
                continue
            rest = text[m.end():]
            see = _SEE.match(rest)
            if see:
                for m2, eid2 in _hits(pattern, owner, rest[see.end():see.end() + 80])[:1]:
                    if rest[see.end():see.end() + m2.start()].strip() == "":
                        add(eid, eid2, "see", 1.0, text[m.start():m.end() + see.end()
                                                       + m2.end()], chunk_id)
                continue
            if _ENTRY_HEAD.match(rest):
                heads.append((m.start(), eid))
        entry_sentences: set[str] = set()
        for index, (start, head) in enumerate(heads):
            end = heads[index + 1][0] if index + 1 < len(heads) else len(text)
            for sentence in _SENTENCE.findall(text[start:end]):
                for naming in _NAMING.finditer(sentence):
                    for eid in _list_after(pattern, owner, sentence, naming.end()):
                        add(head, eid, "entry", 0.9, sentence, chunk_id)
                        entry_sentences.add(sentence.strip())
        # narration: names before a naming word, and the list right after it
        for sentence in _SENTENCE.findall(text):
            if sentence.strip() in entry_sentences:
                continue
            named = _hits(pattern, owner, sentence)
            if len({eid for _m, eid in named}) < 2:
                continue
            for naming in _NAMING.finditer(sentence):
                before = [eid for m, eid in named if m.end() <= naming.start()][-_NAMES_BEFORE:]
                for b in _list_after(pattern, owner, sentence, naming.end()):
                    for a in before:
                        add(a, b, "naming", 0.7, sentence, chunk_id)
            for (m1, a), (m2, b) in zip(named, named[1:]):
                gap = sentence[m1.end():m2.start()]
                if (_APPOSITIVE.fullmatch(gap + m2.group(0)[:len(m2.group(0)) - len(m2.group("n"))])
                        and re.match(r"\s*[,.;:!?”\"]", sentence[m2.end():])
                        and not re.match(r"\s*,\s*(?:and|or)\b", sentence[m2.end():])):
                    add(a, b, "appositive", 0.25, sentence, chunk_id)
    return sorted(found.values(), key=lambda c: (c.a, c.b, c.shape))


def stated_pairs(edges: Iterable[Edge]) -> list[Cue]:
    """Edges whose predicate itself says the two are one (or IS_A, weaker)."""
    found: dict[tuple, Cue] = {}
    for edge in sorted(edges, key=lambda e: (e.chunk_id, e.subject, e.object, e.evidence)):
        if edge.subject == edge.object:
            continue
        key_pred = _predicate_key(edge.predicate)
        if _STATED.search(key_pred):
            shape, strength = "stated", 0.8
        elif _IS_A.match(key_pred):
            shape, strength = "is_a", 0.15
        else:
            continue
        a, b = sorted((edge.subject, edge.object))
        key = (a, b, shape)
        found.setdefault(key, Cue(a, b, shape, strength, edge.evidence[:SNIPPET_CHARS],
                                  edge.chunk_id))
    return sorted(found.values(), key=lambda c: (c.a, c.b, c.shape))


# --------------------------------------------------------------------------
# similarity

class Contexts:
    """Each entity's passages as the mean of their chunk vectors, less the
    book's mean (so the shared genre does not make everything similar).

    Two entities are compared on the passages each has WITHOUT the other: two
    names always met together (four false Dragons in one paragraph of
    history) have identical passages, which says they are mentioned together,
    not that they are one thing. A pair with no passage apart scores 0."""

    def __init__(self, edges: Iterable[Edge], chunk_vectors: Mapping[str, Sequence[float]]):
        self.chunks: dict[str, frozenset[int]] = {}
        if not chunk_vectors:
            self.matrix = np.zeros((0, 0))
            return
        order = sorted(chunk_vectors)
        row = {c: i for i, c in enumerate(order)}
        matrix = np.asarray([chunk_vectors[c] for c in order], dtype=np.float64)
        self.matrix = matrix - matrix.mean(axis=0)
        found: dict[str, set[int]] = {}
        for edge in edges:
            if edge.chunk_id in row:
                found.setdefault(edge.subject, set()).add(row[edge.chunk_id])
                found.setdefault(edge.object, set()).add(row[edge.chunk_id])
        self.chunks = {eid: frozenset(rows) for eid, rows in found.items()}
        self.sums = {eid: self.matrix[sorted(rows)].sum(axis=0) for eid, rows in found.items()}

    def _apart(self, eid: str, shared: frozenset[int]) -> np.ndarray | None:
        rows = self.chunks[eid]
        if len(rows) <= len(shared):
            return None
        total = self.sums[eid] - (self.matrix[sorted(shared)].sum(axis=0) if shared else 0.0)
        norm = float(np.linalg.norm(total))
        return total / norm if norm > 1e-9 else None

    def cosine(self, a: str, b: str) -> float:
        if a not in self.chunks or b not in self.chunks:
            return 0.0
        shared = self.chunks[a] & self.chunks[b]
        va, vb = self._apart(a, shared), self._apart(b, shared)
        return 0.0 if va is None or vb is None else float(va @ vb)

    def together(self, a: str, b: str) -> float:
        """Share of the rarer one's passages that also have the other."""
        ca, cb = self.chunks.get(a, frozenset()), self.chunks.get(b, frozenset())
        return len(ca & cb) / max(1, min(len(ca), len(cb)))


def feature_vectors(edges: Iterable[Edge]) -> dict[str, dict[str, float]]:
    """Entity id -> tf-idf weighted unit vector of (direction, predicate[,
    neighbour]) features: what it does, to whom, and what is done to it."""
    counts: dict[str, Counter] = {}
    for edge in edges:
        if edge.subject == edge.object or is_identity_predicate(edge.predicate):
            continue
        pred = _predicate_key(edge.predicate)
        counts.setdefault(edge.subject, Counter()).update(
            (f">{pred}", f">{pred}>{edge.object}"))
        counts.setdefault(edge.object, Counter()).update(
            (f"<{pred}", f"<{pred}<{edge.subject}"))
    df: Counter = Counter()
    for features in counts.values():
        df.update(features.keys())
    n = len(counts) or 1
    out: dict[str, dict[str, float]] = {}
    for eid, features in counts.items():
        weighted = {f: (1 + math.log(c)) * math.log(1 + n / df[f]) for f, c in features.items()}
        norm = math.sqrt(sum(v * v for v in weighted.values())) or 1.0
        out[eid] = {f: v / norm for f, v in weighted.items()}
    return out


def _sparse_cosine(a: Mapping[str, float], b: Mapping[str, float], skip: Iterable[str]) -> float:
    """Cosine over shared features, ignoring features naming either side
    (A's edges to B say nothing about whether A is B)."""
    skip = tuple(skip)
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b[f] for f, v in a.items()
               if f in b and not any(f.endswith(s) for s in skip))


# Two things that act on each other are two things. The extractor does write
# a few ordinary edges between two names of one thing (it read "the Fade" and
# "the Myrddraal" in one sentence as two actors: 4 of Myrddraal's 76 edges, at
# most 17% of the lighter side's edges over 11 measured synonym pairs), but
# people who talk, fight and travel together have many: Rand/Tam 99 edges
# (46% of Tam's), Rand/Mat 42%, Moiraine/Lan 27%.
ACTS_ON_EDGES = 5
ACTS_ON_SHARE = 0.3


def acts_on(links: int, weight_a: int, weight_b: int) -> bool:
    """Whether `links` ordinary edges between two entities (of `weight_a`
    and `weight_b` edges in all) say they are two things acting on each other."""
    return links >= ACTS_ON_EDGES and links >= ACTS_ON_SHARE * max(1, min(weight_a, weight_b))


def _siblings(left: str, right: str) -> bool:
    """Same head word, each with a word the other lacks ("red ajah", "yellow
    ajah"; "rand al thor", "tam al thor"): two of a kind, never one."""
    lw, rw = left.split(), right.split()
    if not lw or not rw or lw[-1] != rw[-1]:
        return False
    return bool(set(lw) - set(rw)) and bool(set(rw) - set(lw))


def _nest(left: str, right: str) -> bool:
    lt, rt = set(left.split()), set(right.split())
    return bool(lt) and bool(rt) and (lt <= rt or rt <= lt)


def _plainly_different(a: Entity, b: Entity) -> bool:
    """Name shapes the resolver already rules on: one name inside the other
    (its passes decide those), siblings, a bare title."""
    na, nb = normalize_name(a.name), normalize_name(b.name)
    if not na or not nb or na == nb:
        return True
    for n in (na, nb):
        if all(t in HONORIFICS for t in n.split()):
            return True
    return _nest(na, nb) or _siblings(na, nb)


def listed_together(chunks: Iterable[tuple[str, str]], entities: Iterable[Entity]
                    ) -> set[frozenset[str]]:
    """Pairs the text lists as several things: "Trollocs and Myrddraal", or
    "Raolin Darksbane, Yurian Stonebow, Davian and Guaire Amalasan" (names
    joined by commas, the last by "and"/"or"). Not in a sentence with a naming
    word ("known by many names, among them Halfmen, the Eyeless and Fade")."""
    pattern, owner = _name_index(entities)
    out: set[frozenset[str]] = set()
    if pattern is None:
        return out
    for _cid, text in chunks:
        for sentence in _SENTENCE.findall(text.replace("\n", " ")):
            if _NAMING.search(sentence):
                continue
            named = _hits(pattern, owner, sentence)
            run: list[str] = []
            joined = False
            for index, (m, eid) in enumerate(named):
                run.append(eid)
                gap = sentence[m.end():named[index + 1][0].start()] if index + 1 < len(named) else ""
                if _LISTED.fullmatch(gap) or _LISTED_LAST.fullmatch(gap):
                    joined = True
                    continue
                if _LISTED_COMMA.fullmatch(gap):
                    continue
                if joined:
                    ids = sorted(set(run))
                    out.update(frozenset({x, y}) for i, x in enumerate(ids) for y in ids[i + 1:])
                run, joined = [], False
    return out


def candidate_pairs(
    entities: Iterable[Entity],
    edges: Iterable[Edge],
    chunks: Iterable[tuple[str, str]],
    chunk_vectors: Mapping[str, Sequence[float]],
    *,
    cap: int = MAX_CANDIDATES,
) -> list[Pair]:
    """Ranked candidate pairs (see the module docstring), at most `cap`."""
    entities = {e.id: e for e in entities}
    edges = [e for e in edges if e.subject in entities and e.object in entities]
    chunks = sorted(chunks)
    weight: Counter = Counter()
    links: Counter = Counter()
    passages: dict[str, set[str]] = {}
    for edge in edges:
        weight[edge.subject] += 1
        weight[edge.object] += 1
        passages.setdefault(edge.subject, set()).add(edge.chunk_id)
        passages.setdefault(edge.object, set()).add(edge.chunk_id)
        if edge.subject != edge.object and not is_identity_predicate(edge.predicate):
            links[frozenset({edge.subject, edge.object})] += 1
    contexts = Contexts(edges, chunk_vectors)
    features = feature_vectors(edges)

    def similarity(a: str, b: str) -> tuple[float, float, float]:
        ctx = contexts.cosine(a, b)
        dist = (_sparse_cosine(features[a], features[b], (a, b))
                if a in features and b in features else 0.0)
        return 0.5 * ctx + 0.5 * dist, ctx, dist

    def eligible(a: str, b: str) -> bool:
        ea, eb = entities[a], entities[b]
        return compatible(ea.kind, eb.kind) and not _plainly_different(ea, eb)

    pairs: dict[frozenset[str], Pair] = {}
    cues = text_cues(chunks, entities.values()) + stated_pairs(edges)
    for cue in sorted(cues, key=lambda c: (-c.strength, c.a, c.b, c.shape)):
        key = frozenset({cue.a, cue.b})
        if key in pairs or not eligible(cue.a, cue.b):
            continue
        score, ctx, dist = similarity(cue.a, cue.b)
        pairs[key] = Pair(cue.a, cue.b, 2 * cue.strength + score, cue, ctx, dist)

    # similarity only: mutual top-k among entities with enough edges
    listed = listed_together(chunks, entities.values())
    pool = sorted(eid for eid in entities
                  if weight[eid] >= MIN_EDGES and len(passages.get(eid, ())) >= MIN_EDGES)
    best: dict[str, list[tuple[float, str]]] = {}
    scores: dict[frozenset[str], tuple[float, float, float]] = {}
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            if not eligible(a, b):
                continue
            key = frozenset({a, b})
            if key in listed or acts_on(links[key], weight[a], weight[b]):
                continue
            s = similarity(a, b)
            if s[0] < SIMILARITY_FLOOR:
                continue
            scores[key] = s
            best.setdefault(a, []).append((s[0], b))
            best.setdefault(b, []).append((s[0], a))
    top = {eid: {other for _s, other in sorted(row, key=lambda t: (-t[0], t[1]))[:TOP_K]}
           for eid, row in best.items()}
    used: Counter = Counter()
    for key, (score, ctx, dist) in sorted(scores.items(),
                                          key=lambda kv: (-kv[1][0], sorted(kv[0]))):
        a, b = sorted(key)
        if key in pairs or b not in top.get(a, ()) or a not in top.get(b, ()):
            continue
        if used[a] >= MAX_PER_ENTITY or used[b] >= MAX_PER_ENTITY:
            continue
        used[a] += 1
        used[b] += 1
        pairs[key] = Pair(a, b, score, None, ctx, dist)
    ranked = sorted(pairs.values(), key=lambda p: (-p.score, p.a, p.b))
    return ranked[:cap]


# --------------------------------------------------------------------------
# questions

@dataclass(frozen=True)
class Side:
    id: str
    name: str
    kind: str
    snippets: tuple[str, ...]


@dataclass(frozen=True)
class SynonymQuestion:
    """Are these two names in this book one thing, a claimed identity, or two?"""

    book_id: str
    a: Side
    b: Side
    together: str = ""   # the cue's sentence, or one naming both
    together_chunk: str = ""

    def __post_init__(self) -> None:
        if (self.b.id, self.b.name) < (self.a.id, self.a.name):
            a, b = self.b, self.a
            object.__setattr__(self, "a", a)
            object.__setattr__(self, "b", b)

    @property
    def key(self) -> str:
        """Book and the two entity ids: the snippets may be chosen differently
        after a re-extraction without the question changing."""
        parts = ["synonym", self.book_id, *sorted((self.a.id, self.b.id))]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        side = lambda s: {"id": s.id, "name": s.name, "kind": s.kind,  # noqa: E731
                          "snippets": list(s.snippets)}
        return {"book_id": self.book_id, "a": side(self.a), "b": side(self.b),
                "together": self.together, "together_chunk": self.together_chunk}

    @classmethod
    def from_dict(cls, raw: dict) -> "SynonymQuestion":
        side = lambda s: Side(str(s["id"]), str(s["name"]), str(s["kind"]),  # noqa: E731
                              tuple(str(x) for x in s.get("snippets") or ()))
        return cls(str(raw["book_id"]), side(raw["a"]), side(raw["b"]),
                   str(raw.get("together") or ""), str(raw.get("together_chunk") or ""))


def _spread(items: Sequence, n: int) -> list:
    """`n` items spread evenly over a sorted sequence (first, ..., last)."""
    if len(items) <= n:
        return list(items)
    if n == 1:
        return [items[0]]
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def _story_key(chunk_id: str) -> tuple:
    parts = chunk_id.split(":")
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def questions_for(book_id: str, pairs: Iterable[Pair], entities: Iterable[Entity],
                  edges: Iterable[Edge]) -> list[SynonymQuestion]:
    """One question per pair, with up to `SNIPPETS` evidence sentences per side
    spread over the story, and the sentence that links them when there is one."""
    entities = {e.id: e for e in entities}
    evidence: dict[str, list[tuple[tuple, str]]] = {}
    chunk_of: dict[str, str] = {}
    for edge in sorted(edges, key=lambda e: (_story_key(e.chunk_id), e.evidence)):
        sentence = " ".join(edge.evidence.split())
        if not sentence:
            continue
        chunk_of.setdefault(sentence, edge.chunk_id)
        for eid in {edge.subject, edge.object}:
            evidence.setdefault(eid, []).append((_story_key(edge.chunk_id), sentence))

    def side(eid: str, other: str) -> Side:
        rows = sorted(set(evidence.get(eid, [])))
        # a sentence about both says nothing to tell them apart; keep it out
        own = [r for r in rows if r not in set(evidence.get(other, []))] or rows
        seen: list[str] = []
        for _key, sentence in own:
            if sentence not in seen:
                seen.append(sentence)
        picked = _spread(seen, SNIPPETS)
        entity = entities[eid]
        return Side(eid, entity.name, entity.kind,
                    tuple(s[:SNIPPET_CHARS] for s in picked))

    out = []
    for pair in pairs:
        together, chunk = "", ""
        if pair.cue is not None:
            together, chunk = pair.cue.sentence, pair.cue.chunk_id
        else:
            both = sorted(set(evidence.get(pair.a, [])) & set(evidence.get(pair.b, [])))
            if both:
                together, chunk = both[0][1][:SNIPPET_CHARS], chunk_of.get(both[0][1], "")
        out.append(SynonymQuestion(book_id, side(pair.a, pair.b), side(pair.b, pair.a),
                                   together, chunk))
    return out


_PREAMBLE = """\
You are checking whether two names in one novel refer to the same thing.

Each numbered question gives two names from the same book, a few sentences
where each appears, and, when there is one, a sentence linking them. Answer
with one word per question:

  same      - the two names refer to the same person, creature, kind of
              creature, place, group or thing (another name, title,
              nickname, epithet or euphemism for it), as the book's narration
              or glossary presents it
  claimed   - a character believes, suspects or says one is the other, but
              the book does not establish it as fact
  different - they are different things: relatives, allies, a leader and
              those they lead, an individual and a group or kind it belongs
              to, a place and a place inside it, or anything you are not sure
              of

Answer with one line per question and nothing else, in exactly this form:

  <question number>. <first name> / <second name> -> <same, claimed or different>

If you are not sure, answer different. Give exactly {count} line(s), numbered
1 to {count}, in order.
"""


def synonym_prompt(questions: Sequence[SynonymQuestion]) -> str:
    blocks = [_PREAMBLE.format(count=len(questions))]
    for index, q in enumerate(questions, start=1):
        lines = [f"{index}. {q.a.name} ({q.a.kind}) / {q.b.name} ({q.b.kind})"]
        for side in (q.a, q.b):
            lines.append(f"   {side.name}:")
            lines.extend(f"     - {s}" for s in side.snippets or ("(no sentence recorded)",))
        if q.together:
            lines.append(f"   linking sentence: {q.together}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


SAME, CLAIMED, DIFFERENT = "same", "claimed", "different"
_VERDICTS = {"same": SAME, "yes": SAME, "claimed": CLAIMED, "claim": CLAIMED,
             "believed": CLAIMED, "different": DIFFERENT, "no": DIFFERENT}
_ANSWER_LINE = re.compile(r"^\s*(?:[-*•]\s*)?\**\s*(\d+)\s*\**\s*[.):]\s*(.+)$")
_ARROW = re.compile(r"\s*(?:->|→|=>)\s*")


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _strip_kinds(text: str, q: SynonymQuestion) -> str:
    for kind in {q.a.kind, q.b.kind}:
        text = re.sub(rf"\(\s*{re.escape(kind)}\s*\)", "", text, flags=re.IGNORECASE)
    return text


def parse_synonym_answers(questions: Sequence[SynonymQuestion], text: str) -> dict[str, str]:
    """{question key: same | claimed | different}, or AnswerMismatchError.

    The same contract as `disambiguation.parse_batch_answers`: every number
    exactly once, and each line's echo must name its question's two names, or
    nothing is returned. An unreadable verdict inside a matched line is
    "different" (a refusal), never a guess."""
    from lore.resolve.disambiguation import AnswerMismatchError

    parsed: dict[int, tuple[str, str]] = {}
    for line in text.splitlines():
        match = _ANSWER_LINE.match(line)
        if not match:
            continue
        number = int(match.group(1))
        pieces = _ARROW.split(match.group(2).strip().strip("*").strip())
        if len(pieces) != 2:
            raise AnswerMismatchError(f"answer {number} is not `<n>. <a> / <b> -> <verdict>`:"
                                      f" {line.strip()!r}")
        if number in parsed:
            raise AnswerMismatchError(f"answer number {number} appears more than once")
        parsed[number] = (pieces[0].strip(), pieces[1].strip())
    expected = set(range(1, len(questions) + 1))
    if set(parsed) != expected:
        raise AnswerMismatchError(
            f"expected exactly {len(questions)} answer(s), got {len(parsed)};"
            f" missing {sorted(expected - set(parsed))}, extra {sorted(set(parsed) - expected)}")
    out: dict[str, str] = {}
    for number, (echoed, verdict) in sorted(parsed.items()):
        q = questions[number - 1]
        echo = _squash(_strip_kinds(echoed, q))
        if echo not in {_squash(q.a.name + q.b.name), _squash(q.b.name + q.a.name)}:
            raise AnswerMismatchError(
                f"answer {number} echoes {echoed!r} but question {number} asked about"
                f" {q.a.name!r} / {q.b.name!r}")
        word = re.sub(r"[^a-z]", "", verdict.casefold().split()[0]) if verdict.split() else ""
        out[q.key] = _VERDICTS.get(word, DIFFERENT)
    return out


async def ask_synonyms(client, questions: Sequence[SynonymQuestion], *,
                       max_batch: int = SYNONYM_BATCH) -> dict[str, str]:
    """Every question in as few requests as the batch cap allows; raises on
    any failure, having written nothing."""
    answers: dict[str, str] = {}
    for start in range(0, len(questions), max_batch):
        batch = list(questions[start:start + max_batch])
        answers.update(parse_synonym_answers(batch, await client.generate(synonym_prompt(batch))))
    return answers


# --------------------------------------------------------------------------
# applying the answers

@dataclass
class Applied:
    """What the answers did: groups merged (canonical id -> merged ids) and
    the claimed identities (alias id, name id, sentence, chunk id)."""

    merged: dict[str, list[str]] = field(default_factory=dict)
    claimed: list[tuple[str, str, str, str]] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)


def plan(answers: Iterable[tuple[SynonymQuestion, str]], edges: Iterable[Edge],
         weight: Mapping[str, int]) -> Applied:
    """Groups to merge from the "same" answers, and the claimed identities.

    "Same" answers join by union-find, but never two groups that act on each
    other (`acts_on`), whatever one answer said. The canonical member of a
    group is the one with the most edges (then id). A claim is kept as a
    link; one between two members of a merged group links nothing."""
    links: Counter = Counter()
    for edge in edges:
        if edge.subject != edge.object and not is_identity_predicate(edge.predicate):
            links[frozenset({edge.subject, edge.object})] += 1
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    members: dict[str, set[str]] = {}
    result = Applied()
    ordered = sorted(answers, key=lambda qa: (qa[0].a.id, qa[0].b.id))
    for q, verdict in ordered:
        if verdict != SAME:
            continue
        ra, rb = find(q.a.id), find(q.b.id)
        if ra == rb:
            continue
        ga, gb = members.get(ra, {ra}), members.get(rb, {rb})
        if acts_on(sum(links[frozenset({x, y})] for x in ga for y in gb),
                   sum(weight.get(x, 0) for x in ga), sum(weight.get(y, 0) for y in gb)):
            result.refused.append((q.a.id, q.b.id))
            continue
        parent[rb] = ra
        members[ra] = ga | gb
        members.pop(rb, None)
    for root, group in members.items():
        canonical = min(group, key=lambda e: (-weight.get(e, 0), e))
        result.merged[canonical] = sorted(group - {canonical})
    for q, verdict in ordered:
        if verdict != CLAIMED or find(q.a.id) == find(q.b.id):
            continue
        # the less-used name is the one claimed to be the other
        alias, name = sorted((q.a.id, q.b.id), key=lambda e: (weight.get(e, 0), e))
        result.claimed.append((alias, name, q.together, q.together_chunk))
    return result


def merge_resolution(resolution, merged: Mapping[str, Sequence[str]]):
    """The ResolutionResult with each merged id folded into its canonical:
    mention_map re-pointed, alias records re-pointed plus one per merged id,
    aliases and books joined. Returns a new result; the input is untouched."""
    from lore.resolve.normalize import normalize_name as _norm
    from lore.resolve.resolver import AliasRecord, ResolutionResult

    into = {old: canonical for canonical, olds in merged.items() for old in olds}
    if not into:
        return resolution
    by_id = {e.id: e for e in resolution.entities}
    entities = []
    for entity in resolution.entities:
        if entity.id in into:
            continue
        olds = [by_id[o] for o in merged.get(entity.id, ()) if o in by_id]
        if olds:
            aliases = set(entity.aliases)
            books = set(entity.book_ids)
            for old in olds:
                aliases |= set(old.aliases) | {_norm(old.name)}
                books |= set(old.book_ids)
            entity = replace(entity, aliases=tuple(sorted(a for a in aliases if a)),
                             book_ids=tuple(sorted(books)))
        entities.append(entity)
    aliases = [replace(a, canonical_id=into.get(a.canonical_id, a.canonical_id))
               for a in resolution.aliases]
    recorded = {(a.alias_id, a.canonical_id) for a in aliases}
    for old, canonical in sorted(into.items()):
        entity = by_id.get(old)
        if entity is not None and (old, canonical) not in recorded:
            aliases.append(AliasRecord(alias_id=old, alias_name=entity.name,
                                       kind=entity.kind, canonical_id=canonical))
    mention_map = {k: into.get(v, v) for k, v in resolution.mention_map.items()}
    return ResolutionResult(entities=entities, aliases=aliases, mention_map=mention_map)
