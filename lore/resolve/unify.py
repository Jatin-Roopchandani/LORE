"""One name, one node: the same name under different kinds or numbers, and
names the text itself says are one thing.

Measured on a 1,235-passage novel's graph: "Myrddraal" was four nodes (the
extractor called it a Faction 41 times, a Character 30, a Concept 3, an
Object 2), "The Dark One" (Character, 161 edges) and "Dark One" (Concept, 39)
two, and "the Fades" / "Fades" / "The Fade" / "Fade" eight. An entity id is a
hash of (normalized name, kind), and every resolver pass compares within one
kind only, so a kind the extractor wavered on split one thing into several.

Before resolution, within one book:

- **plural folding** (`fold_map`): a name whose last word is a plural of
  another name in the same book is that name ("the Fades" -> "fade",
  "Halfmen" -> "halfman"). Only when the singular is itself a name in the
  book, and for a plain -s/-es/-ies plural only when one of the two is
  written as a common noun somewhere (a leading article, or lower case: "the
  Fades"); a surname plural ("the Rivers" family vs "River") needs that and
  so stays apart when written as a name;
- **one kind per name** (`majority_kinds`): every mention of a (folded) name
  takes the kind most of its edges gave it, ties to the kind earlier in
  `KIND_ORDER`, then alphabetically;
- **the text's own equivalences** (`find_equivalences`): "Myrddraal, also
  called Halfmen", "Halfmen, as some call them", "the Fade, the Myrddraal,".
  Narration's are merge CANDIDATES that go to the existing disambiguation
  questions (lore/resolve/disambiguation.py), never merged by pattern alone;
  ones the text gives as a character's belief or claim ("I think Ba'alzamon
  is the Dark One", in dialogue or with a belief word) are never merged:
  they become an IDENTIFIED_AS edge with their evidence, since a later book
  may contradict a character.

Pure functions, for any book; nothing here knows a title.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from lore.resolve.normalize import normalize_name

# Tie-break among kinds with equal weight: the more specific reading first.
KIND_ORDER = ("Character", "Faction", "Place", "Object", "Event", "Concept")

_ARTICLE = re.compile(r"^\s*(the|a|an)\s+", re.IGNORECASE)


def singulars(token: str) -> list[str]:
    """Singular candidates of a plural word, most specific first."""
    out: list[str] = []
    if len(token) > 4 and token.endswith("men"):
        out.append(token[:-3] + "man")
    if len(token) > 4 and token.endswith("ies"):
        out.append(token[:-3] + "y")
    if len(token) > 3 and token.endswith("es"):
        out.append(token[:-2])
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        out.append(token[:-1])
    return out


def _common_noun(raw: str) -> bool:
    raw = raw.strip()
    return bool(_ARTICLE.match(raw)) or (raw[:1].isalpha() and raw[:1].islower())


def fold_map(names: Iterable[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """(book_id, normalized plural) -> normalized singular, for (book_id, raw
    name) pairs (see the module docstring). Names not folded are absent."""
    raws: dict[tuple[str, str], set[str]] = {}
    for book_id, raw in names:
        normalized = normalize_name(raw)
        if normalized:
            raws.setdefault((book_id, normalized), set()).add(raw)
    out: dict[tuple[str, str], str] = {}
    for (book_id, normalized), forms in sorted(raws.items()):
        *head, last = normalized.split()
        for singular in singulars(last):
            target = " ".join([*head, singular])
            if (book_id, target) not in raws:
                continue
            irregular = last.endswith("men") and singular.endswith("man")
            if irregular or any(_common_noun(r) for r in forms | raws[(book_id, target)]):
                out[(book_id, normalized)] = target
                break
    # a chain (a -> b -> c) lands on its end
    for key in list(out):
        seen = {key[1]}
        while (key[0], out[key]) in out and out[(key[0], out[key])] not in seen:
            seen.add(out[key])
            out[key] = out[(key[0], out[key])]
    return out


def majority_kinds(weights: Mapping[tuple[str, str, str], int]) -> dict[tuple[str, str], str]:
    """(book_id, name key) -> its kind, from (book_id, name key, kind) -> weight."""
    totals: dict[tuple[str, str], dict[str, int]] = {}
    for (book_id, key, kind), weight in weights.items():
        totals.setdefault((book_id, key), {})
        totals[(book_id, key)][kind] = totals[(book_id, key)].get(kind, 0) + weight

    def rank(kind: str) -> int:
        return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)

    return {group: min(kinds, key=lambda k: (-kinds[k], rank(k), k))
            for group, kinds in totals.items()}


@dataclass(frozen=True)
class Equivalence:
    """Two names the text says are one thing: `alias` names `name`."""

    name: str        # raw surface form, as in the book's mentions
    alias: str
    evidence: str    # the sentence
    chunk_id: str
    believed: bool   # a character's belief or claim, not narration


# Words that make a stated identity someone's belief or claim.
_BELIEF = re.compile(
    r"\b(believe[sd]?|believing|think|thinks|thought|suppose[sd]?|suspect(?:s|ed)?|"
    r"claim(?:s|ed)?|convinced|perhaps|maybe|might be|may be|must be|could be|"
    r"rumou?r(?:s|ed)?|is said|was said|they say|some say|calls? himself|calls? herself)\b",
    re.IGNORECASE)
_QUOTES = "\"“”"
_SENTENCE = re.compile(r"[^.!?]*[.!?]+[\"”’]?|[^.!?]+$")


def _names_pattern(names: Iterable[str]) -> re.Pattern | None:
    forms = sorted({n.strip() for n in names if len(n.strip()) > 2}, key=lambda n: (-len(n), n))
    if not forms:
        return None
    alternation = "|".join(re.escape(_ARTICLE.sub("", f)) for f in forms)
    return re.compile(rf"(?:\b(?:the|a|an)\s+)?\b(?P<n>{alternation})\b", re.IGNORECASE)


def _in_quotes(sentence: str, start: int) -> bool:
    return sum(sentence[:start].count(q) for q in _QUOTES) % 2 == 1


# NAME, also called / also known as / known as / or ALIAS
_CALLED = r"\s*,\s*(?:also\s+)?(?:called|known\s+as)\s+"
# ALIAS, as some/men/they call them|it|him
_AS_CALLED = r"\s*,?\s*(?:what|as)\s+(?:some|men|many|people|folk|they|others)\s+call\b"
_IS = r"\s+(?:is|was)\s+(?:really\s+|truly\s+|in\s+truth\s+)?"


def _identity_claim(sentence: str, a: re.Match, b: re.Match) -> bool:
    """"A is B" says who A IS only when B is a proper name standing alone:
    not "a Darkfriend" (a sort), not "the fortress" (a common noun), not
    "Queen Morgase's advisor" or "the Light made flesh" (B goes on), and
    neither side a bare title ("Their King was Aemon ...", a role).
    Measured on a novel: 8 claims before these checks, 2 after ("Lan is
    Andra", a false name, and "Gareth Bryne is Captain-General of the
    Queen's Guards", his office)."""
    from lore.resolve.normalize import HONORIFICS

    if re.match(r"(?:a|an)\s", b.group(0), re.IGNORECASE):
        return False
    if not b.group("n")[:1].isupper():
        return False
    if not re.match(r"\s*(?:(?:him|her|it|them)sel(?:f|ves)\b\s*)?(?:[,.;:!?\"”’]|$)",
                    sentence[b.end():]):
        return False
    for m in (a, b):
        tokens = normalize_name(m.group("n")).split()
        if tokens and all(t in HONORIFICS for t in tokens):
            return False
    return True


def find_equivalences(chunks: Iterable[tuple[str, str]],
                      names: Iterable[str]) -> list[Equivalence]:
    """Equivalences stated between two of `names` in the chunks' text.

    Three shapes: "A, also called B" (and "known as", a list of
    several after it), "B, as some call them" right after "A" ("A, B, as men
    call them"), and "A is B" / "A was really B". The last is the shape of
    an identity CLAIM: it counts only inside a quotation or with a belief
    word, and is then `believed`. Every result names two DIFFERENT
    normalized names. Sorted, so a function of the text."""
    pattern = _names_pattern(names)
    if pattern is None:
        return []
    found: set[Equivalence] = set()
    for chunk_id, text in chunks:
        for sentence in _SENTENCE.findall(text.replace("\n", " ")):
            sentence = sentence.strip()
            hits = list(pattern.finditer(sentence))
            for i, a in enumerate(hits):
                # "A, also called B1, B2 and B3": after the marker, only a
                # list of names (each gap a list separator) may follow
                if i + 1 < len(hits) and re.fullmatch(
                        _CALLED + r"(?:the\s+)?", sentence[a.end():hits[i + 1].start()],
                        re.IGNORECASE):
                    for j in range(i + 1, len(hits)):
                        b = hits[j]
                        if j > i + 1 and not re.fullmatch(
                                r"\s*,?\s*(?:and\s+|or\s+)?(?:the\s+)?",
                                sentence[hits[j - 1].end():b.start()], re.IGNORECASE):
                            break
                        if normalize_name(a.group("n")) != normalize_name(b.group("n")):
                            # a name some people use is naming, not a claim
                            found.add(Equivalence(a.group("n"), b.group("n"), sentence,
                                                  chunk_id, False))
                for b in hits[i + 1:i + 5]:
                    gap = sentence[a.end():b.start()]
                    quoted = _in_quotes(sentence, a.start())
                    believed = quoted or bool(_BELIEF.search(sentence))
                    if normalize_name(a.group("n")) == normalize_name(b.group("n")):
                        continue
                    if re.match(_CALLED, gap, re.IGNORECASE):
                        continue  # handled above
                    elif re.fullmatch(r"\s*,\s*(?:the\s+)?", gap) and re.match(
                            _AS_CALLED, sentence[b.end():], re.IGNORECASE):
                        found.add(Equivalence(a.group("n"), b.group("n"), sentence, chunk_id,
                                              False))
                    elif (re.fullmatch(_IS + r"(?:the\s+)?", gap, re.IGNORECASE) and believed
                          and _identity_claim(sentence, a, b)):
                        found.add(Equivalence(b.group("n"), a.group("n"), sentence, chunk_id,
                                              True))
    return sorted(found, key=lambda e: (e.chunk_id, e.name, e.alias, e.evidence))
