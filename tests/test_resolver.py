import pytest

from lore.resolve.resolver import Mention, resolve_mentions


def _embedder(vectors: dict[str, list[float]]):
    async def embed(texts):
        return [vectors.get(t.split(" — ")[0], [0.0, 0.0, 1.0]) for t in texts]

    return embed


async def _never(canonical, mention) -> bool:
    return False


async def _always(canonical, mention) -> bool:
    return True


@pytest.mark.asyncio
async def test_exact_and_honorific_forms_collapse_without_embedding() -> None:
    calls: list[list[str]] = []

    async def embed(texts):
        calls.append(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    result = await resolve_mentions(
        [
            Mention("Elizabeth Bennet", "Character", "b1", "she laughed"),
            Mention("Miss Bennet", "Character", "b1", "she laughed"),
        ],
        embed,
        _never,
    )
    # "miss bennet" normalizes to "bennet", not "elizabeth bennet" — so this
    # pair does NOT collapse on pass 1. It reaches pass 2, where the
    # token-subset shortcut ("bennet" nests inside "elizabeth bennet")
    # settles it deterministically: one entity, and still no embedding.
    # Before that shortcut existed this pair cost an embedding call, which
    # is what this test used to assert.
    assert len(result.entities) == 1
    assert result.entities[0].aliases == ("bennet", "elizabeth bennet")
    assert calls == [], "a subset match is settled by string matching alone"


@pytest.mark.asyncio
async def test_identical_names_collapse_on_pass_one() -> None:
    calls: list[list[str]] = []

    async def embed(texts):
        calls.append(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    result = await resolve_mentions(
        [
            Mention("Mr. Darcy", "Character", "b1", "he bowed"),
            Mention("Darcy", "Character", "b1", "he rode away"),
        ],
        embed,
        _never,
    )
    assert len(result.entities) == 1
    assert calls == []  # pass 1 handled both


@pytest.mark.asyncio
async def test_same_book_similar_names_merge_on_embedding() -> None:
    embed = _embedder(
        {"Lizzy Bennet": [1.0, 0.0, 0.0], "Elizabeth Bennet": [1.0, 0.0, 0.0]}
    )
    result = await resolve_mentions(
        [
            Mention("Elizabeth Bennet", "Character", "b1", "she laughed"),
            Mention("Lizzy Bennet", "Character", "b1", "she laughed"),
        ],
        embed,
        _never,  # adjudication would say no; same-book similarity is enough
    )
    assert len(result.entities) == 1
    assert set(result.entities[0].aliases) == {"elizabeth bennet", "lizzy bennet"}
    assert len(result.aliases) == 1


@pytest.mark.asyncio
async def test_dissimilar_names_stay_separate() -> None:
    embed = _embedder({"Darcy": [1.0, 0.0, 0.0], "Wickham": [0.0, 1.0, 0.0]})
    result = await resolve_mentions(
        [
            Mention("Darcy", "Character", "b1", "he bowed"),
            Mention("Wickham", "Character", "b1", "he lied"),
        ],
        embed,
        _always,  # even a yes-man adjudicator must not be consulted
    )
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_cross_book_merge_requires_adjudication() -> None:
    embed = _embedder({"the Captain": [1.0, 0.0, 0.0]})
    refused = await resolve_mentions(
        [
            Mention("the Captain", "Character", "b1", "he shouted"),
            Mention("the Captain", "Character", "b2", "he wept"),
        ],
        embed,
        _never,
    )
    assert len(refused.entities) == 2, "different books must not fuse silently"

    approved = await resolve_mentions(
        [
            Mention("the Captain", "Character", "b1", "he shouted"),
            Mention("the Captain", "Character", "b2", "he wept"),
        ],
        embed,
        _always,
    )
    assert len(approved.entities) == 1
    assert set(approved.entities[0].book_ids) == {"b1", "b2"}


@pytest.mark.asyncio
async def test_different_kinds_never_merge() -> None:
    embed = _embedder({"Netherfield": [1.0, 0.0, 0.0]})
    result = await resolve_mentions(
        [
            Mention("Netherfield", "Place", "b1", "the estate"),
            Mention("Netherfield", "Faction", "b1", "the household"),
        ],
        embed,
        _always,
    )
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_mention_map_points_every_mention_at_an_entity() -> None:
    embed = _embedder({"Darcy": [1.0, 0.0, 0.0], "Wickham": [0.0, 1.0, 0.0]})
    mentions = [
        Mention("Darcy", "Character", "b1", "he bowed"),
        Mention("Wickham", "Character", "b1", "he lied"),
    ]
    result = await resolve_mentions(mentions, embed, _never)
    ids = {result.mention_map[(m.name, m.kind, m.book_id)] for m in mentions}
    assert len(ids) == 2
    assert all(i.startswith("e") for i in ids)


@pytest.mark.asyncio
async def test_mention_with_empty_normalized_name_mints_no_entity() -> None:
    async def embed(texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    result = await resolve_mentions(
        [Mention("...", "Character", "b1", "nothing here")],
        embed,
        _never,
    )
    assert result.entities == []
    assert result.aliases == []
    assert result.mention_map == {}


@pytest.mark.asyncio
async def test_entity_ids_do_not_depend_on_mention_order() -> None:
    embed = _embedder({"the Captain": [1.0, 0.0, 0.0]})
    mentions = [
        Mention("the Captain", "Character", "b1", "he shouted"),
        Mention("the Captain", "Character", "b2", "he wept"),
        Mention("the Captain", "Character", "b3", "he sang"),
    ]
    forward = await resolve_mentions(list(mentions), embed, _never)
    backward = await resolve_mentions(list(reversed(mentions)), embed, _never)
    assert {e.id for e in forward.entities} == {e.id for e in backward.entities}
    assert forward.mention_map == backward.mention_map


@pytest.mark.asyncio
async def test_recurring_alias_is_recorded_once() -> None:
    embed = _embedder({
        "Elizabeth Bennet": [1.0, 0.0, 0.0],
        "Lizzy Bennet": [1.0, 0.0, 0.0],
    })
    result = await resolve_mentions(
        [
            Mention("Elizabeth Bennet", "Character", "b1", "she laughed"),
            Mention("Lizzy Bennet", "Character", "b1", "she laughed"),
            Mention("Lizzy Bennet", "Character", "b2", "she laughed"),
        ],
        embed,
        _always,
    )
    lizzy_records = [a for a in result.aliases if a.alias_name == "Lizzy Bennet"]
    assert len(lizzy_records) == 1


@pytest.mark.asyncio
async def test_canonical_name_is_the_fullest_surface_form() -> None:
    """Task 1i changed the entity COUNT here, not the naming rule it asserts.

    `Miss Bennet` normalizes to the bare `bennet`, which nests inside BOTH
    `elizabeth bennet` and `lizzy bennet`. That is now an ambiguous subset
    and merges with neither — the same shape as `sholto` inside two Sholto
    sons, which is what the rule exists for. (It is also right for the
    novel: Miss Bennet is Jane, the eldest, not Elizabeth.) The two full
    names still merge on similarity, and the fullest surface form still
    wins the display name, which is what this test is for.
    """
    embed = _embedder({
        "Elizabeth Bennet": [1.0, 0.0, 0.0],
        "Miss Bennet": [1.0, 0.0, 0.0],
        "Lizzy Bennet": [1.0, 0.0, 0.0],
    })
    for order in ("forward", "reverse"):
        mentions = [
            Mention("Miss Bennet", "Character", "b1", "she laughed"),
            Mention("Elizabeth Bennet", "Character", "b1", "she laughed"),
            Mention("Lizzy Bennet", "Character", "b1", "she laughed"),
        ]
        if order == "reverse":
            mentions.reverse()
        result = await resolve_mentions(mentions, embed, _always)
        assert len(result.entities) == 2
        merged = max(result.entities, key=lambda e: len(e.aliases))
        assert merged.name == "Elizabeth Bennet"
        assert merged.aliases == ("elizabeth bennet", "lizzy bennet")


# --- Pass-2 name gate -------------------------------------------------------
#
# Every test below uses an embedder that returns the SAME vector for every
# text, so cosine similarity is 1.0 for every pair and cannot be what keeps
# two entities apart. If one of these passes, it is the name gate that made
# it pass, not the similarity score.


def _identical(calls: list[list[str]] | None = None):
    async def embed(texts):
        if calls is not None:
            calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    return embed


@pytest.mark.asyncio
async def test_co_occurring_strangers_do_not_merge() -> None:
    # Two different characters named in the SAME evidence sentence. Their
    # pass-2 embedding texts differ only by a short name prefix against a
    # long shared body, so context similarity alone rates them ~0.91 and
    # merges them. Names sharing no token must never be candidates.
    shared = "The two detectives examined the body in the empty house."
    mentions = [
        Mention(name="Holmes", kind="Character", book_id="b1", context=shared),
        Mention(name="Drebber", kind="Character", book_id="b1", context=shared),
    ]

    async def adjudicate(canonical, mention):
        raise AssertionError("unrelated names must not reach adjudication")

    result = await resolve_mentions(mentions, _identical(), adjudicate)
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_gated_out_candidate_costs_no_embedding_call() -> None:
    shared = "The two detectives examined the body in the empty house."
    calls: list[list[str]] = []
    result = await resolve_mentions(
        [
            Mention("Holmes", "Character", "b1", shared),
            Mention("Drebber", "Character", "b1", shared),
        ],
        _identical(calls),
        _always,
    )
    assert len(result.entities) == 2
    assert calls == [], "no qualifying candidate means nothing to embed"


@pytest.mark.asyncio
async def test_shared_token_still_merges_on_context() -> None:
    # Pass 2 must still do the job it exists for: name variants.
    shared = "He turned to the window and considered the case."
    result = await resolve_mentions(
        [
            Mention("Sherlock Holmes", "Character", "b1", shared),
            Mention("Holmes", "Character", "b1", shared),
        ],
        _identical(),
        _never,  # adjudication would refuse; same-book similarity is enough
    )
    assert len(result.entities) == 1
    assert set(result.entities[0].aliases) == {"sherlock holmes", "holmes"}


@pytest.mark.asyncio
async def test_honorific_variant_merges_but_rhyming_stranger_does_not() -> None:
    shared = "He read the note aloud in the sitting room."
    merged = await resolve_mentions(
        [
            Mention("Dr. Watson", "Character", "b1", shared),
            Mention("Watson", "Character", "b1", shared),
        ],
        _identical(),
        _never,
    )
    assert len(merged.entities) == 1

    split = await resolve_mentions(
        [
            Mention("Watson", "Character", "b1", shared),
            Mention("Wilson", "Character", "b1", shared),
        ],
        _identical(),
        _always,  # even a yes-man adjudicator must not be reachable
    )
    assert len(split.entities) == 2


@pytest.mark.asyncio
async def test_three_co_occurring_strangers_stay_three() -> None:
    shared = "Holmes, Drebber and Stangerson were all in that room."
    result = await resolve_mentions(
        [
            Mention("Holmes", "Character", "b1", shared),
            Mention("Drebber", "Character", "b1", shared),
            Mention("Stangerson", "Character", "b1", shared),
        ],
        _identical(),
        _always,
    )
    assert len(result.entities) == 3


@pytest.mark.asyncio
async def test_a_dog_does_not_become_a_detective() -> None:
    # The live corpus merged book two's "Holmes" with "Toby", a dog. A
    # one-token name shares nothing with an unrelated name of any length.
    shared = "Toby led Holmes down the lane at a steady trot."
    result = await resolve_mentions(
        [
            Mention("Holmes", "Character", "b1", shared),
            Mention("Toby", "Character", "b1", shared),
        ],
        _identical(),
        _always,
    )
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_cross_book_shared_token_still_reaches_adjudication() -> None:
    # The Phase 2B guarantee: a cross-book merge is never made without
    # `adjudicate` returning True, even at similarity 1.0.
    seen: list[tuple[str, str]] = []

    async def watching_never(canonical, mention):
        seen.append((canonical.name, mention.name))
        return False

    refused = await resolve_mentions(
        [
            Mention("Sherlock Holmes", "Character", "b1", "he smiled"),
            Mention("Holmes", "Character", "b2", "he frowned"),
        ],
        _identical(),
        watching_never,
    )
    assert seen, "a shared-token cross-book pair must reach adjudication"
    assert len(refused.entities) == 2

    approved = await resolve_mentions(
        [
            Mention("Sherlock Holmes", "Character", "b1", "he smiled"),
            Mention("Holmes", "Character", "b2", "he frowned"),
        ],
        _identical(),
        _always,
    )
    assert len(approved.entities) == 1
    assert set(approved.entities[0].book_ids) == {"b1", "b2"}


@pytest.mark.asyncio
async def test_case_and_punctuation_do_not_defeat_the_gate() -> None:
    result = await resolve_mentions(
        [
            Mention("MR. SHERLOCK HOLMES", "Character", "b1", "he rose"),
            Mention("Holmes", "Character", "b1", "he rose"),
        ],
        _identical(),
        _never,
    )
    assert len(result.entities) == 1


@pytest.mark.asyncio
async def test_a_shared_honorific_is_not_a_shared_token() -> None:
    # normalize_name strips a LEADING honorific, so "mr" is not a token of
    # either name and cannot be the thing they share.
    result = await resolve_mentions(
        [
            Mention("Mr. Smith", "Character", "b1", "they spoke together"),
            Mention("Mr. Jones", "Character", "b1", "they spoke together"),
        ],
        _identical(),
        _always,
    )
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_a_shared_function_word_is_not_a_shared_token() -> None:
    # An interior article or preposition survives normalization ("the Sign
    # of the Four" -> "sign of the four"). Sharing "of" or "the" is not
    # evidence that two names denote one thing.
    shared = "Both were named in the same breath."
    result = await resolve_mentions(
        [
            Mention("the Sign of the Four", "Concept", "b1", shared),
            Mention("the Hound of the Baskervilles", "Concept", "b1", shared),
        ],
        _identical(),
        _always,
    )
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_gate_survives_a_contentless_name_alongside_real_ones() -> None:
    # Phase 2B skips mentions whose normalized name is empty; the gate must
    # not crash on one, nor let it become a candidate for anything.
    result = await resolve_mentions(
        [
            Mention("Holmes", "Character", "b1", "he rose"),
            Mention("...", "Character", "b1", "he rose"),
            Mention("---", "Character", "b1", "he rose"),
            Mention("Sherlock Holmes", "Character", "b1", "he rose"),
        ],
        _identical(),
        _always,
    )
    assert len(result.entities) == 1
    assert result.entities[0].name == "Sherlock Holmes"


@pytest.mark.asyncio
async def test_single_character_tokens_do_not_fuse_strangers() -> None:
    shared = "I saw him take our prisoner away."
    result = await resolve_mentions(
        [
            Mention("I", "Character", "b1", shared),
            Mention("our prisoner", "Character", "b1", shared),
            Mention("J. Watson", "Character", "b1", shared),
            Mention("Watson", "Character", "b1", shared),
        ],
        _identical(),
        _always,
    )
    names = {e.name for e in result.entities}
    # "J. Watson" and "Watson" share "watson" and merge; the pronouns join
    # nothing.
    assert len(result.entities) == 3, names
    assert "J. Watson" in names


@pytest.mark.asyncio
async def test_gate_keeps_resolution_a_function_of_the_mention_set() -> None:
    import itertools

    shared = "Holmes and Drebber stood in the empty house."
    mentions = [
        Mention("Sherlock Holmes", "Character", "b1", shared),
        Mention("Holmes", "Character", "b1", shared),
        Mention("Drebber", "Character", "b1", shared),
        Mention("Toby", "Character", "b2", shared),
    ]
    baseline = None
    for order in itertools.permutations(mentions):
        result = await resolve_mentions(list(order), _identical(), _always)
        signature = (
            sorted((e.id, e.name, e.aliases, e.book_ids) for e in result.entities),
            sorted(result.mention_map.items()),
        )
        if baseline is None:
            baseline = signature
        assert signature == baseline
    assert baseline is not None
    assert len(baseline[0]) == 3


@pytest.mark.asyncio
async def test_a_token_free_nickname_no_longer_merges_in_pass_two() -> None:
    # The accepted cost of the gate, recorded deliberately: "Lizzy" shares
    # no token with "Elizabeth Bennet", so pass 2 will not join them and
    # they stay two nodes. Under-merging leaves two nodes a later pass can
    # join; over-merging destroyed both, plus every edge on them.
    result = await resolve_mentions(
        [
            Mention("Elizabeth Bennet", "Character", "b1", "she laughed"),
            Mention("Lizzy", "Character", "b1", "she laughed"),
        ],
        _identical(),
        _always,
    )
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_a_stray_possessive_s_is_not_a_shared_token() -> None:
    # normalize_name's possessive regex only knows the ASCII apostrophe, so
    # a curly one survives as a bare "s" token ("knight s place"). In the
    # live corpus that lone "s" merged a timber-yard with a street.
    shared = "They passed both on the way to the river."
    result = await resolve_mentions(
        [
            Mention("Knight’s Place", "Place", "b1", shared),
            Mention("Dawson’s wife", "Character", "b1", shared),
            Mention("Broderick and Nelson’s large timber-yard", "Place",
                    "b1", shared),
        ],
        _identical(),
        _always,
    )
    assert len(result.entities) == 3


@pytest.mark.asyncio
async def test_a_shared_initial_is_not_a_shared_token() -> None:
    shared = "Both signed the register that evening."
    result = await resolve_mentions(
        [
            Mention("J. Watson", "Character", "b1", shared),
            Mention("J. Smith", "Character", "b1", shared),
        ],
        _identical(),
        _always,
    )
    assert len(result.entities) == 2


# --- Pass-2 token-subset shortcut -------------------------------------------
#
# `Holmes` vs `Sherlock Holmes` is not a judgement call, it is a name-variant
# question with a deterministic answer. Same book + one name's tokens a subset
# of the other's => same entity, decided by string matching, with no embedding
# and no LLM. Every test below uses the all-identical embedder, so similarity
# is 1.0 for every pair and cannot be what decides; the spies assert the
# shortcut fired rather than the old similarity path.


def _spy_adjudicate(seen: list[tuple[str, str]]):
    async def adjudicate(canonical, mention):
        seen.append((canonical.name, mention.name))
        return False

    return adjudicate


@pytest.mark.asyncio
async def test_subset_name_merges_without_embedding_or_adjudication() -> None:
    calls: list[list[str]] = []
    seen: list[tuple[str, str]] = []
    result = await resolve_mentions(
        [
            Mention("Sherlock Holmes", "Character", "b1", "he rose from the chair"),
            Mention("Holmes", "Character", "b1", "the note lay on the table"),
        ],
        _identical(calls),
        _spy_adjudicate(seen),
    )
    assert len(result.entities) == 1
    assert set(result.entities[0].aliases) == {"holmes", "sherlock holmes"}
    assert result.entities[0].name == "Sherlock Holmes"
    assert calls == [], "a subset match must cost no embedding call"
    assert seen == [], "a subset match must not consult the 3B adjudicator"


@pytest.mark.asyncio
async def test_honorific_and_given_name_variant_merges_on_the_subset_rule() -> None:
    # "Dr. John Watson" -> "john watson"; "Watson" -> "watson". The live 3B
    # refuses this pair; the token subset answers it exactly.
    calls: list[list[str]] = []
    seen: list[tuple[str, str]] = []
    result = await resolve_mentions(
        [
            Mention("Dr. John Watson", "Character", "b1", "he wrote it down"),
            Mention("Watson", "Character", "b1", "a very different sentence"),
        ],
        _identical(calls),
        _spy_adjudicate(seen),
    )
    assert len(result.entities) == 1
    assert set(result.entities[0].aliases) == {"john watson", "watson"}
    assert calls == []
    assert seen == []


@pytest.mark.asyncio
async def test_a_shared_given_name_is_not_a_subset() -> None:
    # The residual bad merge Task 1f left behind. "john ferrier" and
    # "john watson" share a token but neither is a subset of the other, so
    # the shortcut must not fire: the pair still goes down the similarity
    # path and, with dissimilar contexts and a refusing adjudicator, stays
    # two people.
    calls: list[list[str]] = []
    seen: list[tuple[str, str]] = []
    # cosine 0.8: inside the 0.70-0.86 gray band, so pass 3 is consulted.
    vectors = {"John Ferrier": [1.0, 0.0], "John Watson": [0.8, 0.6]}

    async def embed(texts):
        calls.append(list(texts))
        return [vectors[t.split(" — ")[0]] for t in texts]

    result = await resolve_mentions(
        [
            Mention("John Ferrier", "Character", "b1", "he farmed the valley"),
            Mention("John Watson", "Character", "b1", "he wrote it down"),
        ],
        embed,
        _spy_adjudicate(seen),
    )
    assert len(result.entities) == 2
    assert calls, "a non-subset shared-token pair must still be embedded"
    assert seen, "a non-subset shared-token pair must still reach pass 3"


@pytest.mark.asyncio
async def test_a_dog_is_not_a_subset_of_a_detective() -> None:
    calls: list[list[str]] = []
    seen: list[tuple[str, str]] = []
    result = await resolve_mentions(
        [
            Mention("Holmes", "Character", "b1", "Toby led Holmes down the lane."),
            Mention("Toby", "Character", "b1", "Toby led Holmes down the lane."),
        ],
        _identical(calls),
        _spy_adjudicate(seen),
    )
    assert len(result.entities) == 2
    assert calls == []
    assert seen == []


@pytest.mark.asyncio
async def test_cross_book_subset_still_requires_adjudication() -> None:
    # The Phase 2B guarantee the shortcut deliberately does NOT touch:
    # "the Captain" in two unrelated novels must not merge silently, so a
    # cross-book merge is still never made without `adjudicate` saying True.
    seen: list[tuple[str, str]] = []
    refused = await resolve_mentions(
        [
            Mention("Sherlock Holmes", "Character", "b1", "he rose"),
            Mention("Holmes", "Character", "b2", "he frowned"),
        ],
        _identical(),
        _spy_adjudicate(seen),
    )
    assert seen, "a cross-book subset pair must still reach adjudication"
    assert len(refused.entities) == 2

    approved = await resolve_mentions(
        [
            Mention("Sherlock Holmes", "Character", "b1", "he rose"),
            Mention("Holmes", "Character", "b2", "he frowned"),
        ],
        _identical(),
        _always,
    )
    assert len(approved.entities) == 1
    assert set(approved.entities[0].book_ids) == {"b1", "b2"}


@pytest.mark.asyncio
async def test_subset_shortcut_is_gated_by_kind() -> None:
    # A subset of a candidate of a DIFFERENT kind is not a match.
    result = await resolve_mentions(
        [
            Mention("Baker Street", "Place", "b1", "they walked its length"),
            Mention("Street", "Character", "b1", "they walked its length"),
        ],
        _identical(),
        _always,
    )
    assert len(result.entities) == 2
    assert {e.kind for e in result.entities} == {"Place", "Character"}


@pytest.mark.asyncio
async def test_identical_token_sets_are_still_pass_one_not_the_shortcut() -> None:
    # Subset in BOTH directions is an exact normalized-name match, which
    # pass 1 already collapses. The shortcut must not become a second path
    # to the same merge: nothing is embedded and nothing is adjudicated.
    calls: list[list[str]] = []
    seen: list[tuple[str, str]] = []
    result = await resolve_mentions(
        [
            Mention("Mr. Sherlock Holmes", "Character", "b1", "he rose"),
            Mention("Sherlock Holmes", "Character", "b1", "he sat"),
        ],
        _identical(calls),
        _spy_adjudicate(seen),
    )
    assert len(result.entities) == 1
    assert result.entities[0].aliases == ("sherlock holmes",)
    assert result.entities[0].name == "Mr. Sherlock Holmes"
    assert calls == []
    assert seen == []


@pytest.mark.asyncio
async def test_subset_shortcut_survives_a_contentless_name() -> None:
    # Phase 2B skips an empty normalized name upstream; confirm the
    # shortcut cannot be reached by one and does not crash on one.
    result = await resolve_mentions(
        [
            Mention("...", "Character", "b1", "he rose"),
            Mention("Holmes", "Character", "b1", "he rose"),
            Mention("Sherlock Holmes", "Character", "b1", "he rose"),
            Mention("---", "Character", "b1", "he rose"),
        ],
        _identical(),
        _never,
    )
    assert len(result.entities) == 1
    assert result.entities[0].name == "Sherlock Holmes"


@pytest.mark.asyncio
async def test_subset_shortcut_keeps_resolution_a_function_of_the_set() -> None:
    import itertools

    mentions = [
        Mention("Sherlock Holmes", "Character", "b1", "he rose"),
        Mention("Holmes", "Character", "b1", "the note lay there"),
        Mention("Dr. John Watson", "Character", "b1", "he wrote it down"),
        Mention("Watson", "Character", "b1", "a different sentence"),
        Mention("Toby", "Character", "b1", "the dog trotted on"),
    ]
    baseline = None
    for order in itertools.permutations(mentions):
        result = await resolve_mentions(list(order), _identical(), _never)
        signature = (
            sorted((e.id, e.name, e.aliases, e.book_ids) for e in result.entities),
            sorted(result.mention_map.items()),
        )
        if baseline is None:
            baseline = signature
        assert signature == baseline
    assert baseline is not None
    assert len(baseline[0]) == 3
    assert {name for _, name, _, _ in baseline[0]} == {
        "Sherlock Holmes", "Dr. John Watson", "Toby",
    }


# --------------------------------------------------------------------------
# Task 1i, coordinator correction: a subset match may only merge when it is
# UNAMBIGUOUS. `normalize_name` strips honorifics, so `Major Sholto` becomes
# the bare `sholto` — a subset of both his sons, `thaddeus sholto` and
# `bartholomew sholto`. Merging on the first match found fuses the family.
#
# Every test below gives the BARE name a vector identical to one of the full
# names, so if it stays separate it is the ambiguity rule that kept it
# separate and not the similarity score. The two full names get orthogonal
# vectors, because they really are two people and a real embedder says so.


@pytest.mark.asyncio
@pytest.mark.parametrize("bare, twin, other", [
    # The Sholtos: a murdered father and his two sons, in one book.
    ("Major Sholto", "Bartholomew Sholto", "Thaddeus Sholto"),
    # The boatman's wife and the boatman.
    ("Mrs. Smith", "Mordecai Smith", "old Smith"),
    # John Ferrier and his adopted daughter.
    ("Ferrier", "John Ferrier", "Lucy Ferrier"),
])
async def test_a_surname_shared_by_two_characters_merges_with_neither(
    bare: str, twin: str, other: str,
) -> None:
    embed = _embedder({
        bare: [1.0, 0.0, 0.0],
        twin: [1.0, 0.0, 0.0],   # identical to the bare name's vector
        other: [0.0, 1.0, 0.0],
    })
    result = await resolve_mentions(
        [Mention(name, "Character", "b1", "ev") for name in (bare, twin, other)],
        embed,
        _always,  # even an approving adjudicator cannot fuse them
    )
    assert len(result.entities) == 3
    assert sorted(len(e.aliases) for e in result.entities) == [1, 1, 1]


@pytest.mark.asyncio
async def test_a_surname_shared_by_only_one_character_still_merges() -> None:
    """One candidate is not ambiguous: `watson` still joins `john watson`."""
    embed = _embedder({})
    result = await resolve_mentions(
        [
            Mention("Watson", "Character", "b1", "my companion"),
            Mention("Dr. John Watson", "Character", "b1", "my companion"),
        ],
        embed,
        _never,
    )
    assert len(result.entities) == 1
    assert result.entities[0].aliases == ("john watson", "watson")


@pytest.mark.asyncio
async def test_ambiguity_is_measured_over_the_whole_mention_set() -> None:
    """Not over "the entities built so far".

    A bare surname sorts BEFORE every full name containing it, so when the
    streaming loop reaches it none of its rivals exist yet and the first
    full name to arrive would win it. Reversing the input must not change
    the answer, ids included.
    """
    embed = _embedder({
        "Major Sholto": [1.0, 0.0, 0.0],
        "Bartholomew Sholto": [1.0, 0.0, 0.0],
        "Thaddeus Sholto": [0.0, 1.0, 0.0],
    })
    mentions = [
        Mention("Bartholomew Sholto", "Character", "b1", "ev"),
        Mention("Major Sholto", "Character", "b1", "ev"),
        Mention("Thaddeus Sholto", "Character", "b1", "ev"),
    ]
    forward = await resolve_mentions(list(mentions), embed, _always)
    backward = await resolve_mentions(list(reversed(mentions)), embed, _always)
    assert len(forward.entities) == len(backward.entities) == 3
    assert sorted(e.id for e in forward.entities) == sorted(
        e.id for e in backward.entities
    )


@pytest.mark.asyncio
async def test_ambiguity_is_per_book_and_per_kind() -> None:
    """Two books' Sholtos are not each other's ambiguity.

    `thaddeus sholto` in another book cannot make `sholto` ambiguous here:
    the same-book shortcut only ever looks at same-book candidates, so the
    count that gates it is scoped the same way.
    """
    embed = _embedder({})
    result = await resolve_mentions(
        [
            Mention("Major Sholto", "Character", "b1", "ev"),
            Mention("Bartholomew Sholto", "Character", "b1", "ev"),
            Mention("Thaddeus Sholto", "Character", "b2", "ev"),
        ],
        embed,
        _never,
    )
    b1 = [e for e in result.entities if e.book_ids == ("b1",)]
    assert len(b1) == 1
    assert b1[0].aliases == ("bartholomew sholto", "sholto")


@pytest.mark.asyncio
async def test_an_ambiguous_pair_does_not_block_an_unrelated_similarity_merge(
) -> None:
    """The refusal is about ONE pair of names, not about the mention.

    `bennet` is ambiguous between `elizabeth bennet` and `lizzy bennet`, so
    it merges with neither. That must not also stop `Lizzy Bennet` reaching
    `Elizabeth Bennet` through the ordinary similarity path, which never
    involved the bare surname at all.
    """
    embed = _embedder({
        "Elizabeth Bennet": [1.0, 0.0, 0.0],
        "Lizzy Bennet": [1.0, 0.0, 0.0],
        "Miss Bennet": [0.0, 1.0, 0.0],
    })
    result = await resolve_mentions(
        [
            Mention("Miss Bennet", "Character", "b1", "she laughed"),
            Mention("Elizabeth Bennet", "Character", "b1", "she laughed"),
            Mention("Lizzy Bennet", "Character", "b1", "she laughed"),
        ],
        embed,
        _always,
    )
    assert len(result.entities) == 2
    merged = max(result.entities, key=lambda e: len(e.aliases))
    assert merged.name == "Elizabeth Bennet"
    assert merged.aliases == ("elizabeth bennet", "lizzy bennet")


@pytest.mark.asyncio
async def test_a_possessive_description_no_longer_absorbs_the_person(
) -> None:
    """Task 1i pinned this as a known defect; Task 1k fixes it.

    `Lucy Ferrier’s father` normalizes to `lucy ferrier s father` — the
    curly-apostrophe possessive `normalize_name` does not strip — and
    `lucy ferrier` nests inside it with nothing else to be ambiguous with,
    so the daughter was absorbed into the description of her father. The
    ambiguity rule cannot see this: there is only ONE superset. The Task 1k
    guard reads the RAW surface form instead and declines the merge, so all
    three survive.

    The bare `Lucy` IS ambiguous between the two and stays separate, which
    is what Task 1i was responsible for and still holds.
    """
    embed = _embedder({
        "Lucy": [1.0, 0.0, 0.0],
        "Lucy Ferrier": [1.0, 0.0, 0.0],
        "Lucy Ferrier’s father": [0.0, 1.0, 0.0],
    })
    result = await resolve_mentions(
        [Mention(name, "Character", "b1", "ev") for name in
         ("Lucy", "Lucy Ferrier", "Lucy Ferrier’s father")],
        embed,
        _always,
    )
    lucy = [e for e in result.entities if e.aliases == ("lucy",)]
    assert len(lucy) == 1, "the ambiguous bare name stays its own entity"
    assert sorted(e.aliases for e in result.entities) == [
        ("lucy",), ("lucy ferrier",), ("lucy ferrier s father",)
    ], "the daughter is no longer buried inside a description of her father"


# --------------------------------------------------------------------------
# Task 1j: the ambiguous residue, asked ONE QUESTION PER BARE NAME.
#
# Fix round 1. The first cut asked pairwise, once per refusal, and that is
# not a smaller version of the grouped question — it is a different and
# wrong one. Measured against the live model: `Sholto` vs `Major Sholto`
# -> Yes AND `Sholto` vs `Thaddeus Sholto` -> Yes; `Ferrier` vs `John
# Ferrier` -> Yes AND vs `Lucy Ferrier` -> Yes. Each answer is
# individually reasonable — a bare surname really could be either man —
# and the aggregate fuses a murdered father with his son, which is exactly
# what the Task 1i refusal exists to prevent. Nothing in a pairwise
# question can see it is one of several, so the shape has to carry it.
#
# Both hooks are optional and default to None: with neither wired, every
# test above must still hold.

_FAMILY = [
    Mention("Major Sholto", "Character", "b1", "a: he had been dead for years"),
    Mention("Thaddeus Sholto", "Character", "b1", "b: the twin"),
    Mention("Bartholomew Sholto", "Character", "b1", "c: the brother"),
]
# Orthogonal, so nothing below can merge through the similarity path: any
# merge is the Task 1j hook's doing and nothing else's.
_FAMILY_VECTORS = {
    "Major Sholto": [1.0, 0.0, 0.0],
    "Thaddeus Sholto": [0.0, 1.0, 0.0],
    "Bartholomew Sholto": [0.0, 0.0, 1.0],
}


def _family_embed():
    return _embedder(_FAMILY_VECTORS)


def _picks(name: str):
    """A decider that answers one grouped question with one candidate."""
    def decide(question):
        chosen = [c for c in question.candidates if c.name == name]
        return chosen[0].id if chosen else None
    return decide


def _by_name(result):
    return {e.name: e for e in result.entities}


@pytest.mark.asyncio
async def test_three_refusals_for_one_bare_name_make_one_question() -> None:
    """Not three. `sholto` is refused against both sons AND both sons are
    refused against the bare node it minted — several pairwise events, one
    question, every rival offered."""
    recorded: list = []
    result = await resolve_mentions(
        list(_FAMILY), _family_embed(), _never, record_ambiguous=recorded.append
    )
    assert len(result.entities) == 3, "recording must not change the outcome"
    assert len(recorded) == 1
    question = recorded[0]
    # The display name carries the honorific back, which is the evidence
    # that separates the father from his sons.
    assert question.mention == "Major Sholto"
    assert question.kind == "Character"
    assert question.book_id == "b1"
    assert question.evidence == "a: he had been dead for years"
    assert sorted(c.name for c in question.candidates) == [
        "Bartholomew Sholto", "Thaddeus Sholto"
    ]


@pytest.mark.asyncio
async def test_the_chosen_candidate_merges_and_the_others_do_not() -> None:
    """The property the pairwise version broke.

    One answer, one merge. Every other rival of the same bare name must
    come out untouched — a father fused into two sons is the failure this
    whole task exists to avoid.
    """
    result = await resolve_mentions(
        list(_FAMILY), _family_embed(), _never,
        decide_ambiguous=_picks("Bartholomew Sholto"),
    )
    assert len(result.entities) == 2
    entities = _by_name(result)
    assert entities["Bartholomew Sholto"].aliases == (
        "bartholomew sholto", "sholto"
    )
    assert entities["Thaddeus Sholto"].aliases == ("thaddeus sholto",)


@pytest.mark.asyncio
async def test_the_other_candidate_is_equally_reachable() -> None:
    """Nothing about the first candidate is privileged."""
    result = await resolve_mentions(
        list(_FAMILY), _family_embed(), _never,
        decide_ambiguous=_picks("Thaddeus Sholto"),
    )
    entities = _by_name(result)
    assert entities["Thaddeus Sholto"].aliases == ("sholto", "thaddeus sholto")
    assert entities["Bartholomew Sholto"].aliases == ("bartholomew sholto",)


@pytest.mark.asyncio
async def test_none_for_a_group_leaves_every_candidate_separate() -> None:
    for answer in ("", None):
        result = await resolve_mentions(
            list(_FAMILY), _family_embed(), _never,
            decide_ambiguous=lambda question, a=answer: a,
        )
        assert len(result.entities) == 3
        assert sorted(len(e.aliases) for e in result.entities) == [1, 1, 1]


@pytest.mark.asyncio
async def test_two_bare_names_make_two_questions_that_do_not_cross() -> None:
    mentions = _FAMILY + [
        Mention("Ferrier", "Character", "b1", "f: he looked at the girl"),
        Mention("John Ferrier", "Character", "b1", "g: the father"),
        Mention("Lucy Ferrier", "Character", "b1", "h: the daughter"),
    ]
    vectors = dict(_FAMILY_VECTORS)
    vectors.update({"Ferrier": [0.0, 0.0, 0.0, 1.0],
                    "John Ferrier": [0.0, 0.0, 0.0, 0.0, 1.0],
                    "Lucy Ferrier": [0.0, 0.0, 0.0, 0.0, 0.0, 1.0]})
    recorded: list = []
    await resolve_mentions(list(mentions), _embedder(vectors), _never,
                           record_ambiguous=recorded.append)
    assert sorted(q.mention for q in recorded) == ["Ferrier", "Major Sholto"]
    assert len({q.key for q in recorded}) == 2

    # Answering the Ferriers must leave the Sholtos exactly as they were.
    result = await resolve_mentions(
        list(mentions), _embedder(vectors), _never,
        decide_ambiguous=_picks("Lucy Ferrier"),
    )
    entities = _by_name(result)
    assert entities["Lucy Ferrier"].aliases == ("ferrier", "lucy ferrier")
    assert entities["John Ferrier"].aliases == ("john ferrier",)
    assert entities["Major Sholto"].aliases == ("sholto",)
    assert entities["Thaddeus Sholto"].aliases == ("thaddeus sholto",)
    assert entities["Bartholomew Sholto"].aliases == ("bartholomew sholto",)


@pytest.mark.asyncio
async def test_grouping_is_stable_whatever_order_the_refusals_arrive_in() -> None:
    """This project has been bitten by order dependence repeatedly.

    A bare surname sorts BEFORE the full names containing it, so the
    refusals genuinely arrive in different directions depending on the
    input sequence. The question — and therefore the cache key — must not
    notice.
    """
    import itertools

    keys: set[tuple] = set()
    for permutation in itertools.permutations(_FAMILY):
        recorded: list = []
        result = await resolve_mentions(
            list(permutation), _family_embed(), _never,
            record_ambiguous=recorded.append,
        )
        keys.add((
            tuple(q.key for q in recorded),
            tuple(sorted(e.id for e in result.entities)),
        ))
        assert [q.mention for q in recorded] == ["Major Sholto"]
        assert [tuple(sorted(c.id for c in q.candidates)) for q in recorded] == [
            tuple(sorted(c.id for c in recorded[0].candidates))
        ]
    assert len(keys) == 1, "the question and the ids are a function of the SET"


@pytest.mark.asyncio
async def test_an_unambiguous_merge_records_nothing() -> None:
    recorded: list = []
    await resolve_mentions(
        [
            Mention("Watson", "Character", "b1", "my companion"),
            Mention("Dr. John Watson", "Character", "b1", "my companion"),
        ],
        _embedder({}),
        _never,
        record_ambiguous=recorded.append,
    )
    assert recorded == []


@pytest.mark.asyncio
async def test_an_answer_naming_an_unoffered_entity_never_merges() -> None:
    # A stale cache entry, or a mis-mapped batch that somehow got this far,
    # must not be allowed to fuse an arbitrary pair of entities.
    result = await resolve_mentions(
        list(_FAMILY), _family_embed(), _never,
        decide_ambiguous=lambda question: "not-a-candidate-id",
    )
    assert len(result.entities) == 3


@pytest.mark.asyncio
async def test_no_answer_refuses_exactly_as_before() -> None:
    with_hook = await resolve_mentions(
        list(_FAMILY), _family_embed(), _never,
        decide_ambiguous=lambda question: None,
    )
    without = await resolve_mentions(list(_FAMILY), _family_embed(), _never)
    assert sorted(e.id for e in with_hook.entities) == sorted(
        e.id for e in without.entities
    )
    assert [e.aliases for e in with_hook.entities] == [
        e.aliases for e in without.entities
    ]


@pytest.mark.asyncio
async def test_a_candidate_that_does_not_exist_yet_is_still_offered() -> None:
    """The reason grouping had to move off the pairwise path.

    `ferrier` sorts before both `john ferrier` and `lucy ferrier`, so when
    the bare name is processed NEITHER rival exists as an entity. A
    question built from "the candidates that happen to exist right now"
    could never offer them; one built from the mention set always can.
    """
    mentions = [
        Mention("Ferrier", "Character", "b1", "f: he looked at the girl"),
        Mention("John Ferrier", "Character", "b1", "g: the father"),
        Mention("Lucy Ferrier", "Character", "b1", "h: the daughter"),
    ]
    embed = _embedder({"Ferrier": [1.0, 0.0, 0.0],
                       "John Ferrier": [0.0, 1.0, 0.0],
                       "Lucy Ferrier": [0.0, 0.0, 1.0]})
    recorded: list = []
    await resolve_mentions(list(mentions), embed, _never,
                           record_ambiguous=recorded.append)
    assert len(recorded) == 1
    assert sorted(c.name for c in recorded[0].candidates) == [
        "John Ferrier", "Lucy Ferrier"
    ]


@pytest.mark.asyncio
async def test_superset_names_and_counts_agree() -> None:
    from lore.resolve.resolver import superset_counts, superset_names

    rows = [
        ("b1", "Character", "sholto"),
        ("b1", "Character", "thaddeus sholto"),
        ("b1", "Character", "bartholomew sholto"),
        ("b2", "Character", "sholto"),
    ]
    names = superset_names(rows)
    assert names[("b1", "Character", "sholto")] == (
        "bartholomew sholto", "thaddeus sholto"
    )
    assert names[("b2", "Character", "sholto")] == ()
    assert superset_counts(rows) == {k: len(v) for k, v in names.items()}


# --- Task 1k: a definite description must not swallow a name ---------------
#
# The token-subset rule above is what correctly unifies `Holmes` with
# `Sherlock Holmes`. Measured on the rebuilt graph it also did this:
#
#   'the base hospital at Peshawar'  <- absorbed 'peshawar'
#   'boarding-house at Camberwell'   <- absorbed 'camberwell'
#   "Lucy Ferrier's father"          <- absorbed 'lucy ferrier'
#
# A description's tokens are a superset of the name inside it, so the
# description wins the merge and the real place or person stops being a
# node at all. A description may exist as an entity; it must never be
# allowed to swallow one. Every test below uses the all-identical embedder,
# so similarity is 1.0 for every pair and only the rule can decide — which
# also means the guard has to drop the declined candidate from the
# SIMILARITY path too, not merely from the shortcut.


@pytest.mark.asyncio
async def test_a_description_does_not_swallow_the_place_it_names() -> None:
    calls: list[list[str]] = []
    seen: list[tuple[str, str]] = []
    result = await resolve_mentions(
        [
            Mention("boarding-house at Camberwell", "Place", "b1", "he lodged there"),
            Mention("Camberwell", "Place", "b1", "he lodged there"),
        ],
        _identical(calls),
        _spy_adjudicate(seen),
    )
    assert len(result.entities) == 2
    assert sorted(e.name for e in result.entities) == [
        "Camberwell", "boarding-house at Camberwell",
    ]


@pytest.mark.asyncio
async def test_a_possessive_description_does_not_swallow_the_character() -> None:
    # `Lucy Ferrier's father` NORMALIZES to `lucy ferrier father` — the
    # possessive is stripped, so the description is only visible on the RAW
    # surface form. A guard that looks at the normalized name sees an
    # ordinary three-token name here and fires on nothing.
    result = await resolve_mentions(
        [
            Mention("Lucy Ferrier's father", "Character", "b1", "he swore an oath"),
            Mention("Lucy Ferrier", "Character", "b1", "she rode beside him"),
        ],
        _identical(),
        _never,
    )
    assert len(result.entities) == 2
    assert sorted(e.name for e in result.entities) == [
        "Lucy Ferrier", "Lucy Ferrier's father",
    ]


@pytest.mark.asyncio
async def test_the_curly_apostrophe_possessive_is_detected_too() -> None:
    # The corpus text uses U+2019, not the ASCII apostrophe, and
    # `normalize_name`'s possessive regex only knows the ASCII one — so
    # `Ferrier’s farm` normalizes to `ferrier s farm` and the stray `s` is
    # dropped as a one-character token. Nothing but a raw-form test that
    # knows both apostrophes catches this.
    result = await resolve_mentions(
        [
            Mention("Ferrier’s farm", "Place", "b1", "the crops stood high"),
            Mention("Ferrier", "Place", "b1", "the crops stood high"),
        ],
        _identical(),
        _never,
    )
    assert len(result.entities) == 2
    assert sorted(e.name for e in result.entities) == [
        "Ferrier", "Ferrier’s farm",
    ]


@pytest.mark.asyncio
async def test_the_guard_leaves_the_working_name_variant_merges_alone() -> None:
    # The whole point of the subset rule. None of these is description
    # shaped, so all four still merge on string matching alone.
    for full, bare, kind in [
        ("Sherlock Holmes", "Holmes", "Character"),
        ("Dr. John Watson", "Watson", "Character"),
        ("Mr. Thaddeus Sholto", "Thaddeus", "Character"),
        ("Upper-Norwood", "Norwood", "Place"),
    ]:
        calls: list[list[str]] = []
        seen: list[tuple[str, str]] = []
        result = await resolve_mentions(
            [
                Mention(full, kind, "b1", "he rose from the chair"),
                Mention(bare, kind, "b1", "the note lay on the table"),
            ],
            _identical(calls),
            _spy_adjudicate(seen),
        )
        assert len(result.entities) == 1, f"{bare} should still merge into {full}"
        assert result.entities[0].name == full
        assert calls == [], "a subset match must still cost no embedding call"
        assert seen == []


@pytest.mark.asyncio
async def test_a_leading_article_is_not_a_description() -> None:
    # `The Daily News` and `the Marchioness de Brinvilliers` are real
    # entities in the corpus. Only a possessive or an INTERNAL function word
    # disqualifies a merge target; widening this into a leading-article test
    # would lose both.
    for full, bare, kind in [
        ("The Daily News", "News", "Concept"),
        ("the Marchioness de Brinvilliers", "Brinvilliers", "Character"),
    ]:
        result = await resolve_mentions(
            [
                Mention(full, kind, "b1", "it was printed that morning"),
                Mention(bare, kind, "b1", "a second sentence entirely"),
            ],
            _identical(),
            _never,
        )
        assert len(result.entities) == 1, f"{bare} should still merge into {full}"
        assert result.entities[0].name == full


@pytest.mark.asyncio
async def test_an_apostrophe_that_is_not_possessive_is_not_a_description() -> None:
    # `O'Brien` carries an apostrophe but no `'s`, so the possessive test
    # must not fire on it.
    result = await resolve_mentions(
        [
            Mention("Sergeant O'Brien", "Character", "b1", "he saluted"),
            Mention("O'Brien", "Character", "b1", "a different sentence"),
        ],
        _identical(),
        _never,
    )
    assert len(result.entities) == 1
    assert result.entities[0].name == "Sergeant O'Brien"


@pytest.mark.asyncio
async def test_the_declined_merge_does_not_come_back_through_similarity() -> None:
    # The ambiguity refusal above had to drop its candidate from the
    # similarity pass as well, because an entity and its own name share an
    # evidence sentence and the embedding measures co-occurrence. Same door,
    # same lock: an approving adjudicator must not be able to reopen it
    # either.
    result = await resolve_mentions(
        [
            Mention("the broad valley of Utah", "Place", "b1", "it lay before them"),
            Mention("Utah", "Place", "b1", "it lay before them"),
        ],
        _identical(),
        _always,
    )
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_the_guard_keeps_resolution_a_function_of_the_set() -> None:
    import itertools

    mentions = [
        Mention("boarding-house at Camberwell", "Place", "b1", "a: he lodged there"),
        Mention("Camberwell", "Place", "b1", "b: he lodged there"),
        Mention("Lucy Ferrier's father", "Character", "b1", "c: he swore"),
        Mention("Lucy Ferrier", "Character", "b1", "d: she rode"),
        Mention("Sherlock Holmes", "Character", "b1", "e: he rose"),
        Mention("Holmes", "Character", "b1", "f: the note lay there"),
    ]
    baseline = None
    for order in itertools.permutations(mentions):
        result = await resolve_mentions(list(order), _identical(), _never)
        signature = (
            sorted((e.id, e.name, e.aliases, e.book_ids) for e in result.entities),
            sorted(result.mention_map.items()),
        )
        if baseline is None:
            baseline = signature
        assert signature == baseline
    assert baseline is not None
    assert {name for _, name, _, _ in baseline[0]} == {
        "boarding-house at Camberwell", "Camberwell",
        "Lucy Ferrier's father", "Lucy Ferrier",
        "Sherlock Holmes",
    }


def test_description_shaped_names_against_the_measured_corpus_cases() -> None:
    from lore.resolve.resolver import is_description_shaped

    for name in [
        "Lucy Ferrier's father",
        "Lucy Ferrier’s father",
        "Ferrier’s farm",
        "foreigners in England",
        "the broad valley of Utah",
        "one of the main arteries of suburban London",
        "the base hospital at Peshawar",
        "boarding-house at Camberwell",
    ]:
        assert is_description_shaped(name), name
    for name in [
        "Mr. Thaddeus Sholto",
        "John Ferrier",
        "Sherlock Holmes",
        "Baker Street",
        "Scotland Yard",
        "The Daily News",
        "the Marchioness de Brinvilliers",
        "O'Brien",
        "Upper-Norwood",
        "Of Mice",          # a leading function word is not internal
        "Toby",
        "",
    ]:
        assert not is_description_shaped(name), name


# --- Task 11a: names with EQUAL token sets are one rival, not several ------
#
# Measured on A Study in Scarlet: bare `watson` nested inside `john watson`,
# `john watson m d` and `john h watson m d`. Initials and the "M.D." letters
# are single characters and carry no identity, so all three are the SAME
# token set {john, watson}, i.e. one person written three ways. Counting them
# as three rivals made `watson` "ambiguous", refused its merge, and split
# Watson into "Doctor Watson" (110 edges) and "John H. Watson, M.D." (56).


def test_superset_count_is_over_distinct_token_sets() -> None:
    from lore.resolve.resolver import superset_counts, superset_names

    rows = [
        ("b1", "Character", "watson"),
        ("b1", "Character", "john watson"),
        ("b1", "Character", "john watson m d"),
        ("b1", "Character", "john h watson m d"),
    ]
    assert superset_counts(rows)[("b1", "Character", "watson")] == 1
    assert len(superset_names(rows)[("b1", "Character", "watson")]) == 1


def test_superset_count_still_sees_two_different_people() -> None:
    from lore.resolve.resolver import superset_counts

    rows = [
        ("b1", "Character", "sholto"),
        ("b1", "Character", "thaddeus sholto"),
        ("b1", "Character", "thaddeus j sholto"),  # same man, an initial
        ("b1", "Character", "bartholomew sholto"),
    ]
    assert superset_counts(rows)[("b1", "Character", "sholto")] == 2


@pytest.mark.asyncio
async def test_a_bare_name_joins_one_person_written_several_ways() -> None:
    mentions = [
        Mention("Doctor Watson", "Character", "b1", "a: the doctor"),
        Mention("Dr. Watson", "Character", "b1", "b: apply Dr. Watson"),
        Mention("John Watson", "Character", "b1", "c: John Watson"),
        Mention("John Watson, M.D.", "Character", "b1", "d: reprinted"),
        Mention("John H. Watson, M.D.", "Character", "b1", "e: reminiscences"),
    ]
    recorded: list = []
    embed = _embedder({})
    result = await resolve_mentions(mentions, embed, _never,
                                    record_ambiguous=recorded.append)
    assert len(result.entities) == 1
    assert recorded == []
    targets = {result.mention_map[(m.name, m.kind, m.book_id)] for m in mentions}
    assert len(targets) == 1


# --- Task 11a: one name, two titles, two people ------------------------------
#
# `normalize_name` strips the title, so "Major Sholto", "Mr. Sholto" and
# "Sholto" are one name, `sholto`, and every decision about that name used to
# apply to all three. Measured on The Sign of the Four: the cached answer for
# the grouped `sholto` question (asked with "Major Sholto was strolling on the
# beach") merged it into Major John Sholto, and Small's "I never raised hand
# against Mr. Sholto. It was that little hell-hound Tonga who shot one of his
# cursed darts into him" (14:0, Bartholomew) became "Tonga KILLS Major John
# Sholto". Across both novels exactly three names carry two different
# titles, and each is two people: Major/Mr. Sholto, Captain/Miss Morstan,
# Mr./Mrs. Smith. Showing the model six passages per name instead of one
# sentence was tried live on gemini-3.5-flash-lite: told to pick an option only
# if every passage meant it, it still picked the Major; asked for one verdict
# per passage and merged only on unanimity, it refused the Major but also lost
# five correct merges and made two new wrong ones (London -> South London,
# Nevada -> Sierra Nevada). So the rule is deterministic: a surface form never
# joins an entity by NAME alone when its title conflicts with every title that
# entity is written with.


@pytest.mark.asyncio
async def test_a_conflicting_title_does_not_join_on_pass_one() -> None:
    result = await resolve_mentions([
        Mention("Mr. Smith", "Character", "b1", "a: the boatman"),
        Mention("Mrs. Smith", "Character", "b1", "b: his wife"),
    ], _embedder({}), _always)
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_equal_or_absent_titles_still_join() -> None:
    result = await resolve_mentions([
        Mention("Dr. Watson", "Character", "b1", "a"),
        Mention("Doctor Watson", "Character", "b1", "b"),
        Mention("Watson", "Character", "b1", "c"),
        Mention("Mr. Holmes", "Character", "b1", "d"),
        Mention("Holmes", "Character", "b1", "e"),
        Mention("Sherlock Holmes", "Character", "b1", "f"),
    ], _embedder({}), _never)
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_a_conflicting_title_does_not_join_through_a_name_variant() -> None:
    # "Miss Morstan" nests inside "Captain Arthur Morstan" unambiguously, and
    # the identical embedder would merge them on similarity too.
    same = [1.0, 0.0, 0.0]
    result = await resolve_mentions([
        Mention("Captain Arthur Morstan", "Character", "b1", "a: the father"),
        Mention("Miss Morstan", "Character", "b1", "a: the father"),
    ], _embedder({"Captain Arthur Morstan": same, "Miss Morstan": same}), _always)
    assert len(result.entities) == 2


@pytest.mark.asyncio
async def test_a_bare_name_answer_does_not_carry_a_conflicting_title_with_it() -> None:
    mentions = [
        Mention("Major John Sholto", "Character", "b1", "a: the major"),
        Mention("Major Sholto", "Character", "b1", "b: strolling on the beach"),
        Mention("Sholto", "Character", "b1", "c: I have killed him in my sleep"),
        Mention("Mr. Sholto", "Character", "b1", "d: never raised hand against"),
        Mention("Thaddeus Sholto", "Character", "b1", "e: the twin"),
        Mention("Bartholomew Sholto", "Character", "b1", "f: the other twin"),
    ]
    result = await resolve_mentions(
        mentions, _embedder({}), _never,
        decide_ambiguous=_picks("Major John Sholto"),
    )
    target = {m.name: result.mention_map[(m.name, m.kind, m.book_id)] for m in mentions}
    assert target["Major Sholto"] == target["Major John Sholto"]
    assert target["Sholto"] == target["Major John Sholto"]
    assert target["Mr. Sholto"] not in {
        target["Major John Sholto"], target["Thaddeus Sholto"],
        target["Bartholomew Sholto"],
    }


# --------------------------------------------------------------------------
# The Sign of the Four left `Small` (60 edges, the confession) and `Jonathan
# Small` as two nodes. Root cause, measured on the real extraction: a bare
# GIVEN name, `Jonathan`, nests in exactly one longer name and merges into
# `Jonathan Small` first (it sorts before it). The entity then carries the
# aliases {jonathan, jonathan small}, and the bare surname `small` — also
# nesting in exactly one longer name — had to nest with EVERY alias, which
# `jonathan` is not. The candidate was dropped silently: no merge, and no
# refusal either, so it was never queued for `lore disambiguate`.
#
# The vectors are orthogonal so a merge can only come from the name rule.


def _orthogonal(*names: str):
    return _embedder({
        name: [1.0 if i == j else 0.0 for j in range(len(names))]
        for i, name in enumerate(names)
    })


@pytest.mark.asyncio
async def test_a_bare_surname_joins_a_full_name_that_already_took_the_given_name(
) -> None:
    names = ("Jonathan", "Jonathan Small", "Small")
    result = await resolve_mentions(
        [Mention(name, "Character", "b1", "ev") for name in names],
        _orthogonal(*names),
        _never,
    )
    assert len(result.entities) == 1
    assert result.entities[0].name == "Jonathan Small"
    assert set(result.entities[0].aliases) == {
        "jonathan", "jonathan small", "small",
    }


@pytest.mark.xfail(strict=True, reason=(
    "Known residual, not present in either graphed book: when TWO bare names"
    " (`brown`, `john`) each made their own entity before `john brown` arrived,"
    " the full name joins only one of them and the other is stranded. Folding"
    " entities together mid-stream is a bigger change than this fix; the"
    " measured data has no instance of it."
))
@pytest.mark.asyncio
async def test_two_bare_names_that_both_precede_the_full_name_are_stranded() -> None:
    """Documents the residual: `Brown` sorts before `John`."""
    names = ("Brown", "John", "John Brown")
    result = await resolve_mentions(
        [Mention(name, "Character", "b1", "ev") for name in names],
        _orthogonal(*names),
        _never,
    )
    assert len(result.entities) == 1
    assert set(result.entities[0].aliases) == {"brown", "john", "john brown"}


@pytest.mark.asyncio
async def test_the_surname_is_still_refused_when_two_full_names_share_it() -> None:
    """The Sholto guard, through the bridge: `Thaddeus` merged with `Thaddeus
    Sholto` must not let `Sholto` (Major Sholto, the father) follow."""
    names = ("Thaddeus", "Thaddeus Sholto", "Bartholomew Sholto", "Major Sholto")
    recorded = []
    result = await resolve_mentions(
        [Mention(name, "Character", "b1", "ev") for name in names],
        _orthogonal(*names),
        _always,
        record_ambiguous=recorded.append,
    )
    assert len(result.entities) == 3
    thaddeus = next(e for e in result.entities if "thaddeus" in e.aliases)
    assert "sholto" not in thaddeus.aliases
    # ... and the refusal is now ASKED, not silently dropped.
    assert [q.mention for q in recorded] == ["Major Sholto"]


@pytest.mark.asyncio
async def test_the_surname_stays_out_when_the_given_name_is_ambiguous_too() -> None:
    """`John` nests in two longer names, so it is no proof that `John Small`
    and the bare `Small` are one man."""
    names = ("John", "John Small", "John Watson", "Small")
    result = await resolve_mentions(
        [Mention(name, "Character", "b1", "ev") for name in names],
        _orthogonal(*names),
        _never,
    )
    small = next(e for e in result.entities if "small" in e.aliases)
    assert "john small" in small.aliases
    john = next(e for e in result.entities if "john" in e.aliases)
    assert "john small" not in john.aliases
    assert len(result.entities) == 3


@pytest.mark.asyncio
async def test_a_description_superset_still_makes_a_bare_name_ambiguous() -> None:
    """Pinned, not changed: `Lucy Ferrier's father` is evidence that another
    Ferrier exists, so it counts as a rival (see the Lucy test above)."""
    names = ("Jonathan", "Jonathan Small", "Small", "Small’s father")
    result = await resolve_mentions(
        [Mention(name, "Character", "b1", "ev") for name in names],
        _orthogonal(*names),
        _never,
    )
    small = [e for e in result.entities if e.aliases == ("small",)]
    assert len(small) == 1


@pytest.mark.asyncio
async def test_an_ambiguous_surname_cannot_slip_into_a_bridged_entity() -> None:
    """The back door the silent skip left open.

    `Adam` joined `Adam Smith`, so that entity holds {adam, adam smith} and
    the bare `Smith` — which sorts AFTER both — used to be skipped by the
    every-alias rule without being refused. Skipped candidates stay in the
    similarity pass, and `Smith` shares an evidence sentence with Adam Smith
    (the embedding measures co-occurrence), so an approving embedder fused
    Adam Smith with a surname that is equally Will Smith's. Now it is a
    refusal, and an asked one.
    """
    names = ("Adam", "Adam Smith", "Will Smith", "Smith")
    shared = [1.0, 0.0, 0.0, 0.0]
    embed = _embedder({
        "Adam": shared,   # the entity's own vector is its FIRST name's
        "Adam Smith": shared,
        "Will Smith": [0.0, 0.0, 1.0, 0.0],
        "Smith": shared,
    })
    recorded = []
    result = await resolve_mentions(
        [Mention(name, "Character", "b1", "ev") for name in names],
        embed,
        _always,
        record_ambiguous=recorded.append,
    )
    adam = next(e for e in result.entities if "adam" in e.aliases)
    assert "smith" not in adam.aliases
    assert [(q.mention, sorted(c.name for c in q.candidates)) for q in recorded] == [
        ("Smith", ["Adam Smith", "Will Smith"]),
    ]


@pytest.mark.asyncio
async def test_a_bridged_question_is_about_the_name_asked_not_the_given_name() -> None:
    """`Charpentier` meeting {arthur, arthur charpentier} is a question about
    `Charpentier`, never about `Arthur` (which has one superset and is not
    ambiguous at all)."""
    names = ("Arthur", "Arthur Charpentier", "Madame Charpentier", "Charpentier")
    recorded = []
    await resolve_mentions(
        [Mention(name, "Character", "b1", "ev") for name in names],
        _orthogonal(*names),
        _never,
        record_ambiguous=recorded.append,
    )
    assert [q.mention for q in recorded] == ["Charpentier"]


@pytest.mark.asyncio
async def test_a_house_number_alias_is_not_a_bridge() -> None:
    """`No. 3` and `No. 7` are both the token set {no}, so an entity holding
    them is weak evidence of identity. The street `Pinchin Lane` must not be
    pulled into the house through them (measured on The Sign of the Four)."""
    names = ("No. 3", "No. 7", "No. 3, Pinchin Lane", "Pinchin Lane")
    result = await resolve_mentions(
        [Mention(name, "Place", "b1", "ev") for name in names],
        _orthogonal(*names),
        _never,
    )
    street = next(e for e in result.entities if "pinchin lane" in e.aliases)
    assert street.aliases == ("pinchin lane",)
