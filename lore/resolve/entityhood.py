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

Two shapes slip past that rule and are rejected ahead of it, both measured on
the freshly rebuilt two-novel graph (431 entities):

  * a BARE PRONOUN. Nine nodes were pronouns ("He", "I", "We", "You", "YOU",
    some once per book), and "I" was the second-busiest entity in the whole
    graph with 116 edges — a capitalised single token looks exactly like a
    name. Only the whole name counts: "I" goes, "Ivan", "Iris", "Wells" and
    "He Xiaolong" stay.

  * an ALL-CAPS MULTI-WORD heading. 29 survived, from "CHAPTER III" to
    "A CONTINUATION OF THE REMINISCENCES OF JOHN WATSON, M.D.". They are not
    inert: a long heading shares a token with many real names, so the
    resolver's shared-token path pulled "john ferrier", "john rance" and
    "john watson" onto that last one — a core character absorbed into a
    table-of-contents artifact. Real names in running prose are not written
    in full caps, so requiring MORE THAN ONE word keeps a lone all-caps
    token ("IRS", the Gutenberg licence's "FOUNDATION") out of scope.
"""
from __future__ import annotations

import re

# A name that is nothing but one of these is not an entity, however
# capitalised it looks. Whole-name only: "Ida" and "Wells" merely start with
# one, and "He Xiaolong" merely contains one.
_PRONOUNS = frozenset({
    "i", "he", "she", "we", "they", "you", "it",
    "me", "him", "her", "us", "them",
})

# Determiners and possessives to strip, leading only, case-insensitive.
_DETERMINERS = frozenset({
    "the", "a", "an", "this", "that", "these", "those",
    "my", "your", "his", "her", "its", "our", "their",
})


_POSSESSIVE = re.compile(r"['’]s\b")


def _bare_word(token: str) -> str:
    """The token reduced to its lowercase letters, possessive removed.

    "Inspector's" -> "inspector", "inspector." -> "inspector", "(we)" -> "we".
    Used only by the pronoun and generic-token tests below, both of which ask
    whether a WHOLE token is one specific word, so dropping every non-letter
    is safe here in a way it would not be for `normalize_name`.
    """
    return "".join(
        char for char in _POSSESSIVE.sub("", token) if char.isalpha()
    ).casefold()


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
    if not tokens:
        return False

    # A bare pronoun, whatever its case or trailing punctuation.
    if len(tokens) == 1 and _bare_word(tokens[0]) in _PRONOUNS:
        return False

    # An all-caps multi-word chapter heading. `str.isupper()` is False when
    # the name has no cased characters at all, so an uncased string ("221 43")
    # falls through to the ordinary rule below rather than being caught here.
    if len(tokens) > 1 and name.isupper():
        return False

    if tokens[0].casefold() in _DETERMINERS:
        tokens = tokens[1:]
    return any(_has_capitalised_letter(token) for token in tokens)


# --------------------------------------------------------------------------
# Task 1h defect 3: is a name distinctive enough to be the same entity in
# two different books?
#
# `_reconcile_ids` in lore/graph/loader.py used to hand that question to the
# local 3B adjudicator. Measured on the freshly rebuilt two-novel graph, the
# model got it backwards in both directions: it merged `the police Inspector`
# across books (Lestrade in one, Athelney Jones in the other — two people)
# and refused `Sherlock Holmes` <-> `Mr. Sherlock Holmes`, which normalize to
# the identical string. Two rounds of prompt tuning did not fix it, so the
# decision is taken away from the model, as Task 1g did for same-book name
# variants. Deliberate, and deliberately deterministic: an id collision means
# the normalized names are IDENTICAL, and identical names are not a judgement
# call. What matters is whether the name is distinctive or generic, and a
# stoplist answers that repeatably.
#
# A name is generic only when EVERY one of its tokens is a role, a descriptor
# or a determiner. One real name beside a role word is enough to be
# distinctive, which is what keeps `Inspector Lestrade`, `Doctor Watson` and
# `Captain Morstan` mergeable across books while `the police Inspector` is
# not.
#
# Deliberately NOT on the list: adjectives that double as surnames. `young`
# would make "a young man" generic but also "Young" — and Brigham Young is a
# character in this very corpus. Same reasoning keeps `king`, `prince` and
# `hope` off it (compare normalize.py's note on honorifics).
_GENERIC_TOKENS = frozenset({
    # Determiners and possessives (a bare "the Inspector" must stay generic).
    "the", "a", "an", "this", "that", "these", "those",
    "my", "your", "his", "her", "its", "our", "their",
    # Roles and titles.
    "inspector", "captain", "doctor", "sergeant", "colonel", "major",
    "professor", "landlady", "detective", "constable", "officer", "police",
    "prisoner", "stranger", "servant", "companion", "friend", "elder",
    "prophet",
    # Bare descriptors of a person.
    "boy", "girl", "man", "woman", "lady", "gentleman",
    "brother", "sister", "son", "daughter", "wife", "husband", "child",
    # Plurals of the above that appear as their own nodes in the live graph
    # ("Elders", "Servants", "The two Smiths" is not one of these).
    "inspectors", "captains", "doctors", "sergeants", "colonels", "majors",
    "professors", "detectives", "constables", "officers", "prisoners",
    "strangers", "servants", "companions", "friends", "elders",
    "boys", "girls", "men", "women", "ladies", "gentlemen",
    "brothers", "sisters", "sons", "daughters", "wives", "husbands",
    "children",
})


def is_generic_name(name: str) -> bool:
    """True when every token of `name` is a role, descriptor or determiner.

    Case- and punctuation-insensitive, and possessive-insensitive, so
    `INSPECTOR`, `inspector.` and `the Inspector's` all read as generic.
    Accepts either a raw surface name or an already-normalized one, so the
    loader can pass `candidate.base` straight in.

    A name with no word characters is reported NOT generic: "every token is
    generic" is vacuously true of no tokens, and turning that into a merge
    refusal would be an accident rather than a decision.
    """
    tokens = [word for word in (_bare_word(t) for t in name.split()) if word]
    return bool(tokens) and all(token in _GENERIC_TOKENS for token in tokens)
