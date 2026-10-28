import sqlite3
from pathlib import Path

from lore.beats.store import Beat, beat_id, beats_for_entities, count, relink, save_beats
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import EMBED_DIM, LanceStore
from lore.db.sqlite import open_db


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = open_db(tmp_path / "lore.db")
    for b in ("bk1", "bk2"):
        conn.execute("INSERT INTO books (id, title, file_path) VALUES (?,?,?)",
                     (b, b, "x"))
    conn.commit()
    return conn


def _beat(chapter=1, names=("Mira",), quote="she walked to the gate alone",
          book="bk1", kind="action", chunk=None) -> Beat:
    return Beat(
        id=beat_id(book, chapter, names, quote), book_id=book,
        chapter_seq=chapter, chunk_id=chunk or f"{book}:{chapter}:0",
        entities=tuple(names), kind=kind, text="Mira goes to the gate.",
        quote=quote)


def _kuzu(tmp_path: Path) -> KuzuStore:
    k = KuzuStore(tmp_path / "kuzu")
    k.execute("CREATE (:Entity {id:'e1', name:'Mira Voss', kind:'Character',"
              " aliases:['Mira'], books:['bk1']})")
    k.execute("CREATE (:Entity {id:'e2', name:'Tomas', kind:'Character',"
              " aliases:[], books:['bk1']})")
    return k


def test_beat_id_is_stable_and_distinct():
    assert beat_id("b", 1, ("A",), "q") == beat_id("b", 1, ("A",), "q")
    assert beat_id("b", 1, ("A",), "q") != beat_id("b", 2, ("A",), "q")
    assert beat_id("b", 1, ("A",), "q") != beat_id("b", 1, ("A", "B"), "q")


def test_round_trip(tmp_path):
    conn = _conn(tmp_path)
    b = _beat(names=("Mira", "Tomas"))
    save_beats(conn, "bk1", 1, 0, [b])
    k = _kuzu(tmp_path)
    relink(conn, k)
    got = beats_for_entities(conn, ["e1"])
    assert len(got) == 1
    g = got[0]
    assert (g.id, g.book_id, g.chapter_seq, g.chunk_id, g.kind, g.quote) == (
        b.id, "bk1", 1, "bk1:1:0", "action", b.quote)
    assert g.entities == ("Mira", "Tomas")
    assert g.entity_ids == ("e1", "e2")
    # a beat involving e2 as second entity is found through e2 too, once
    assert len(beats_for_entities(conn, ["e1", "e2"])) == 1
    assert beats_for_entities(conn, []) == []
    assert beats_for_entities(conn, ["nope"]) == []
    assert count(conn, "bk1") == 1 and count(conn, "bk2") == 0
    k.close()


def test_beats_for_entities_in_chapter_order(tmp_path):
    conn = _conn(tmp_path)
    save_beats(conn, "bk1", 3, 0, [_beat(chapter=3, quote="third one here")])
    save_beats(conn, "bk1", 1, 0, [_beat(chapter=1, quote="first one here")])
    k = _kuzu(tmp_path)
    relink(conn, k)
    assert [b.chapter_seq for b in beats_for_entities(conn, ["e1"])] == [1, 3]
    k.close()


def test_resave_is_idempotent_and_replaces(tmp_path):
    conn = _conn(tmp_path)
    save_beats(conn, "bk1", 1, 0, [_beat(quote="old quote words")])
    save_beats(conn, "bk1", 1, 0, [_beat(quote="new quote words")])
    save_beats(conn, "bk1", 1, 0, [_beat(quote="new quote words")])
    assert count(conn, "bk1") == 1
    assert conn.execute("SELECT COUNT(*) FROM beat_entities").fetchone()[0] == 1
    # other parts and chapters are untouched
    save_beats(conn, "bk1", 1, 1, [_beat(quote="part two quote")])
    save_beats(conn, "bk1", 1, 0, [])
    assert count(conn, "bk1") == 1


def test_relink_after_merge(tmp_path):
    conn = _conn(tmp_path)
    save_beats(conn, "bk1", 1, 0, [_beat(names=("Mira",)),
                                   _beat(names=("Ghost",), quote="unknown one")])
    k = _kuzu(tmp_path)
    relink(conn, k)
    assert len(beats_for_entities(conn, ["e1"])) == 1
    # merge: e1 becomes an alias stub of canonical e9
    k.execute("CREATE (:Entity {id:'e9', name:'Mira Voss', kind:'Character',"
              " aliases:['Mira', 'Mira Voss'], books:['bk1','bk2']})")
    k.execute("MATCH (a:Entity {id:'e1'}), (c:Entity {id:'e9'}) "
              "CREATE (a)-[:ALIAS_OF]->(c)")
    relink(conn, k)
    assert beats_for_entities(conn, ["e1"]) == []
    assert len(beats_for_entities(conn, ["e9"])) == 1
    # a node deleted from the graph leaves the name unlinked, not dangling
    k.execute("MATCH (e:Entity {id:'e9'}) DETACH DELETE e")
    relink(conn, k)
    assert beats_for_entities(conn, ["e9"]) == []
    k.close()


def test_relink_case_insensitive_alias_and_stub_name(tmp_path):
    conn = _conn(tmp_path)
    save_beats(conn, "bk1", 1, 0, [_beat(names=("tomas",))])
    k = _kuzu(tmp_path)
    relink(conn, k)
    assert len(beats_for_entities(conn, ["e2"])) == 1
    k.close()


def test_lance_beats_upsert_and_search(tmp_path):
    store = LanceStore(tmp_path / "lancedb")
    assert "beats" in store.table_names()
    near = [1.0] + [0.0] * (EMBED_DIM - 1)
    far = [0.0] * (EMBED_DIM - 1) + [1.0]
    store.upsert_beats([{"id": "a", "book_id": "bk1", "vector": near},
                        {"id": "b", "book_id": "bk1", "vector": far}])
    store.upsert_beats([{"id": "a", "book_id": "bk1", "vector": near}])
    assert store.count("beats") == 2
    assert [h["id"] for h in store.search_beats(near, ["a", "b"], 1)] == ["a"]
    assert [h["id"] for h in store.search_beats(near, ["b"], 5)] == ["b"]
    assert store.search_beats(near, [], 5) == []
    store.delete_beats(["a"])
    assert store.count("beats") == 1
