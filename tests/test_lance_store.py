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
