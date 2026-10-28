"""Three-pass entity resolution: the "Voldemort ≡ The Dark Lord" problem.

Pass 1 is exact/alias matching on the normalized name — free, and it
handles most of a book. Pass 2 embeds name plus local context and compares
against same-kind candidates, but first settles the same-book name-variant
case by string matching alone: if one name's identity tokens nest inside
the other's ("holmes" in "sherlock holmes"), that is not a judgement call
and neither the embedder nor the LLM is consulted. Pass 3 asks an LLM, and
only for the ambiguous middle. A merge across two different books ALWAYS
requires pass 3 to approve it, so "the Captain" in two unrelated novels
stays two people.

Embeddings are computed lazily and cached: a brand-new entity with no
existing same-kind candidates never touches the embedder (nothing to
compare it to yet). Its representative name/context is kept so that if a
*later* mention needs to compare against it, that comparison batches the
mention's own text with any not-yet-cached candidate texts into a single
embed call.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Container, Iterable, Iterator
from dataclasses import dataclass, field

from lore.resolve.disambiguation import Candidate, Question
from lore.resolve.normalize import entity_id, leading_titles, normalize_name
from lore.resolve.predicates import cosine

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]
# Task 1j's two optional hooks onto the ambiguous residue. Both default to
# None, and with neither wired every decision below is exactly what it was
# before: an ambiguous subset match refuses, offline and for free.
RecordAmbiguous = Callable[[Question], None]
DecideAmbiguous = Callable[[Question], str | None]


@dataclass(frozen=True)
class Mention:
    name: str
    kind: str
    book_id: str
    context: str


@dataclass(frozen=True)
class CanonicalEntity:
    id: str
    name: str
    kind: str
    aliases: tuple[str, ...]
    book_ids: tuple[str, ...]


@dataclass(frozen=True)
class AliasRecord:
    alias_id: str
    alias_name: str
    kind: str
    canonical_id: str


@dataclass
class ResolutionResult:
    entities: list[CanonicalEntity] = field(default_factory=list)
    aliases: list[AliasRecord] = field(default_factory=list)
    mention_map: dict[tuple[str, str, str], str] = field(default_factory=dict)


Adjudicate = Callable[[CanonicalEntity, Mention], Awaitable[bool]]


# Tokens that carry no identity: articles, determiners, bare pronouns,
# the prepositions and conjunctions that join name parts, and nobiliary
# particles. `normalize_name` strips a LEADING article or honorific, but an
# interior one survives ("the Sign of the Four" -> "sign of the four"), and
# the corpus really does yield mentions named "i", "we" and "our prisoner".
# Two names whose only common token is one of these are not related names.
_FUNCTION_WORDS = frozenset({
    "the", "a", "an", "this", "that", "these", "those",
    "i", "me", "my", "we", "us", "our", "you", "your",
    "he", "him", "his", "she", "her", "hers", "it", "its",
    "they", "them", "their",
    "of", "and", "or", "in", "on", "at", "to", "for", "from", "with", "by",
    "de", "del", "della", "di", "du", "da", "van", "von", "der", "den",
    "la", "le", "el", "los", "las", "bin", "ibn",
    # Clitic remnants. `normalize_name`'s possessive regex only knows the
    # ASCII apostrophe, so a curly one (which is what the corpus text
    # actually uses) survives as a bare token: "Knight’s Place" normalizes
    # to "knight s place" and "Dawson’s wife" to "dawson s wife". Those
    # two merged in the live corpus on nothing but that stray "s".
    "ll", "re", "ve",
})


def name_tokens(normalized: str) -> frozenset[str]:
    """The identity-bearing tokens of an already-normalized name.

    Single-character tokens are excluded along with the function words:
    they are initials, clitic remnants and list markers, never identity.
    "J. Watson" and "J. Smith" share only "j", which is not evidence they
    are one person — and "J. Watson" still reaches "Watson" through
    "watson", so nothing real is lost.

    Empty for a name made of nothing but such tokens — which makes that
    name un-mergeable in pass 2. That is the safe direction: it can only
    under-merge, and pass 1 still collapses exact repeats of it.
    """
    return frozenset(
        token
        for token in normalized.split()
        if len(token) > 1 and token not in _FUNCTION_WORDS
    )


def tokens_nest(left: frozenset[str], right: frozenset[str]) -> bool:
    """True when one of two non-empty token sets contains the other.

    Public because two rules share it: pass 2a below decides SAME-book name
    variants with it, and `lore.graph.loader._reconcile_ids` decides
    CROSS-book ones (Task 1i). One rule, one place.

    Both sides must be non-empty: the empty set is a subset of everything,
    and a name whose tokens are all function words or initials must never
    nest inside — and so merge with — an arbitrary other name.
    """
    return bool(left) and bool(right) and (left <= right or right <= left)


# Task 1k — a definite description must not swallow the name inside it.
#
# The subset rule above is what correctly unifies `Holmes` with `Sherlock
# Holmes`, but a definite description's tokens are a superset of the name
# it is about, so the same rule let the description win. Measured on the
# rebuilt two-novel graph (353 entities), 53 names were description-shaped
# and 44 had absorbed something:
#
#     'the base hospital at Peshawar'  <- absorbed 'peshawar'
#     'boarding-house at Camberwell'   <- absorbed 'camberwell'
#     'the broad valley of Utah'       <- absorbed 'utah'
#     'foreigners in England'          <- absorbed 'england'
#     "Lucy Ferrier's father"          <- absorbed 'lucy ferrier'
#
# `Camberwell` had stopped being a node at all, so nothing could be asked
# about it. The root cause is upstream — `is_probable_entity` admits any
# phrase containing a capitalised token, so `foreigners in England` passes
# as a name — and fixing that needs a re-extraction (Phase 7). Until then a
# description may EXIST as an entity, but it may never be the TARGET of a
# subset merge.
#
# TEST THE RAW SURFACE FORM, NEVER THE NORMALIZED ONE. `normalize_name`
# strips possessives, so "Lucy Ferrier's father" -> 'lucy ferrier father',
# which looks like an ordinary three-token name; a guard reading the
# normalized string fires on nothing and the defect ships silently. And the
# corpus text uses the CURLY apostrophe, which `normalize_name`'s ASCII-only
# possessive regex does not even strip ("Ferrier’s farm" -> 'ferrier s
# farm'), so both apostrophes have to be recognised here.
_POSSESSIVE_SUFFIX = re.compile(r"['’]s\b")

# INTERNAL function words only, and a much smaller list than
# `_FUNCTION_WORDS` above. Do NOT "simplify" this into a leading-article
# test: `The Daily News` and `the Marchioness de Brinvilliers` are real
# entities in the corpus and must keep merging normally, and `de` joins the
# parts of a name rather than describing a relation between two things.
# Only a possessive or a function word AFTER the first token disqualifies.
_DESCRIPTION_WORDS = frozenset({
    "of", "in", "at", "on", "for", "with", "from", "to",
})
_EDGE_PUNCTUATION = ".,;:!?\"()[]"


def is_description_shaped(raw_name: str) -> bool:
    """True when a RAW surface form describes an entity instead of naming it.

    "boarding-house at Camberwell" and "Lucy Ferrier's father" are
    descriptions; "Mr. Thaddeus Sholto", "Baker Street", "Scotland Yard"
    and "The Daily News" are names. Used only to disqualify a merge TARGET
    (see `swallows_a_name`), so a false positive costs an under-merge —
    two nodes a later pass can still join — while a false negative
    destroys a real entity.
    """
    tokens = raw_name.split()
    return any(_POSSESSIVE_SUFFIX.search(token) for token in tokens) or any(
        token.strip(_EDGE_PUNCTUATION).casefold() in _DESCRIPTION_WORDS
        for token in tokens[1:]
    )


def swallows_a_name(
    left: str, right: str, descriptive: Container[str]
) -> bool:
    """True when the SUPERSET side of a nesting pair is description-shaped.

    Direction is the whole point: `Camberwell` being absorbed INTO
    `boarding-house at Camberwell` is the defect, so the guard applies to
    the longer name, the one that would end up owning the node and its
    display name. Two names with equal token sets are the same name written
    two ways and neither swallows the other; two descriptions merging with
    each other is not this defect (and is out of scope).

    `descriptive` holds NORMALIZED names whose raw surface form was found
    description-shaped, because that is the form an entity's aliases are
    kept in.
    """
    left_tokens, right_tokens = name_tokens(left), name_tokens(right)
    if left_tokens < right_tokens:
        return right in descriptive
    if right_tokens < left_tokens:
        return left in descriptive
    return False


def superset_names(
    mentions: Iterable[tuple[str, str, str]]
) -> dict[tuple[str, str, str], tuple[str, ...]]:
    """The DISTINCT longer names each normalized name nests inside.

    Keyed by (book_id, kind, normalized name) and sorted, so the value is a
    function of the mention SET and never of the order it arrived in. This
    is `superset_counts` before it throws the names away: the count gates
    the ambiguity rule, and the names themselves are the candidate list
    Task 1j must offer for that bare name (see `resolve_mentions`).

    Names with no identity-bearing token are excluded on both sides — an
    empty token set is a subset of everything and must never nest.

    Token sets that are EQUAL are not listed: `john h watson m d` and
    `john watson` are the same name written two ways, not two people.

    Task 11a — and the same holds AMONG the longer names: supersets that
    share one token set are one rival, listed once under its alphabetically
    smallest spelling. Measured on A Study in Scarlet, bare `watson` nested
    inside `john watson`, `john watson m d` and `john h watson m d` — all
    {john, watson} — and counting three rivals refused the merge and split
    Watson into two nodes (110 and 56 edges). Every spelling of one token set
    merges into one entity (equal sets always nest), so any of them names it.
    """
    grouped: dict[tuple[str, str], set[str]] = {}
    for book_id, kind, normalized in mentions:
        if normalized:
            grouped.setdefault((book_id, kind), set()).add(normalized)
    found: dict[tuple[str, str, str], tuple[str, ...]] = {}
    for (book_id, kind), names in grouped.items():
        tokens = {name: name_tokens(name) for name in names}
        for name, mine in tokens.items():
            if not mine:
                continue
            rivals: dict[frozenset[str], str] = {}
            for other, theirs in tokens.items():
                if other != name and theirs and mine < theirs:
                    current = rivals.get(theirs)
                    if current is None or other < current:
                        rivals[theirs] = other
            found[(book_id, kind, name)] = tuple(sorted(rivals.values()))
    return found


def superset_counts(
    mentions: Iterable[tuple[str, str, str]]
) -> dict[tuple[str, str, str], int]:
    """How many DISTINCT longer names each normalized name nests inside.

    Keyed by (book_id, kind, normalized name), counted over the whole
    mention set — a function of the data, never of processing order.

    This is the ambiguity test the subset shortcut needs. A one-token name
    is a subset of every longer name containing it, and `normalize_name`
    strips honorifics, so a full and distinct character name can be reduced
    to a bare surname:

        normalize_name('Major Sholto')       -> 'sholto'
        normalize_name('Thaddeus Sholto')    -> 'thaddeus sholto'
        normalize_name('Bartholomew Sholto') -> 'bartholomew sholto'

    Major Sholto is the murdered father, Thaddeus and Bartholomew his sons.
    `sholto` nests inside both sons, so a subset rule that merges on the
    first match it finds fuses the family into one node and makes every
    edge on it wrong. Same shape for `smith` (Mrs. Smith vs Mordecai
    Smith), `ferrier` (John vs Lucy) and `lucy` (Lucy Ferrier vs "Lucy
    Ferrier's father").

    So a subset match may merge only when it is UNAMBIGUOUS: the shorter
    name nests inside exactly ONE longer name in that book and kind. Two or
    more and the merge is refused and the entities stay separate — under-
    merging leaves two nodes a later pass can join, over-merging destroys
    both.

    Token sets that are EQUAL are not counted: `john h watson m d` and
    `john watson` are the same name written two ways, not two people, and
    counting them would make every such pair ambiguous with itself.
    """
    return {
        key: len(names) for key, names in superset_names(mentions).items()
    }


def bare_name_of(names: Iterable[str]) -> str:
    """The shortest name of a nesting chain — the one a group is about.

    Every name reaching here nests pairwise with every other, so their
    token sets form a chain and the smallest is the bare one. Ties break
    alphabetically, so the answer is a function of the names and not of
    the order they were collected in.
    """
    return min(names, key=lambda name: (len(name_tokens(name)), name))


def unambiguous_nesting(
    left: str, right: str, book_id: str, kind: str,
    counts: dict[tuple[str, str, str], int],
) -> bool:
    """True when the shorter of two nesting names has exactly one superset.

    Equal token sets are always unambiguous — neither name is the shorter.
    A name absent from `counts` (it was never a mention in this group) is
    treated as having no supersets, so it cannot be the ambiguous side.
    """
    left_tokens, right_tokens = name_tokens(left), name_tokens(right)
    if left_tokens < right_tokens:
        shorter = left
    elif right_tokens < left_tokens:
        shorter = right
    else:
        return True
    return counts.get((book_id, kind, shorter), 0) <= 1


def nests_with_entity(
    normalized: str, aliases: Iterable[str], book_id: str, kind: str,
    counts: dict[tuple[str, str, str], int],
) -> bool:
    """True when a name is comparable with an entity's WHOLE alias set.

    The strict rule is that the name nests with EVERY alias the entity
    carries, so an entity never becomes a bridge between two names that do
    not nest with each other (`mary` + `mary morstan`, then `morstan`
    fusing the daughter with her father).

    That rule is too strict in one measured shape. A bare GIVEN name that
    has one superset merges into it first — `jonathan` into `jonathan
    small`, because it sorts first — and the entity then carries
    {jonathan, jonathan small}. The bare surname `small`, which also has
    exactly one superset, nests inside `jonathan small` but not inside
    `jonathan`, so it was dropped from the comparison silently (no merge, no
    refusal, so it was never queued either) and The Sign of the Four ended
    with `Small` (60 edges) and `Jonathan Small` as two people.

    The relaxation: the aliases the name does NOT nest with may be skipped
    when every one of them is a sub-name of one ANCHOR alias the name is
    also a sub-name of, and each has that anchor as its ONLY superset. Then
    they and the name are all short forms of one full name, and nothing
    links two different people.

    This only decides whether the entity is COMPARED. Whether the name then
    merges is still the ambiguity rule's call (`unambiguous_nesting`, against
    the anchor), so `sholto` — two sons — meeting an entity that holds
    {thaddeus, thaddeus sholto} is now a refusal that is asked, not a
    silent skip that the similarity path could undo. Anything outside the
    shape keeps the strict rule: an alias with two supersets, or one that
    lost a token to `name_tokens` (`No. 3` and `No. 7` are both {no}; bridging
    through them would pull the street `Pinchin Lane` into the house `No. 3,
    Pinchin Lane`).
    """
    mine = name_tokens(normalized)
    alias_list = list(aliases)
    loose = [
        alias for alias in alias_list
        if not tokens_nest(mine, name_tokens(alias))
    ]
    if not loose:
        return True
    if any(
        len(alias.split()) != len(name_tokens(alias))
        or counts.get((book_id, kind, alias), 0) > 1
        for alias in loose
    ):
        return False
    return any(
        mine < name_tokens(anchor)
        and all(name_tokens(alias) < name_tokens(anchor) for alias in loose)
        for anchor in alias_list
    )


def display_name(candidates: Iterable[str]) -> str:
    """The longest surface form, ties broken alphabetically.

    Content-derived, not order-derived: "Elizabeth Bennet" beats "Miss
    Bennet" and "Lizzy" on length, and a same-length tie ("THE CAPTAIN"
    vs "the captain") resolves the same way regardless of input order.

    One rule, one place: `resolve_mentions` picks an entity's display name
    with it, and `lore.graph.loader` applies it again when merging with a
    name a previous load already put on the node.
    """
    return sorted(candidates, key=lambda s: (-len(s), s))[0]


def entity_id_chain(normalized: str, kind: str) -> Iterator[str]:
    """The deterministic collision chain for a (normalized name, kind).

    `entity_id` is a pure hash of (normalized_name, kind), so a refused
    merge — two "the Captain"s in different books, or two same-named
    mentions too dissimilar to merge — must not silently collide with, and
    overwrite, the existing entity of that same name and kind. This yields
    `id(n), id(n#1), id(n#2), …`: the same id every time, for the same
    name, at the same position in the chain.

    Two callers walk it: `resolve_mentions` below, for a refusal inside one
    resolution pass, and `lore.graph.loader`, for a refusal against an
    entity another book already persisted. One scheme, one place.
    """
    yield entity_id(normalized, kind)
    suffix = 0
    while True:
        suffix += 1
        yield entity_id(f"{normalized}#{suffix}", kind)


def disambiguated_entity_id(
    normalized: str, kind: str, taken: Container[str]
) -> str:
    """The first id in the collision chain that nothing has claimed yet."""
    return next(
        candidate
        for candidate in entity_id_chain(normalized, kind)
        if candidate not in taken
    )


async def resolve_mentions(
    mentions: list[Mention],
    embed: Embed,
    adjudicate: Adjudicate,
    *,
    same_book_floor: float = 0.86,
    cross_book_floor: float = 0.92,
    reject_floor: float = 0.70,
    record_ambiguous: RecordAmbiguous | None = None,
    decide_ambiguous: DecideAmbiguous | None = None,
) -> ResolutionResult:
    result = ResolutionResult()
    # Process mentions in a canonical order (not input order) so the result
    # — including the disambiguation suffix on a refused merge, below — is a
    # function of the mention SET, never of the sequence they happen to
    # arrive in. Without this, which entity gets the "bare" id and which
    # gets a "#1" suffix depends on read order, and a rebuild from the same
    # data in a different order would produce different ids.
    ordered_mentions = sorted(
        mentions,
        key=lambda m: (m.book_id, normalize_name(m.name), m.kind, m.context),
    )
    # How many distinct longer names each name nests inside — the ambiguity
    # gate on pass 2a below. Computed over the whole mention set up front,
    # because the streaming loop cannot see it: a bare surname sorts BEFORE
    # every full name containing it, so at the moment it is processed none
    # of its rivals exist yet and the first one to arrive would win it.
    #
    # Task 1j needs the NAMES, not just the count: a grouped question has
    # to offer every rival the bare name has, and the count is that list's
    # length.
    superset_map = superset_names(
        (m.book_id, m.kind, normalize_name(m.name)) for m in ordered_mentions
    )
    nesting_counts = {key: len(names) for key, names in superset_map.items()}
    # Task 1k — the normalized names whose RAW surface form is a definite
    # description ("boarding-house at Camberwell", "Lucy Ferrier's father").
    # Collected over the whole mention set, like the nesting counts above,
    # so the answer is a property of the DATA and not of which mention of a
    # name happens to be in hand: two raw forms can normalize to one name,
    # and if either of them is a description the name is treated as one —
    # the conservative direction, since the guard can only under-merge.
    descriptive_names = {
        normalize_name(m.name)
        for m in ordered_mentions
        if is_description_shaped(m.name)
    }

    # Surface form and evidence per normalized name, over the whole mention
    # set — so a grouped question can name a rival that does not exist as
    # an entity yet. A bare surname sorts BEFORE the full names containing
    # it, so at the moment it is processed most of its rivals have not been
    # created; asking about only the ones that happen to exist is what
    # makes the question pairwise, and pairwise is wrong (see below).
    raw_by_name: dict[tuple[str, str, str], set[str]] = {}
    evidence_by_name: dict[tuple[str, str, str], str] = {}
    for mention in ordered_mentions:
        normalized = normalize_name(mention.name)
        if not normalized:
            continue
        name_key = (mention.book_id, mention.kind, normalized)
        raw_by_name.setdefault(name_key, set()).add(mention.name)
        seen_evidence = evidence_by_name.get(name_key)
        if seen_evidence is None or mention.context < seen_evidence:
            evidence_by_name[name_key] = mention.context
    display_by_name = {
        name_key: display_name(raws) for name_key, raws in raw_by_name.items()
    }

    def grouped_question(bare: str, kind: str, book_id: str) -> Question:
        """The one question a bare name deserves: ALL of its rivals.

        Asking pairwise ("is bare `Sholto` Thaddeus?", then "is bare
        `Sholto` Bartholomew?") is not a smaller version of this — it is a
        different and wrong question. Measured against the live model:
        every pairwise answer came back Yes, because a bare surname really
        could be either man, and nothing in a pairwise question can see
        that it is being asked about two. The aggregate of those answers
        merges the murdered father into both his sons — exactly the fusion
        the ambiguity refusal exists to prevent. One question, every
        candidate, one answer.
        """
        key = (book_id, kind, bare)
        return Question(
            mention=display_by_name.get(key, bare),
            kind=kind,
            book_id=book_id,
            evidence=evidence_by_name.get(key, ""),
            candidates=tuple(
                Candidate(
                    id=entity_id(rival, kind),
                    name=display_by_name.get((book_id, kind, rival), rival),
                )
                for rival in superset_map.get(key, ())
            ),
        )

    # One grouped question per (book, kind, bare name), however many
    # pairwise refusals it took to discover it.
    asked: set[str] = set()

    # Working state, keyed by canonical id.
    names: dict[str, str] = {}
    kinds: dict[str, str] = {}
    contexts: dict[str, str] = {}
    alias_sets: dict[str, set[str]] = {}
    # The union of `name_tokens` over an entity's aliases, maintained in
    # step with `alias_sets` so the pass-2 gate below is a set lookup.
    alias_tokens: dict[str, set[str]] = {}
    books: dict[str, set[str]] = {}
    vectors: dict[str, list[float]] = {}  # lazily populated cache
    raw_names: dict[str, set[str]] = {}  # every raw surface form seen, for display
    # Task 11a: every title an entity's surface forms carry ("major", "mr").
    titles: dict[str, set[str]] = {}

    for mention in ordered_mentions:
        normalized = normalize_name(mention.name)
        if not normalized:
            # A mention with no word characters at all mints no entity.
            continue
        mention_tokens = name_tokens(normalized)
        key = (mention.name, mention.kind, mention.book_id)
        mention_titles = leading_titles(mention.name)

        def title_conflict(cid: str) -> bool:
            """Task 11a — this surface form and the entity carry only
            DIFFERENT titles, so they are not one person by name alone.

            `normalize_name` strips titles, so "Major Sholto" and "Mr.
            Sholto" are one name, and every decision about `sholto` used to
            apply to both. Measured on the two novels, exactly three names
            carry two different titles and each is two people: Major/Mr.
            Sholto (the father, and a son: "Tonga KILLS Major John Sholto"
            was Bartholomew), Captain/Miss Morstan (father and daughter),
            Mr./Mrs. Smith. An untitled form conflicts with nothing, so
            "Sholto" and "Holmes" still join by name.

            Applied at every NAME-based door — pass 1, the pass-2a subset
            shortcut, a disambiguation answer — and to the similarity path
            too, for the reason the ambiguity refusal is: an entity and its
            own surname share evidence, so the embedding would bring the
            refused merge straight back.
            """
            theirs = titles[cid]
            return bool(mention_titles) and bool(theirs) and not (
                mention_titles & theirs
            )

        # Pass 1 — exact or alias match, same kind, same book. Restricted to
        # a book the canonical already spans: a normalized-name match across
        # books ("the Captain" in two novels) must not skip adjudication.
        hit = next(
            (
                cid
                for cid, aliases in alias_sets.items()
                if kinds[cid] == mention.kind
                and mention.book_id in books[cid]
                and normalized in aliases
                and not title_conflict(cid)
            ),
            None,
        )
        if hit is not None:
            raw_names[hit].add(mention.name)
            titles[hit] |= mention_titles
            result.mention_map[key] = hit
            continue

        # Pass-2 name gate. Pass 2 embeds "{name} — {evidence sentence}" and
        # compares those vectors, so when two entities are named in one
        # sentence their texts differ only by a short prefix against a long
        # shared body and the sentence dominates the vector. Measured
        # against the live nomic-embed-text: "Holmes" vs "Drebber" sharing
        # an evidence sentence scored 0.9135 and merged at the 0.86
        # same-book floor, while "Holmes" vs "Holmes" in two different
        # sentences scored 0.7110 and did not. The metric measures
        # co-occurrence, not identity, and the bands are inverted — so no
        # threshold separates them and raising same_book_floor cannot fix
        # it. Context may only DISAMBIGUATE names that already look
        # related; it must never be able to CREATE a merge between
        # unrelated names. A candidate therefore has to share an
        # identity-bearing token with the mention before its similarity is
        # consulted at all — which also means an unrelated candidate costs
        # no embedding call. Do not "simplify" this away.
        candidates = [
            cid
            for cid in alias_sets
            if kinds[cid] == mention.kind and alias_tokens[cid] & mention_tokens
            and not title_conflict(cid)
        ]
        chosen: str | None = None
        vector: list[float] | None = None

        # Pass 2a — the name-variant shortcut, decided by string matching.
        # If one name's identity tokens are a subset of the other's, they
        # are the same entity: "holmes" ⊂ "sherlock holmes", "watson" ⊂
        # "john watson". This is not a judgement call, so it deliberately
        # takes the decision away from the local 3B, which two rounds of
        # prompt tuning showed cannot be relied on for it — at HEAD the
        # live model answered False to both 'Holmes'/'Sherlock Holmes' and
        # 'Watson'/'Dr. Watson' while correctly answering False to
        # 'John Ferrier'/'John Watson'. The subset test answers all three
        # exactly, every time, for free, and it separates every real case
        # in the corpus: `sierra blanco`/`sierra nevada`,
        # `bond street`/`miles street` and `john ferrier`/`john rance`
        # share a token but nest in neither direction, so they fall
        # through to the similarity path below, unchanged.
        #
        # The rule is pairwise, so lifting it to an entity — which holds a
        # SET of names — has to nest with EVERY alias the entity already
        # carries, not merely with one of them or with their union.
        # Otherwise a merged entity becomes a bridge between two names that
        # never nested with each other, and one merge chains into a second:
        # measured on the live corpus, `mary` merged with `mary morstan`,
        # after which `morstan` nested with the union and fused Mary
        # Morstan with her father Captain Morstan. `bartholomew` +
        # `bartholomew sholto` then swallowing `sholto` (Major Sholto,
        # another father) was the same chain. Requiring every alias blocks
        # both — `{morstan}` does not nest with the alias `mary` — while
        # still admitting every merge this task exists for, because those
        # entities are single-alias at the moment the shortcut fires.
        #
        # SAME BOOK ONLY. A cross-book merge is still never made without
        # `adjudicate` returning True — Phase 2B's whole point is that
        # "the Captain" in two unrelated novels must not merge silently,
        # and only a judgement call separates that from a real recurring
        # character. Cross-book candidates therefore fall through below.
        #
        # Placed before the embedding batch is built, so a subset match
        # costs no embedding call at all.
        #
        # AMBIGUITY. A nesting pair is evidence of one entity only when the
        # shorter name has exactly ONE longer name to nest inside. `sholto`
        # nests inside both `thaddeus sholto` and `bartholomew sholto` —
        # two sons and their murdered father — and merging on whichever
        # arrived first would fuse the family. When the nesting is
        # ambiguous the mention is refused outright: it does not fall
        # through to the similarity path, because that path would happily
        # merge it too (an entity and its own surname share an evidence
        # sentence, and the embedding measures co-occurrence). Under-merging
        # leaves nodes a later pass can join; over-merging destroys both.
        nests = [
            cid
            for cid in candidates
            if mention.book_id in books[cid]
            and nests_with_entity(
                normalized, alias_sets[cid], mention.book_id, mention.kind,
                nesting_counts,
            )
        ]
        # Task 1k — a description may not be the TARGET of a subset merge.
        # `Camberwell` nests inside `boarding-house at Camberwell` and
        # `lucy ferrier` inside "Lucy Ferrier's father", and on the rebuilt
        # graph the description won both, leaving no node for the real place
        # or the real character. Declining is not the same as refusing an
        # ambiguous nesting: there is no question to ask (Task 1j's residue
        # is about which of two people a bare surname is), so these
        # candidates are dropped silently.
        #
        # Dropped from the SIMILARITY path as well, for the same reason the
        # ambiguity refusal is: a description and the name inside it share
        # an evidence sentence, and the pass-2 embedding measures
        # co-occurrence, so leaving the candidate in would let the very
        # merge just declined come straight back through the other door.
        declined = {
            cid
            for cid in nests
            if any(
                swallows_a_name(normalized, alias, descriptive_names)
                for alias in alias_sets[cid]
            )
        }
        if declined:
            nests = [cid for cid in nests if cid not in declined]
            candidates = [cid for cid in candidates if cid not in declined]
        subset_matches = [
            cid
            for cid in nests
            if all(
                unambiguous_nesting(
                    normalized, alias, mention.book_id, mention.kind,
                    nesting_counts,
                )
                for alias in alias_sets[cid]
            )
        ]
        # An ambiguous nesting candidate is dropped from the similarity pass
        # as well, not merely from the shortcut: an entity and its own
        # surname share an evidence sentence, and the embedding measures
        # co-occurrence, so leaving it in would let the very merge just
        # refused come back through the other door. Candidates that never
        # nested are untouched — the ambiguity is about one pair of names,
        # not about this mention.
        refused = {cid for cid in nests if cid not in subset_matches}
        if refused:
            candidates = [cid for cid in candidates if cid not in refused]

        # Task 1j — the residue. A refusal here is the ONE class of merge
        # no rule can settle: the bare surname really might be either son,
        # or the father who is neither. It is recorded as a question, and
        # if a previous `lore disambiguate` already paid for an answer,
        # that answer is honoured. Both hooks are optional; with neither
        # wired this block records nothing, decides nothing, and the
        # refusal stands exactly as Task 1i left it.
        #
        # ASK ONCE PER BARE NAME, NEVER PAIRWISE. The refusals arrive one
        # pair at a time and from both directions, but the question is
        # about the bare name and all of its rivals at once. Measured
        # against the live model, the pairwise form is not a smaller
        # version of the grouped one — it is a different and wrong
        # question: `Sholto` vs `Major Sholto` -> Yes AND `Sholto` vs
        # `Thaddeus Sholto` -> Yes, each individually reasonable, together
        # fusing a murdered father with his son. Nothing in a pairwise
        # question can see that it is one of several. So the refusals are
        # grouped by their bare name and each group is asked once, with
        # every rival offered and exactly one answer accepted.
        verdict: str | None = None
        if refused and not subset_matches and (
            record_ambiguous is not None or decide_ambiguous is not None
        ):
            groups: dict[str, list[str]] = {}
            for cid in sorted(refused):
                # Only the aliases this name actually NESTS with decide what
                # the question is about: a bridged entity also holds names
                # that are unrelated to it (`arthur` beside `charpentier`),
                # and the shortest of ALL of them would ask about the wrong
                # bare name.
                groups.setdefault(
                    bare_name_of({normalized, *(
                        alias for alias in alias_sets[cid]
                        if tokens_nest(mention_tokens, name_tokens(alias))
                    )}), []
                ).append(cid)

            for bare in sorted(groups):
                question = grouped_question(bare, mention.kind, mention.book_id)
                if record_ambiguous is not None and question.key not in asked:
                    asked.add(question.key)
                    record_ambiguous(question)
                if decide_ambiguous is None or verdict is not None:
                    continue
                answer = decide_ambiguous(question)
                # A stale cache entry — written before a re-extraction
                # changed the entity set — must be ignored, never allowed
                # to fuse an arbitrary pair of entities.
                if not answer or answer not in {
                    candidate.id for candidate in question.candidates
                }:
                    continue
                # The answer names ONE rival. It applies to this pair only
                # if that rival is the non-bare side of THIS pair, which is
                # what stops one grouped answer from merging the bare name
                # into two different people: every other rival sees an
                # answer that is not about it, and refuses exactly as
                # before.
                for cid in groups[bare]:
                    picked = (
                        answer in {
                            entity_id(alias, mention.kind)
                            for alias in alias_sets[cid]
                        }
                        if normalized == bare
                        else answer == entity_id(normalized, mention.kind)
                    )
                    if picked:
                        verdict = cid
                        break

        if subset_matches:
            # Prefer the most specific match (the largest shared token set;
            # one side nests in the other, so that is the larger of the two
            # names), and break a tie on the content-derived id so the
            # choice stays a function of the mention set, not of iteration
            # order.
            chosen = min(
                subset_matches,
                key=lambda cid: (-len(alias_tokens[cid] & mention_tokens), cid),
            )
        elif verdict is not None:
            chosen = verdict
        elif candidates:
            # Pass 2 — embed this mention plus any not-yet-cached candidates,
            # in one batch, then compare against same-kind candidates.
            uncached = [cid for cid in candidates if cid not in vectors]
            texts = [f"{mention.name} — {mention.context}"] + [
                f"{names[cid]} — {contexts[cid]}" for cid in uncached
            ]
            embedded = await embed(texts)
            vector = embedded[0]
            for cid, vec in zip(uncached, embedded[1:]):
                vectors[cid] = vec

            scored = sorted(
                ((cosine(vector, vectors[cid]), cid) for cid in candidates),
                reverse=True,
            )
            score, best = scored[0]
            if score >= reject_floor:
                same_book = mention.book_id in books[best]
                floor = same_book_floor if same_book else cross_book_floor
                candidate_entity = CanonicalEntity(
                    id=best, name=display_name(raw_names[best]), kind=kinds[best],
                    aliases=tuple(sorted(alias_sets[best])),
                    book_ids=tuple(sorted(books[best])),
                )
                if same_book and score >= floor:
                    chosen = best
                # Pass 3 — the gray band, and every cross-book candidate
                # (even one at or above cross_book_floor: a cross-book merge
                # is never made without adjudication returning True).
                elif await adjudicate(candidate_entity, mention):
                    chosen = best

        if chosen is None:
            # A refused merge must not silently collide with — and
            # overwrite — the existing entity of that same name and kind.
            chosen = disambiguated_entity_id(normalized, mention.kind, alias_sets)
            names[chosen] = mention.name
            kinds[chosen] = mention.kind
            contexts[chosen] = mention.context
            alias_sets[chosen] = {normalized}
            alias_tokens[chosen] = set(mention_tokens)
            books[chosen] = {mention.book_id}
            raw_names[chosen] = {mention.name}
            titles[chosen] = set(mention_titles)
            if vector is not None:
                # Reuse the vector already computed above instead of an
                # extra embed call; entities created with no candidates to
                # compare against are cached lazily, on first future use.
                vectors[chosen] = vector
        else:
            # A known alias resurfacing in another book (pass 1 is
            # book-scoped, so it lands here rather than short-circuiting)
            # must not be recorded twice — only the book membership grows.
            is_new_alias = normalized not in alias_sets[chosen]
            alias_sets[chosen].add(normalized)
            alias_tokens[chosen] |= mention_tokens
            books[chosen].add(mention.book_id)
            raw_names[chosen].add(mention.name)
            titles[chosen] |= mention_titles
            if is_new_alias:
                result.aliases.append(
                    AliasRecord(
                        alias_id=entity_id(normalized, mention.kind),
                        alias_name=mention.name,
                        kind=mention.kind,
                        canonical_id=chosen,
                    )
                )
        result.mention_map[key] = chosen

    result.entities = [
        CanonicalEntity(
            id=cid,
            # The display name is picked from content, not from whichever
            # mention happened to create or be processed for this entity.
            name=display_name(raw_names[cid]),
            kind=kinds[cid],
            aliases=tuple(sorted(alias_sets[cid])),
            book_ids=tuple(sorted(books[cid])),
        )
        for cid in alias_sets
    ]
    return result
