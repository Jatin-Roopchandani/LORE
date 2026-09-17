"""Surface-name normalization — where most false merges are won or lost.

Deliberately conservative: it strips the decorations that make one
character look like two (honorifics, possessives, punctuation, a leading
article) and nothing else. Honors the invariant: for input containing at least
one word character, never returns an empty string; for punctuation-only or
blank input, legitimately yields "" (callers skip such mentions).

Honorifics are stripped only when LEADING a name (a title prefix), never
mid-name, to avoid fusing unrelated characters: "King" and "Prince" are
common surnames and must survive in "Larry King" or "Martin Luther King".
"""
from __future__ import annotations

import hashlib
import re

HONORIFICS = frozenset({
    "mr", "mrs", "miss", "ms", "dr", "doctor", "sir", "madam", "lady", "lord",
    "captain", "colonel", "major", "general", "king", "queen", "prince",
    "princess", "st", "saint", "father", "mother", "uncle", "aunt",
})
_ARTICLES = frozenset({"the", "a", "an"})
_POSSESSIVE = re.compile(r"['']s\b")
_NON_WORD = re.compile(r"[^\w\s]")


def normalize_name(name: str) -> str:
    text = _POSSESSIVE.sub("", name.casefold())
    text = _NON_WORD.sub(" ", text)
    tokens = [t for t in text.split() if t]

    # Remember state before stripping articles
    tokens_before_articles = tokens[:]

    # Strip leading articles
    while tokens and tokens[0] in _ARTICLES:
        tokens = tokens[1:]

    # Remember state before stripping honorifics
    tokens_before_honorifics = tokens[:]

    # Strip leading honorifics (a title prefix), not from anywhere in the name
    # to preserve common surnames like King, Prince, Lord, Major, Sir
    while tokens and tokens[0] in HONORIFICS:
        tokens = tokens[1:]

    # Fall back progressively to ensure non-empty for word-containing input,
    # and empty only for punctuation-only or blank input
    if tokens:
        return " ".join(tokens)
    elif tokens_before_honorifics:
        return " ".join(tokens_before_honorifics)
    elif tokens_before_articles:
        return " ".join(tokens_before_articles)
    else:
        return ""


# Two spellings of one title are one title ("Dr. Watson", "Doctor Watson").
_TITLE_SPELLINGS = {"dr": "doctor", "st": "saint", "ms": "miss"}


def leading_titles(name: str) -> frozenset[str]:
    """The titles `normalize_name` strips from the front of a surface form.

    Task 11a. Stripping them is what lets "Mr. Darcy" and "Darcy" be one
    name — and also what makes "Major Sholto" and "Mr. Sholto", or "Captain
    Morstan" and "Miss Morstan", one name when they are two people. The
    resolver uses these to keep a conflicting title from joining an entity
    by name alone (see `resolve_mentions`).

    Empty for a name that is nothing but a title ("the Captain"): there the
    title IS the name, and `normalize_name` keeps it.
    """
    text = _NON_WORD.sub(" ", _POSSESSIVE.sub("", name.casefold()))
    tokens = text.split()
    while tokens and tokens[0] in _ARTICLES:
        tokens = tokens[1:]
    titles: set[str] = set()
    while tokens and tokens[0] in HONORIFICS:
        titles.add(_TITLE_SPELLINGS.get(tokens[0], tokens[0]))
        tokens = tokens[1:]
    return frozenset(titles) if tokens else frozenset()


def entity_id(normalized_name: str, kind: str) -> str:
    digest = hashlib.sha256(f"{normalized_name}|{kind}".encode()).hexdigest()
    return f"e{digest[:12]}"
