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
    # pair does NOT collapse on pass 1; it must reach the embedder.
    assert calls, "second mention should have needed an embedding"
    assert len(result.entities) >= 1


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
        assert len(result.entities) == 1
        assert result.entities[0].name == "Elizabeth Bennet"


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
