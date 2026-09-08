"""Whether a mention's surface name plausibly names an actual entity.

`lore/extract/prompts.py` tells the extraction model to "use the entity's
fullest name as written in the passage" without ever requiring that name be
a NAMED thing, so the model happily mints a first-class entity for whatever
noun phrase the passage handed it. Measured on the loaded graph for *A Study
in Scarlet* (518 entities), only 142 (27.4%) were actually named things; the
rest were definite descriptions like "his nose", "the pavement", "my
curiosity", "the weather" (47.1%), lowercase fragments (20.8%), and chapter
heading / licence boilerplate (3.5%). Fixing the prompt requires
re-extracting the corpus (~3 GPU-hours); this check runs at load time
instead, so it is the cheap lever and can be iterated in ~2.5 minutes/book.

A naive "does the name start with 'the'" test was considered and rejected:
it would also destroy real entities whose leading article is just how
English names them — "the Marchioness de Brinvilliers", "The Daily News",
"The Saints", "the United States". Do not simplify this back into that
test; the examples above must keep passing.

The rule that survives real data: strip ONE leading determiner or
possessive token, then require at least one capitalised token in what
remains.

    "the box"                        -> "box"                  -> reject
    "his hands"                      -> "hands"                 -> reject
    "The two detectives"             -> "two detectives"        -> reject
    "My dear fellow"                 -> "dear fellow"            -> reject
    "the Marchioness de Brinvilliers"-> "Marchioness de Brinvilliers" -> keep
    "The Daily News"                 -> "Daily News"            -> keep
    "the United States"              -> "United States"         -> keep
    "Sherlock Holmes" (no determiner)-> "Sherlock Holmes"        -> keep

Because only ONE leading token is stripped, a name whose only capital letter
IS the sentence-initial determiner ("The pass") is correctly rejected: after
stripping "The", "pass" is not capitalised.
"""
from __future__ import annotations

# Determiners and possessives to strip, leading only, case-insensitive.
_DETERMINERS = frozenset({
    "the", "a", "an", "this", "that", "these", "those",
    "my", "your", "his", "her", "its", "our", "their",
})


def _has_capitalised_letter(token: str) -> bool:
    """True if the token's first ALPHABETIC character is uppercase.

    Scanning past leading non-letters (a curly quote, a stray hyphen)
    rather than checking `token[0]` means a name like a leading curly
    quote before an accented capital ("'Emile) is still recognised as
    capitalised, and a purely punctuational token ("...") is not.
    """
    for char in token:
        if char.isalpha():
            return char.isupper()
    return False


def is_probable_entity(name: str) -> bool:
    """Reject a definite description or lowercase fragment; keep a name.

    Pure and dependency-free by design (see module docstring for why the
    rule looks the way it does, and what a naive "starts with 'the'" test
    would break).
    """
    tokens = name.split()
    if tokens and tokens[0].casefold() in _DETERMINERS:
        tokens = tokens[1:]
    return any(_has_capitalised_letter(token) for token in tokens)
