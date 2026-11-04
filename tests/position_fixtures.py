"""A synthetic library for the reading position, on real SQLite, Kuzu and LanceDB.

World "Hollow Saga" holds Hollow Gate (series index 1), Hollow Keep (index 2)
and Wanderer Road (no series: its order against the others is unknown). World
"Other Realm" holds one book. Every chunk carries a marker `MARK-<book>-<chapter>`
so a test can see exactly which chapters reached a prompt, and the third chapter
of Hollow Gate holds SECRETFACT, the spoiler the position must hide at chapter 2.

Ren Ashby is first met as "Old Tam" (an alias stub of Ren Ashby, mentioned in
chapters 1 and 2 of Hollow Gate); the canonical name appears from chapter 3 on.
"""
from __future__ import annotations

from pathlib import Path

from lore.beats.store import Beat, save_beats
from lore.codex.store import CodexEntry, Statement, save_entry
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.db.sqlite import open_db
from lore.worlds import create_world
from tests.worlds_fixtures import unit

HOLLOW, OTHER = "Hollow Saga", "Other Realm"
# (book id, title, world, series, series_index, chapters)
BOOKS = [("hb1", "Hollow Gate", HOLLOW, HOLLOW, 1.0, 3),
         ("hb2", "Hollow Keep", HOLLOW, HOLLOW, 2.0, 3),
         ("hb3", "Wanderer Road", HOLLOW, None, None, 2),
         ("ob1", "Other Solo", OTHER, None, None, 1)]
TEXT = {
    ("hb1", 1): "Old Tam met Mira Cole at the gate of the hollow.",
    ("hb1", 2): "Old Tam walked the long road with Mira Cole.",
    ("hb1", 3): "SECRETFACT: Old Tam was truly Ren Ashby, and Ren Ashby betrayed Mira Cole to Dorn Vale.",
    ("hb2", 1): "Ren Ashby returned to the keep and Mira Cole trusted him.",
    ("hb2", 2): "Ren Ashby held the keep through the winter.",
    ("hb2", 3): "Ren Ashby left the keep at dawn.",
    ("hb3", 1): "Pip Marsh and Lan Dray crossed the wanderer road together.",
    ("hb3", 2): "Pip Marsh and Lan Dray camped by the wanderer road.",
    ("ob1", 1): "Zed Quill spoke with Yan Vale in the other realm.",
}
# (entity id, name, kind, books)
ENTITIES = [("h_ren", "Ren Ashby", "Character", ["hb1", "hb2"]),
            ("h_tam", "Old Tam", "Character", ["hb1"]),  # an alias stub of h_ren
            ("h_mira", "Mira Cole", "Character", ["hb1", "hb2"]),
            ("h_dorn", "Dorn Vale", "Character", ["hb1"]),
            ("w_pip", "Pip Marsh", "Character", ["hb3"]),
            ("w_lan", "Lan Dray", "Character", ["hb3"]),
            ("o_zed", "Zed Quill", "Character", ["ob1"]),
            ("o_yan", "Yan Vale", "Character", ["ob1"])]
# chunk id -> the entity nodes the chunk mentions
MENTIONS = {
    "hb1:1:0": ["h_tam", "h_mira"], "hb1:2:0": ["h_tam", "h_mira"],
    "hb1:3:0": ["h_tam", "h_ren", "h_mira", "h_dorn"],
    "hb2:1:0": ["h_ren", "h_mira"], "hb2:2:0": ["h_ren"], "hb2:3:0": ["h_ren"],
    "hb3:1:0": ["w_pip", "w_lan"], "hb3:2:0": ["w_pip", "w_lan"],
    "ob1:1:0": ["o_zed", "o_yan"]}
# (subject id, object id, book id, chapter, predicate)
EDGES = [("h_ren", "h_mira", "hb1", 1, "MET"), ("h_ren", "h_mira", "hb1", 3, "BETRAYED"),
         ("h_ren", "h_dorn", "hb1", 3, "SERVES"), ("h_mira", "h_ren", "hb2", 1, "TRUSTS"),
         ("w_pip", "w_lan", "hb3", 1, "MET"), ("w_pip", "w_lan", "hb3", 2, "TRAVELS_WITH"),
         ("o_zed", "o_yan", "ob1", 1, "MET")]
BEATS = [("hb1", 1, "EARLYBEAT"), ("hb1", 3, "LATEBEAT"), ("hb2", 1, "SEQUELBEAT")]
STATEMENTS = [("history", "EARLYSTATE", "hb1", 1), ("history", "LATESTATE", "hb1", 3),
              ("change", "SEQUELSTATE", "hb2", 1)]


def build_position_library(tmp_path: Path, *, one_world: bool = False) -> LoreConfig:
    """`one_world`: only Hollow Saga's books (the library is then unscoped)."""
    config = LoreConfig(data_dir=tmp_path / "data")
    conn = open_db(config.sqlite_path)
    books = [b for b in BOOKS if not (one_world and b[2] == OTHER)]
    ids = {HOLLOW: create_world(conn, HOLLOW)}
    if not one_world:
        ids[OTHER] = create_world(conn, OTHER)
    for book_id, title, world, series, index, _chapters in books:
        conn.execute("INSERT INTO books (id, title, file_path, series, series_index,"
                     " world_id) VALUES (?, ?, 'x.txt', ?, ?, ?)",
                     (book_id, title, series, index, ids[world]))
    have = {b[0] for b in books}
    lance = LanceStore(config.lancedb_dir)
    rows = []
    for n, ((book_id, chapter), text) in enumerate(TEXT.items()):
        if book_id not in have:
            continue
        chunk_id = f"{book_id}:{chapter}:0"
        body = (f"MARK-{book_id}-{chapter} " + text + " ") * 3
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, ?, ?, 0, 0, ?)", (chunk_id, book_id, chapter, body))
        rows.append({"vector": unit(0, 5 + n, 0.05), "chunk_id": chunk_id,
                     "book_id": book_id, "chapter_seq": chapter, "text": body,
                     "entity_ids": MENTIONS[chunk_id]})
    lance.add_chunks(rows)
    for book_id, chapter, text in BEATS:
        beat = Beat(id=f"beat-{book_id}-{chapter}", book_id=book_id, chapter_seq=chapter,
                    chunk_id=f"{book_id}:{chapter}:0", entities=("Ren Ashby",),
                    kind="action", text=text, quote="Ren Ashby")
        save_beats(conn, book_id, chapter, 0, [beat])
        conn.execute("UPDATE beat_entities SET entity_id = 'h_ren' WHERE beat_id = ?",
                     (beat.id,))
    save_entry(conn, CodexEntry(
        entity_id="h_ren", world_id=ids[HOLLOW], name="Ren Ashby", kind="Character",
        summary="WHOLESTORYSUMMARY",
        statements=tuple(Statement(aspect=a, text=t, quote="Ren Ashby",
                                   chunk_id=f"{b}:{c}:0") for a, t, b, c in STATEMENTS)))
    conn.commit()
    conn.close()

    graph = KuzuStore(config.kuzu_dir / "graph")
    for book_id, title, *_ in books:
        graph.execute("MERGE (b:Book {id: $id}) SET b.title = $t, b.author = ''",
                      {"id": book_id, "t": title})
    for chunk_id in MENTIONS:
        book_id, chapter, _seq = chunk_id.split(":")
        if book_id in have:
            graph.execute("CREATE (:Chunk {id: $id, book_id: $b, chapter_seq: $c})",
                          {"id": chunk_id, "b": book_id, "c": int(chapter)})
    for entity_id, name, kind, ebooks in ENTITIES:
        if ebooks[0] not in have:
            continue
        graph.execute("CREATE (:Entity {id: $id, name: $n, kind: $k, books: $b,"
                      " aliases: []})", {"id": entity_id, "n": name, "k": kind, "b": ebooks})
    graph.execute("MATCH (a:Entity {id: 'h_tam'}), (c:Entity {id: 'h_ren'})"
                  " CREATE (a)-[:ALIAS_OF]->(c)")
    for chunk_id, nodes in MENTIONS.items():
        if chunk_id.split(":")[0] not in have:
            continue
        for node in nodes:
            graph.execute("MATCH (c:Chunk {id: $c}), (e:Entity {id: $e})"
                          " CREATE (c)-[:MENTIONS]->(e)", {"c": chunk_id, "e": node})
    for subject, obj, book_id, chapter, predicate in EDGES:
        if book_id not in have:
            continue
        chunk_id = f"{book_id}:{chapter}:0"
        graph.execute(
            "MATCH (s:Entity {id: $s}), (o:Entity {id: $o}) CREATE (s)-[:REL {predicate: $p,"
            " predicate_raw: $p, evidence: $ev, confidence: 0.9, chunk_id: $c,"
            " book_id: $b, chapter_seq: $ch, run_id: 'r'}]->(o)",
            {"s": subject, "o": obj, "p": predicate, "c": chunk_id, "b": book_id,
             "ch": chapter, "ev": f"MARK-{book_id}-{chapter} {subject} {predicate} {obj}"})
    graph.close()
    return config
