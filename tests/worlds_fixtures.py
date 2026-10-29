"""A synthetic library of two worlds on real SQLite, Kuzu and LanceDB.

World "Alpha Saga" holds two books of a series (Alpha One, Alpha Two) and
world "Beta Realm" one standalone book (Beta Solo). Each world has its OWN
"Zed Quill" and "Yan Vale", the same names with different ids, as the loader
keeps them once resolution is scoped to a world. Every passage carries a
marker word (ALPHAMARK, BETAMARK) so a test can see which world's text
reached a prompt.

Chunk vectors are unit vectors near axis 0, so a question embedded as axis 0
is near every chunk; `beta_axis` moves Beta's chunks away from it.
"""
from __future__ import annotations

import math
from pathlib import Path

from lore.beats.store import Beat, save_beats
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import EMBED_DIM, LanceStore
from lore.db.sqlite import open_db
from lore.query.agent import QueryModels
from lore.query.decompose import SubQuestions
from lore.query.synthesize import Synthesis
from lore.worlds import create_world

ALPHA, BETA = "Alpha Saga", "Beta Realm"
# (book id, title, world name or None, series, series_index)
BOOKS = [("ba1", "Alpha One", ALPHA, ALPHA, 1.0),
         ("ba2", "Alpha Two", ALPHA, ALPHA, 2.0),
         ("bb1", "Beta Solo", BETA, None, None)]
# (entity id, name, kind, books)
ENTITIES = [("a_zed", "Zed Quill", "Character", ["ba1", "ba2"]),
            ("a_yan", "Yan Vale", "Character", ["ba1"]),
            ("b_zed", "Zed Quill", "Character", ["bb1"]),
            ("b_yan", "Yan Vale", "Character", ["bb1"])]
# (subject id, object id, book id, chunk id, chapter)
EDGES = [("a_zed", "a_yan", "ba1", "ba1:1:0", 1),
         ("a_zed", "a_yan", "ba2", "ba2:1:0", 1),
         ("b_zed", "b_yan", "bb1", "bb1:1:0", 1)]
MARK = {"ba1": "ALPHAMARK", "ba2": "ALPHAMARK", "bb1": "BETAMARK"}
NO_WORLD = None


def unit(axis: int, mix_axis: int | None = None, mix: float = 0.0) -> list[float]:
    v = [0.0] * EMBED_DIM
    v[axis] = 1.0
    if mix_axis is not None:
        v[mix_axis] = mix
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


QUESTION_VECTOR = unit(0)


def build_library(tmp_path: Path, *, worlds: bool = True, beta_axis: int = 0,
                  one_world: bool = False, cross_edge: bool = False) -> LoreConfig:
    """`worlds=False`: the library as it was before worlds (no world rows).
    `one_world`: every book in "Alpha Saga". `cross_edge`: also one REL edge
    joining a_zed to b_yan, which no loader should ever write."""
    config = LoreConfig(data_dir=tmp_path / "data")
    conn = open_db(config.sqlite_path)
    ids = {}
    if worlds:
        ids[ALPHA] = create_world(conn, ALPHA)
        if not one_world:
            ids[BETA] = create_world(conn, BETA)
    for book_id, title, world, series, index in BOOKS:
        world_id = ids.get(world if not one_world else ALPHA) if worlds else None
        conn.execute(
            "INSERT INTO books (id, title, file_path, series, series_index, world_id)"
            " VALUES (?, ?, 'x.txt', ?, ?, ?)", (book_id, title, series, index, world_id))
    lance = LanceStore(config.lancedb_dir)
    rows = []
    for n, (book_id, _t, _w, _s, _i) in enumerate(BOOKS):
        for chapter in (1, 2):
            chunk_id = f"{book_id}:{chapter}:0"
            text = (f"{MARK[book_id]} {book_id} chapter {chapter}: the traveller Zed Quill"
                    f" walked on with Yan Vale through the {book_id} hills. " * 3)
            conn.execute(
                "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                " VALUES (?, ?, ?, 0, 0, ?)", (chunk_id, book_id, chapter, text))
            who = ("a_" if book_id.startswith("ba") else "b_")
            rows.append({
                "vector": (unit(beta_axis, 5 + n, 0.05 * chapter) if book_id == "bb1"
                           else unit(0, 5 + n, 0.05 * chapter)),
                "chunk_id": chunk_id, "book_id": book_id, "chapter_seq": chapter,
                "text": text, "entity_ids": [who + "zed", who + "yan"]})
    lance.add_chunks(rows)
    beats = {"ba1": ("a_zed", "ALPHABEAT"), "bb1": ("b_zed", "BETABEAT")}
    for book_id, (entity, text) in beats.items():
        beat = Beat(id=f"beat-{book_id}", book_id=book_id, chapter_seq=1,
                    chunk_id=f"{book_id}:1:0", entities=("Zed Quill",), kind="action",
                    text=text, quote="walked on")
        save_beats(conn, book_id, 1, 0, [beat])
        conn.execute("UPDATE beat_entities SET entity_id = ? WHERE beat_id = ?",
                     (entity, beat.id))
    conn.commit()
    conn.close()

    graph = KuzuStore(config.kuzu_dir / "graph")
    for book_id, title, *_ in BOOKS:
        graph.execute("MERGE (b:Book {id: $id}) SET b.title = $t, b.author = ''",
                      {"id": book_id, "t": title})
    for entity_id, name, kind, books in ENTITIES:
        graph.execute("CREATE (:Entity {id: $id, name: $n, kind: $k, books: $b,"
                      " aliases: []})", {"id": entity_id, "n": name, "k": kind, "b": books})
    edges = list(EDGES)
    if cross_edge:
        edges.append(("a_zed", "b_yan", "ba1", "ba1:2:0", 2))
    for subject, obj, book_id, chunk_id, chapter in edges:
        graph.execute(
            "MATCH (s:Entity {id: $s}), (o:Entity {id: $o}) CREATE (s)-[:REL {predicate:"
            " 'INTERACTS_WITH', predicate_raw: 'met', evidence: $ev, confidence: 0.9,"
            " chunk_id: $c, book_id: $b, chapter_seq: $ch, run_id: 'r'}]->(o)",
            {"s": subject, "o": obj, "ev": f"{MARK[book_id]} {subject} met {obj}",
             "c": chunk_id, "b": book_id, "ch": chapter})
    graph.close()
    return config


def scripted_models(sub_questions=None, synthesis=None, select_entities=(1,)):
    """QueryModels that embed every text as axis 0 and record every call."""
    from lore.query.select import TemplateChoice
    calls = {"embed": [], "decompose": [], "select": [], "synthesize": []}

    async def embed(texts):
        calls["embed"].append(list(texts))
        return [QUESTION_VECTOR for _ in texts]

    async def decompose(prompt, schema):
        calls["decompose"].append(prompt)
        return SubQuestions(sub_questions=list(sub_questions))

    async def select(prompt, schema):
        calls["select"].append(prompt)
        return TemplateChoice(template="neighbours", entities=list(select_entities))

    async def synthesize(prompt, schema):
        calls["synthesize"].append(prompt)
        return synthesis or Synthesis(claims=[], can_answer=False)

    async def aclose():
        return None

    models = QueryModels(embed, select, synthesize, lambda: "writer", aclose,
                         lambda: "chooser",
                         decompose=decompose if sub_questions is not None else None)
    return models, calls
