from pathlib import Path

import pytest

from lore.db.sqlite import open_db
from lore.extract.embed import QUERY_PREFIX
from lore.ingest.registry import chunks_by_id
from lore.query.seed import canonical_ids, live_entity_ids, seed_entities


class FakeLance:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.searched_with: list[float] | None = None

    def search_chunks(self, vector: list[float], limit: int = 5) -> list[dict]:
        self.searched_with = vector
        return self._rows[:limit]


class FakeKuzu:
    """Answers the three shapes seed.py issues: ALIAS_OF hop, REL degree,
    entity fetch.

    `degrees` defaults to "every known entity has an edge", which is what the
    tests that predate the degree filter assume. Real Kuzu omits a degree-0
    node from the aggregate result entirely rather than returning 0, so this
    fake omits it too -- a fake that returned a 0 row would let a filter that
    only checks membership pass while the real one let dead nodes through.
    """

    def __init__(
        self,
        aliases: dict[str, str],
        entities: dict[str, tuple],
        degrees: dict[str, int] | None = None,
    ) -> None:
        self._aliases = aliases
        self._entities = entities
        self._degrees = {i: 1 for i in entities} if degrees is None else degrees

    def execute(self, cypher: str, params: dict | None = None):
        params = params or {}
        if "ALIAS_OF" in cypher:
            return [[i, self._aliases[i]] for i in params["ids"] if i in self._aliases]
        if ":REL]" in cypher:
            return [
                [i, self._degrees[i]]
                for i in params["ids"]
                if self._degrees.get(i, 0) > 0
            ]
        return [list(self._entities[i]) for i in params["ids"] if i in self._entities]


@pytest.mark.asyncio
async def test_query_vector_uses_the_query_prefix(tmp_path: Path) -> None:
    # Index-time vectors carry DOCUMENT_PREFIX; query vectors must carry
    # QUERY_PREFIX or retrieval silently degrades instead of failing.
    seen: list[str] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        seen.extend(texts)
        return [[0.1] * 4 for _ in texts]

    lance = FakeLance([])
    await seed_entities("who is holmes?", lance, FakeKuzu({}, {}), embed)
    assert seen == [QUERY_PREFIX + "who is holmes?"]


@pytest.mark.asyncio
async def test_seeds_come_from_the_chunk_entity_bridge() -> None:
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    lance = FakeLance([
        {"chunk_id": "b:1:0", "entity_ids": ["e1", "e2"], "_distance": 0.10},
        {"chunk_id": "b:1:1", "entity_ids": ["e2"], "_distance": 0.20},
    ])
    kuzu = FakeKuzu({}, {
        "e1": ("e1", "Holmes", "Character", ["b1"]),
        "e2": ("e2", "Watson", "Character", ["b1", "b2"]),
    })
    seeds = await seed_entities("q", lance, kuzu, embed)
    assert {s.name for s in seeds} == {"Holmes", "Watson"}
    assert next(s for s in seeds if s.name == "Watson").books == ("b1", "b2")


@pytest.mark.asyncio
async def test_an_alias_stub_resolves_to_its_canonical() -> None:
    # Spec 2.1: REL edges hang only on canonical nodes. A seed that lands
    # on an alias stub must hop ALIAS_OF or every traversal returns
    # nothing -- the exact failure that made Phase 2B's demo empty.
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    lance = FakeLance([{"chunk_id": "b:1:0", "entity_ids": ["alias1"],
                        "_distance": 0.1}])
    kuzu = FakeKuzu({"alias1": "canon1"},
                    {"canon1": ("canon1", "Sherlock Holmes", "Character", ["b1"])})
    seeds = await seed_entities("q", lance, kuzu, embed)
    assert [s.id for s in seeds] == ["canon1"]
    assert seeds[0].name == "Sherlock Holmes"


@pytest.mark.asyncio
async def test_no_chunks_gives_no_seeds_and_does_not_raise() -> None:
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    assert await seed_entities("q", FakeLance([]), FakeKuzu({}, {}), embed) == []


@pytest.mark.asyncio
async def test_seeds_are_capped_and_ordered_by_best_score() -> None:
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    lance = FakeLance([
        {"chunk_id": "c0", "entity_ids": ["e3"], "_distance": 0.9},
        {"chunk_id": "c1", "entity_ids": ["e1"], "_distance": 0.1},
        {"chunk_id": "c2", "entity_ids": ["e2"], "_distance": 0.5},
    ])
    kuzu = FakeKuzu({}, {i: (i, i.upper(), "Character", ["b1"])
                         for i in ["e1", "e2", "e3"]})
    seeds = await seed_entities("q", lance, kuzu, embed, max_seeds=2)
    assert [s.id for s in seeds] == ["e1", "e2"]


def test_chunks_by_id_returns_text_for_known_ids_only(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    conn.execute(
        "INSERT INTO books (id, title, author, source, file_path, status) "
        "VALUES ('b1', 'T', NULL, 'upload', '/x', 'chunked')"
    )
    conn.execute(
        "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text) "
        "VALUES ('b1:1:0', 'b1', 1, 0, 0, 'the text')"
    )
    conn.commit()
    assert chunks_by_id(conn, ["b1:1:0", "missing"]) == {"b1:1:0": "the text"}
    assert chunks_by_id(conn, []) == {}
    conn.close()


@pytest.mark.asyncio
async def test_a_missing_distance_sorts_last_not_first() -> None:
    # LanceDB returns squared-L2 distance over unit vectors, so the range
    # is [0, 4] -- not a [0, 1] score. Lower is nearer, so a chunk with no
    # _distance must fall back to infinity; any finite fallback is a
    # distance a real chunk can exceed and would then be ranked ahead of.
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    lance = FakeLance([
        {"chunk_id": "c0", "entity_ids": ["e_missing"]},
        {"chunk_id": "c1", "entity_ids": ["e_real"], "_distance": 1.9},
    ])
    kuzu = FakeKuzu({}, {i: (i, i.upper(), "Character", ["b1"])
                         for i in ["e_missing", "e_real"]})
    seeds = await seed_entities("q", lance, kuzu, embed)
    assert [s.id for s in seeds] == ["e_real", "e_missing"]


@pytest.mark.asyncio
async def test_a_degree_zero_entity_is_not_a_seed_even_when_nearest() -> None:
    # No REL edge touches it, so every template returns zero rows for it by
    # construction: it is not a place a traversal can start.
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    lance = FakeLance([
        {"chunk_id": "c0", "entity_ids": ["dead"], "_distance": 0.10},
        {"chunk_id": "c1", "entity_ids": ["live"], "_distance": 0.90},
    ])
    kuzu = FakeKuzu(
        {},
        {
            "dead": ("dead", "Church", "Place", ["b1"]),
            "live": ("live", "Holmes", "Character", ["b1"]),
        },
        degrees={"live": 205},
    )

    seeds = await seed_entities("q", lance, kuzu, embed)

    assert [s.id for s in seeds] == ["live"]


@pytest.mark.asyncio
async def test_the_degree_filter_runs_before_the_cap_not_after() -> None:
    # Eight candidates, the two nearest dead. The cap must be spent on eight
    # live entities, not on six survivors of a cap applied to the dead ones.
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    ids = [f"e{n}" for n in range(10)]
    lance = FakeLance([
        {"chunk_id": f"c{n}", "entity_ids": [i], "_distance": 0.1 * n}
        for n, i in enumerate(ids)
    ])
    kuzu = FakeKuzu(
        {},
        {i: (i, i.upper(), "Character", ["b1"]) for i in ids},
        degrees={i: 3 for i in ids[2:]},
    )

    seeds = await seed_entities("q", lance, kuzu, embed, k=10)

    assert [s.id for s in seeds] == ids[2:10]


@pytest.mark.asyncio
async def test_max_seeds_defaults_to_eight_and_an_explicit_value_wins() -> None:
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    ids = [f"e{n}" for n in range(12)]
    rows = [
        {"chunk_id": f"c{n}", "entity_ids": [i], "_distance": 0.1 * n}
        for n, i in enumerate(ids)
    ]
    kuzu = FakeKuzu({}, {i: (i, i.upper(), "Character", ["b1"]) for i in ids})

    default = await seed_entities("q", FakeLance(rows), kuzu, embed, k=12)
    assert [s.id for s in default] == ids[:8]

    explicit = await seed_entities(
        "q", FakeLance(rows), kuzu, embed, k=12, max_seeds=3
    )
    assert [s.id for s in explicit] == ids[:3]


def test_the_degree_query_asks_for_undirected_rel_edges_on_the_given_ids() -> None:
    # Pins the shape the fake answers against the shape seed.py sends: one
    # batched aggregate over $ids, not one query per seed.
    seen: list[str] = []

    class Recorder(FakeKuzu):
        def execute(self, cypher: str, params: dict | None = None):
            seen.append(" ".join(cypher.split()))
            return super().execute(cypher, params)

    live_ids = live_entity_ids(
        Recorder({}, {"e1": ("e1", "E1", "Character", ["b1"])}), ["e1", "e2"]
    )

    assert live_ids == ["e1"]
    degree_query = next(q for q in seen if ":REL]" in q)
    assert "MATCH (e:Entity)-[r:REL]-()" in degree_query
    assert "WHERE e.id IN $ids" in degree_query
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_precomputed_question_vector_is_used_without_embedding_again() -> None:
    # Task 9b: the agent embeds the question once and reuses the vector for
    # seeding and for ranking evidence.
    async def embed(texts: list[str]) -> list[list[float]]:
        raise AssertionError("must not embed when a vector is supplied")

    lance = FakeLance([{"chunk_id": "b:1:0", "entity_ids": ["e1"], "_distance": 0.1}])
    kuzu = FakeKuzu({}, {"e1": ("e1", "Holmes", "Character", ["b1"])})
    seeds = await seed_entities("q", lance, kuzu, embed, vector=[0.3] * 4)
    assert [s.id for s in seeds] == ["e1"]
    assert lance.searched_with == [0.3] * 4


@pytest.mark.asyncio
async def test_embed_question_applies_the_query_prefix_once() -> None:
    from lore.query.seed import embed_question

    seen: list[str] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        seen.extend(texts)
        return [[0.2] * 4 for _ in texts]

    assert await embed_question("who?", embed) == [0.2] * 4
    assert seen == [QUERY_PREFIX + "who?"]


# --- Task 10d: entities the question names outright ---------------------------

from lore.query.seed import NameIndex, load_name_index, named_entity_ids  # noqa: E402

NAMES = NameIndex.from_rows([
    ("hope", "Mr. Jefferson Hope", ["jefferson hope", "hope"]),
    ("ferrier", "John Ferrier", ["ferrier"]),
    ("lucy", "Lucy Ferrier", []),
    ("jeff", "Jefferson", []),
    ("dog", "Toby", []),
    ("ann", "Ann", []),          # 3 letters: never matched alone
    ("mr", "Mr.", []),           # a title alone is not a name
    ("doc", "the Doctor", []),   # nor is a title behind an article
    ("police", "the police", []),
    ("small", "Small", []),
])


def test_a_whole_phrase_name_matches_case_insensitively_in_order() -> None:
    assert named_entity_ids("how is JOHN FERRIER connected to jefferson hope?",
                            NAMES) == ["ferrier", "hope"]


def test_a_name_inside_a_longer_matched_name_is_not_a_second_match() -> None:
    # "Jefferson" (its own node) and "Ferrier" sit inside longer matches.
    assert named_entity_ids("Why did Jefferson Hope visit Lucy Ferrier?", NAMES) == [
        "hope", "lucy"]


def test_word_boundaries_hold() -> None:
    assert named_entity_ids("Hopeful Tobyish people", NAMES) == []


def test_a_three_letter_name_never_matches_but_a_multi_word_one_may() -> None:
    assert named_entity_ids("Did Ann see him?", NAMES) == []
    short = NameIndex.from_rows([("ab", "Al Bo", [])])
    assert named_entity_ids("where did al bo go", short) == ["ab"]


def test_titles_and_articles_alone_are_not_names() -> None:
    assert named_entity_ids("Did Mr. Smith call the Doctor?", NAMES) == []


def test_a_generic_phrase_hits_only_an_entity_literally_named_that() -> None:
    assert named_entity_ids("when did the police arrive?", NAMES) == ["police"]
    assert named_entity_ids("when did the police arrive?",
                            NameIndex.from_rows([("x", "Scotland Yard", [])])) == []


def test_a_lowercase_word_in_a_capitalised_question_is_not_a_one_word_name() -> None:
    # "small" in ordinary prose is an adjective; in an all-lowercase question
    # casing says nothing, so it may still be the name.
    assert named_entity_ids("Who rowed a small boat?", NAMES) == []
    assert named_entity_ids("Where was Small caught?", NAMES) == ["small"]
    assert named_entity_ids("where was small caught?", NAMES) == ["small"]


@pytest.mark.asyncio
async def test_named_seeds_come_first_and_do_not_use_the_vector_cap() -> None:
    from lore.query.seed import named_seeds

    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    entities = {i: (i, i.upper(), "Character", ["b1"]) for i in ["e1", "e2", "e3", "n1"]}
    kuzu = FakeKuzu({"alias_n": "n1"}, entities, degrees={"e1": 1, "e2": 1, "e3": 1, "n1": 2})
    named = await named_seeds(kuzu, ["alias_n", "dead"])
    assert [s.id for s in named] == ["n1"]  # resolved through ALIAS_OF; dead dropped
    lance = FakeLance([
        {"chunk_id": "c1", "entity_ids": ["e1", "n1"], "_distance": 0.1},
        {"chunk_id": "c2", "entity_ids": ["e2"], "_distance": 0.2},
        {"chunk_id": "c3", "entity_ids": ["e3"], "_distance": 0.3},
    ])
    seeds = await seed_entities("q", lance, kuzu, embed, max_seeds=2, named=named)
    assert [s.id for s in seeds] == ["n1", "e1", "e2"]


def test_load_name_index_reads_names_and_aliases_from_a_real_graph(tmp_path: Path) -> None:
    from lore.db.kuzu_store import KuzuStore

    store = KuzuStore(tmp_path / "graph")
    store.execute("CREATE (:Entity {id: 'h', name: 'Mr. Jefferson Hope', kind: 'Character',"
                  " aliases: ['jefferson hope'], books: ['b1']})")
    store.execute("CREATE (:Entity {id: 'f', name: 'John Ferrier', kind: 'Character',"
                  " aliases: [], books: ['b1']})")
    queries: list[str] = []

    class Counting:
        def execute(self, cypher, params=None):
            queries.append(cypher)
            return store.execute(cypher, params)

    index = load_name_index(Counting())
    assert len(queries) == 1, "one batched read per ask"
    assert named_entity_ids("Did jefferson hope know John Ferrier?", index) == ["h", "f"]
    store.close()
