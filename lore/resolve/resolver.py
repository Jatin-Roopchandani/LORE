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

from collections.abc import Awaitable, Callable
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
    books: dict[str, set[str]] = {}
    vectors: dict[str, list[float]] = {}  # lazily populated cache
    raw_names: dict[str, set[str]] = {}  # every raw surface form seen, for display

    def display_name(cid: str) -> str:
        # Content-derived, not order-derived: the longest raw surface form
        # seen for this entity, ties broken alphabetically. See the note at
        # the final entities list below for why this matters.
        return sorted(raw_names[cid], key=lambda s: (-len(s), s))[0]

    for mention in ordered_mentions:
        normalized = normalize_name(mention.name)
        if not normalized:
            # A mention with no word characters at all mints no entity.
            continue
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

        candidates = [cid for cid in alias_sets if kinds[cid] == mention.kind]
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
                    id=best, name=display_name(best), kind=kinds[best],
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
            # entity_id is a pure hash of (normalized_name, kind), so a
            # refused merge (e.g. two "the Captain"s in different books, or
            # two same-named mentions too dissimilar to merge) must not
            # silently collide with — and overwrite — the existing entity
            # of that same name and kind. Disambiguate deterministically.
            chosen = entity_id(normalized, mention.kind)
            suffix = 0
            while chosen in alias_sets:
                suffix += 1
                chosen = entity_id(f"{normalized}#{suffix}", mention.kind)
            names[chosen] = mention.name
            kinds[chosen] = mention.kind
            contexts[chosen] = mention.context
            alias_sets[chosen] = {normalized}
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
            # mention happened to create or be processed for this entity:
            # the longest raw surface form seen, ties broken alphabetically.
            # "Elizabeth Bennet" beats "Miss Bennet" and "Lizzy" on length;
            # a same-length tie ("THE CAPTAIN" vs "the captain") resolves
            # the same way regardless of input order.
            name=display_name(cid),
            kind=kinds[cid],
            aliases=tuple(sorted(alias_sets[cid])),
            book_ids=tuple(sorted(books[cid])),
        )
        for cid in alias_sets
    ]
    return result
