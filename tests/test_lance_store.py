from pathlib import Path

from lore.db.lance_store import EMBED_DIM, LanceStore


def test_creates_tables(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    assert {"chunks", "entities"} <= store.table_names()


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    LanceStore(tmp_path / "lancedb")
    store = LanceStore(tmp_path / "lancedb")
    assert "chunks" in store.table_names()


def test_add_and_search_chunks(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    near = [1.0] + [0.0] * (EMBED_DIM - 1)
    far = [0.0] * (EMBED_DIM - 1) + [1.0]
    store.add_chunks([
        {"vector": near, "chunk_id": "b1:1:0", "book_id": "b1",
         "chapter_seq": 1, "text": "the fall of faction A", "entity_ids": ["e1"]},
        {"vector": far, "chunk_id": "b1:9:4", "book_id": "b1",
         "chapter_seq": 9, "text": "unrelated scene", "entity_ids": []},
    ])
    hits = store.search_chunks(near, limit=1)
    assert hits[0]["chunk_id"] == "b1:1:0"
    assert hits[0]["entity_ids"] == ["e1"]


def test_add_and_count_entities(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    store.add_entities([
        {"vector": [0.0] * EMBED_DIM, "entity_id": "e1", "name": "Darcy",
         "kind": "Character"},
    ])
    assert store.count("entities") == 1


def test_delete_chunks_for_book(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    store.add_chunks([
        {"vector": [0.0] * EMBED_DIM, "chunk_id": "b1:1:0", "book_id": "b1",
         "chapter_seq": 1, "text": "one", "entity_ids": []},
        {"vector": [0.0] * EMBED_DIM, "chunk_id": "b2:1:0", "book_id": "b2",
         "chapter_seq": 1, "text": "two", "entity_ids": []},
    ])
    store.delete_chunks_for_book("b1")
    assert store.count("chunks") == 1


def test_delete_entities(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    store.add_entities([
        {"vector": [0.0] * EMBED_DIM, "entity_id": "e1", "name": "A", "kind": "Character"},
        {"vector": [0.0] * EMBED_DIM, "entity_id": "e2", "name": "B", "kind": "Character"},
    ])
    store.delete_entities(["e1"])
    assert store.count("entities") == 1


# --- Task 9b: stored chunk vectors, for ranking graph evidence -------------


def _unit(i: int) -> list[float]:
    v = [0.0] * EMBED_DIM
    v[i] = 1.0
    return v


def test_chunk_vectors_returns_vectors_for_known_ids_and_omits_unknown(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    store.add_chunks([
        {"vector": _unit(0), "chunk_id": "b1:1:0", "book_id": "b1",
         "chapter_seq": 1, "text": "one", "entity_ids": []},
        {"vector": _unit(1), "chunk_id": "b2:9:3", "book_id": "b2",
         "chapter_seq": 9, "text": "two", "entity_ids": []},
        {"vector": _unit(2), "chunk_id": "b1:2:0", "book_id": "b1",
         "chapter_seq": 2, "text": "three", "entity_ids": []},
    ])
    vectors = store.chunk_vectors(["b1:1:0", "b2:9:3", "nowhere", "it's:quoted"])
    assert set(vectors) == {"b1:1:0", "b2:9:3"}
    assert vectors["b1:1:0"] == _unit(0)
    assert vectors["b2:9:3"] == _unit(1)


def test_chunk_vectors_of_no_ids_is_empty(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    store.add_chunks([
        {"vector": _unit(0), "chunk_id": "b1:1:0", "book_id": "b1",
         "chapter_seq": 1, "text": "one", "entity_ids": []},
    ])
    assert store.chunk_vectors([]) == {}


def test_chunk_vectors_is_not_truncated_to_a_default_page(tmp_path: Path) -> None:
    # LanceDB's query builder defaults to 10 rows; a ranker handed only the
    # first page would quietly rank every other candidate last.
    store = LanceStore(tmp_path / "lancedb")
    ids = [f"b1:{i}:0" for i in range(30)]
    store.add_chunks([
        {"vector": _unit(i), "chunk_id": c, "book_id": "b1", "chapter_seq": i,
         "text": "t", "entity_ids": []}
        for i, c in enumerate(ids)
    ])
    assert set(store.chunk_vectors(ids)) == set(ids)


def test_search_chunks_in_book_filters_inside_the_search(tmp_path: Path) -> None:
    """Nearest chunks are all book a; asking for book b's top 2 must still
    return 2 (a post-filter of the global top 2 would return none)."""
    store = LanceStore(tmp_path / "lance")

    def row(cid, book, x):
        vec = [0.0] * EMBED_DIM
        vec[0] = x
        return {"vector": vec, "chunk_id": cid, "book_id": book,
                "chapter_seq": 1, "text": cid, "entity_ids": []}

    store.add_chunks([row("a1", "a", 0.0), row("a2", "a", 0.1),
                      row("b1", "b", 5.0), row("b2", "b", 6.0)])
    query = [0.0] * EMBED_DIM
    hits = store.search_chunks_in_book(query, "b", limit=2)
    assert [h["chunk_id"] for h in hits] == ["b1", "b2"]
    assert store.search_chunks_in_book(query, "it's", limit=2) == []


def test_book_has_chunks(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lance")
    assert not store.book_has_chunks("b1")
    store.add_chunks([
        {"vector": [0.0] * EMBED_DIM, "chunk_id": "x:1:0", "book_id": "o'brien",
         "chapter_seq": 1, "text": "t", "entity_ids": []},
    ])
    assert store.book_has_chunks("o'brien")      # quoted, not broken by the '
    assert not store.book_has_chunks("b1")
    assert not store.book_has_chunks("x' OR '1'='1")  # no filter injection
