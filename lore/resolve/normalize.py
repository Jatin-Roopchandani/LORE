"""Surface-name normalization — where most false merges are won or lost.

Deliberately conservative: it strips the decorations that make one
character look like two (honorifics, possessives, punctuation, a leading
article) and nothing else. It never returns an empty string, because
"the Captain" is a real way to name someone.
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
    while tokens and tokens[0] in _ARTICLES:
        tokens = tokens[1:]
    without_honorifics = [t for t in tokens if t not in HONORIFICS]
    # Falling back to `tokens` keeps honorific-only names ("the Captain")
    # from normalizing to nothing.
    return " ".join(without_honorifics or tokens)


def entity_id(normalized_name: str, kind: str) -> str:
    digest = hashlib.sha256(f"{normalized_name}|{kind}".encode()).hexdigest()
    return f"e{digest[:12]}"
