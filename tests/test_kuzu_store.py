from pathlib import Path

from lore.db.kuzu_store import KuzuStore


def test_creates_schema(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kuzu")
    assert {"Book", "Chapter", "Chunk", "Entity", "HAS_CHAPTER", "HAS_CHUNK",
            "MENTIONS", "ALIAS_OF", "REL"} <= store.table_names()
    store.close()


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    KuzuStore(tmp_path / "kuzu").close()
    store = KuzuStore(tmp_path / "kuzu")  # must not fail on existing tables
    assert "Entity" in store.table_names()
    store.close()


def test_write_and_traverse(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kuzu")
    store.execute(
        "CREATE (:Entity {id: 'e1', name: 'Voldemort', kind: 'Character'})"
    )
    store.execute(
        "CREATE (:Entity {id: 'e2', name: 'The Dark Lord', kind: 'Character'})"
    )
    store.execute(
        "MATCH (a:Entity {id:'e2'}), (b:Entity {id:'e1'}) "
        "CREATE (a)-[:ALIAS_OF]->(b)"
    )
    result = store.execute(
        "MATCH (a:Entity)-[:ALIAS_OF]->(b:Entity) RETURN a.name, b.name"
    )
    rows = [tuple(r) for r in result]
    assert rows == [("The Dark Lord", "Voldemort")]
    store.close()


def test_rel_table_has_predicate_raw(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kuzu")
    store.execute(
        "CREATE (:Entity {id:'e1', name:'A', kind:'Character', aliases:['a']})"
    )
    store.execute(
        "CREATE (:Entity {id:'e2', name:'B', kind:'Character', aliases:['b']})"
    )
    store.execute(
        "MATCH (a:Entity {id:'e1'}), (b:Entity {id:'e2'}) "
        "CREATE (a)-[:REL {predicate:'OPPOSES', predicate_raw:'DISTRUSTS', "
        "evidence:'ev', confidence:0.9, chunk_id:'c1', book_id:'b1', "
        "chapter_seq:3, run_id:'r1'}]->(b)"
    )
    rows = [
        tuple(r)
        for r in store.execute(
            "MATCH ()-[r:REL]->() RETURN r.predicate, r.predicate_raw"
        )
    ]
    assert rows == [("OPPOSES", "DISTRUSTS")]
    store.close()
