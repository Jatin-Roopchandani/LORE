"""Three-pass entity resolution: the "Voldemort ≡ The Dark Lord" problem.

Pass 1 is exact/alias matching on the normalized name — free, and it
handles most of a book. Pass 2 embeds name plus local context and compares
against same-kind candidates. Pass 3 asks an LLM, and only for the
ambiguous middle. A merge across two different books ALWAYS requires
pass 3 to approve it, so "the Captain" in two unrelated novels stays two
people.

Embeddings are computed lazily and cached: a brand-new entity with no
existing same-kind candidates never touches the embedder (nothing to
compare it to yet). Its representative name/context is kept so that if a
*later* mention needs to compare against it, that comparison batches the
mention's own text with any not-yet-cached candidate texts into a single
embed call.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Container, Iterable, Iterator
from dataclasses import dataclass, field

from lore.resolve.normalize import entity_id, normalize_name
from lore.resolve.predicates import cosine

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]


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

    for mention in ordered_mentions:
        normalized = normalize_name(mention.name)
        if not normalized:
            # A mention with no word characters at all mints no entity.
            continue
        mention_tokens = name_tokens(normalized)
        key = (mention.name, mention.kind, mention.book_id)

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
            ),
            None,
        )
        if hit is not None:
            raw_names[hit].add(mention.name)
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
        ]
        chosen: str | None = None
        vector: list[float] | None = None

        if candidates:
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
