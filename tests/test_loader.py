import hashlib
import itertools
import re
from pathlib import Path

import lancedb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import EMBED_DIM, LanceStore
from lore.db.sqlite import open_db
from lore.graph.artifacts import MissingArtifactError
from lore.graph.loader import PartialLoadError, load_book
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book
from lore.orchestrator.run import set_unit_state
from lore.resolve.normalize import entity_id

BOOK = "abcdef0123456789"
UNIT = f"{BOOK}-u0000-aaaa"


async def _embed(texts):
    return [[0.0] * EMBED_DIM for _ in texts]


async def _never(canonical, mention) -> bool:
    return False


def _setup(tmp_path: Path):
    config = LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1, scene_seq=0,
              seq=i, text=f"passage {i}")
        for i in range(2)
    ]
    ingest_book(conn, book_id=BOOK, title="T", author="A", source="upload",
                file_path="/x/b.epub", chunks=chunks)
    d = config.extractions_dir / BOOK / UNIT
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({
            "chunk_id": [f"{BOOK}:1:0", f"{BOOK}:1:1"],
            "book_id": [BOOK, BOOK],
            "subject": ["Mr. Darcy", "Darcy"],
            "predicate": ["OPPOSES", "LOVES"],
            "object": ["Wickham", "Elizabeth"],
            "evidence": ["he despised him", "he admired her"],
            "confidence": [0.9, 0.8],
            "entity_names": [["Mr. Darcy", "Wickham"], ["Darcy", "Elizabeth"]],
            "entity_kinds": [["Character", "Character"], ["Character", "Character"]],
            "extractor_model": ["m", "m"],
            "run_id": [UNIT, UNIT],
        }),
        d / "triplets.parquet",
    )
    pq.write_table(
        pa.table({
            "chunk_id": [f"{BOOK}:1:0", f"{BOOK}:1:1"],
            "vector": [[0.0] * EMBED_DIM, [0.0] * EMBED_DIM],
            "embed_model": ["e", "e"],
        }),
        d / "embeddings.parquet",
    )
    set_unit_state(conn, BOOK, UNIT, "completed")
    kuzu = KuzuStore(config.kuzu_dir / "graph")
    lance = LanceStore(config.lancedb_dir)
    return config, conn, kuzu, lance


@pytest.mark.asyncio
async def test_load_writes_nodes_and_edges(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert report.rel_edges == 2
    assert report.chunks == 2
    books = [tuple(r) for r in kuzu.execute("MATCH (b:Book) RETURN b.id, b.title")]
    assert books == [(BOOK, "T")]
    edges = [
        tuple(r)
        for r in kuzu.execute(
            "MATCH (a:Entity)-[r:REL]->(b:Entity) "
            "RETURN a.name, r.predicate, r.predicate_raw, b.name, r.chapter_seq"
        )
    ]
    assert len(edges) == 2
    assert all(e[4] == 1 for e in edges)  # chapter_seq carried through
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_aliases_collapse_to_one_entity(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    # "Mr. Darcy" and "Darcy" normalize identically — one canonical node.
    names = sorted(
        r[0] for r in kuzu.execute(
            "MATCH (e:Entity) WHERE e.kind = 'Character' RETURN e.name"
        )
    )
    assert names.count("Mr. Darcy") + names.count("Darcy") == 1
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_load_is_idempotent(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    first = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    second = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert (second.entities, second.rel_edges, second.chunks) == (
        first.entities, first.rel_edges, first.chunks
    )
    total = len([r for r in kuzu.execute("MATCH ()-[r:REL]->() RETURN r.chunk_id")])
    assert total == first.rel_edges
    assert lance.count("chunks") == first.chunks
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_chunks_carry_the_entity_bridge(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    rows = lance.search_chunks([0.0] * EMBED_DIM, limit=5)
    assert rows and all(isinstance(r["entity_ids"], list) for r in rows)
    assert any(r["entity_ids"] for r in rows)
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_book_with_no_completed_units_raises(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    conn.execute("UPDATE jobs SET state = 'failed'")
    conn.commit()
    with pytest.raises(ValueError, match="no completed"):
        await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    conn.close(); kuzu.close()


# --------------------------------------------------------------------------
# Self-review probes: shapes the brief never lists.
# --------------------------------------------------------------------------

_TRIPLET_SCHEMA = pa.schema([
    pa.field("chunk_id", pa.string()),
    pa.field("book_id", pa.string()),
    pa.field("subject", pa.string()),
    pa.field("predicate", pa.string()),
    pa.field("object", pa.string()),
    pa.field("evidence", pa.string()),
    pa.field("confidence", pa.float64()),
    pa.field("entity_names", pa.list_(pa.string())),
    pa.field("entity_kinds", pa.list_(pa.string())),
    pa.field("extractor_model", pa.string()),
    pa.field("run_id", pa.string()),
])

_EMBED_SCHEMA = pa.schema([
    pa.field("chunk_id", pa.string()),
    pa.field("vector", pa.list_(pa.float64())),
    pa.field("embed_model", pa.string()),
])

BOOK_B = "0123456789abcdef"


def _env(tmp_path: Path):
    config = LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    kuzu = KuzuStore(config.kuzu_dir / "graph")
    lance = LanceStore(config.lancedb_dir)
    return config, conn, kuzu, lance


def _row(chunk_id, book_id, subject, predicate, obj, names, kinds, evidence="ev"):
    return {
        "chunk_id": chunk_id, "book_id": book_id, "subject": subject,
        "predicate": predicate, "object": obj, "evidence": evidence,
        "confidence": 0.5, "entity_names": names, "entity_kinds": kinds,
        "extractor_model": "m", "run_id": f"{book_id}-u0000-aaaa",
    }


def _write_unit(config, book_id, triplet_rows, chunk_ids, firsts=None):
    """(Re)write a unit's artifacts — used to simulate a re-extraction."""
    unit = f"{book_id}-u0000-aaaa"
    d = config.extractions_dir / book_id / unit
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(triplet_rows, schema=_TRIPLET_SCHEMA),
        d / "triplets.parquet",
    )
    if firsts is None:
        firsts = [float(i + 1) for i in range(len(chunk_ids))]
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"chunk_id": cid,
                 "vector": [first] + [0.0] * (EMBED_DIM - 1),
                 "embed_model": "e"}
                for cid, first in zip(chunk_ids, firsts)
            ],
            schema=_EMBED_SCHEMA,
        ),
        d / "embeddings.parquet",
    )
    return unit


def _add_book(config, conn, book_id, chunks, triplet_rows, *, embedded=None,
              title=None, firsts=None):
    ingest_book(conn, book_id=book_id, title=title or f"title-{book_id}",
                author="A", source="upload", file_path=f"/x/{book_id}.epub",
                chunks=chunks)
    ids = [c.id for c in chunks] if embedded is None else embedded
    unit = _write_unit(config, book_id, triplet_rows, ids, firsts)
    set_unit_state(conn, book_id, unit, "completed")
    return unit


def _entity_vectors(config) -> dict[str, list[float]]:
    table = lancedb.connect(str(config.lancedb_dir)).open_table("entities")
    return {r["entity_id"]: list(r["vector"]) for r in table.to_arrow().to_pylist()}


def _chunk(book_id, chapter, seq, text="passage"):
    return Chunk(id=f"{book_id}:{chapter}:{seq}", book_id=book_id,
                 chapter_seq=chapter, scene_seq=0, seq=seq, text=text)


def _rows(kuzu, cypher):
    return [tuple(r) for r in kuzu.execute(cypher)]


def _snapshot(kuzu, lance) -> dict:
    return {
        "entities": sorted(
            (r[0], r[1], r[2], tuple(r[3] or ()))
            for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.id, e.name, e.kind, e.books")
        ),
        "rel": sorted(_rows(
            kuzu,
            "MATCH (a:Entity)-[r:REL]->(b:Entity) RETURN a.id, r.predicate,"
            " r.predicate_raw, b.id, r.chunk_id, r.chapter_seq, r.evidence",
        )),
        "mentions": sorted(_rows(
            kuzu, "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) RETURN c.id, e.id")),
        "alias_of": sorted(_rows(
            kuzu, "MATCH (a:Entity)-[:ALIAS_OF]->(b:Entity) RETURN a.id, b.id")),
        "has_chapter": sorted(_rows(
            kuzu, "MATCH (b:Book)-[:HAS_CHAPTER]->(c:Chapter) RETURN b.id, c.id")),
        "has_chunk": sorted(_rows(
            kuzu, "MATCH (c:Chapter)-[:HAS_CHUNK]->(k:Chunk) RETURN c.id, k.id")),
        "books": sorted(_rows(kuzu, "MATCH (b:Book) RETURN b.id, b.title")),
        "lance_chunks": lance.count("chunks"),
        "lance_entities": lance.count("entities"),
    }


class _KuzuFailingOn:
    """Kuzu double that raises the Nth time a marker statement is issued."""

    def __init__(self, inner, marker: str, nth: int = 1) -> None:
        self._inner, self._marker, self._nth, self.hits = inner, marker, nth, 0

    def execute(self, cypher, params=None):
        if self._marker in cypher:
            self.hits += 1
            if self.hits >= self._nth:
                raise RuntimeError("injected mid-write failure")
        return self._inner.execute(cypher, params)


class _LanceFailingOnAdd:
    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def add_chunks(self, rows):
        raise RuntimeError("injected vector-write failure")


@pytest.mark.asyncio
async def test_probe_idempotent_by_query_not_by_trust(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    first = _snapshot(kuzu, lance)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert _snapshot(kuzu, lance) == first
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_empty_and_unnormalizable_entity_names(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0), _chunk(BOOK, 1, 1)]
    _add_book(config, conn, BOOK, chunks, [
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Bo", [], []),
        _row(chunks[1].id, BOOK, "!!!", "LOVES", "Cy", ["!!!", "Cy"],
             ["Character", "Character"]),
    ])
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    names = sorted(r[0] for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.name"))
    assert names == ["Cy"]          # no junk node for "!!!", none for row 1
    assert report.rel_edges == 0    # neither row could resolve both endpoints
    assert report.entities == 1
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_triplet_for_unknown_chunk(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, chunks, [
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
        _row(f"{BOOK}:9:9", BOOK, "Cy", "OPPOSES", "Dee", ["Cy", "Dee"],
             ["Character", "Character"]),
    ])
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert report.rel_edges == 2
    # chapter_seq 0 flags "chunk not in the registry"; chapters are 1-based.
    seqs = sorted(r[0] for r in _rows(
        kuzu, "MATCH ()-[r:REL]->() RETURN r.chapter_seq"))
    assert seqs == [0, 1]
    # The report counts only MENTIONS edges that a Chunk node could anchor.
    mentions = _rows(kuzu, "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) RETURN c.id, e.id")
    assert report.mentions == len(mentions) == 2
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_entity_across_two_chapters(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0), _chunk(BOOK, 2, 0)]
    _add_book(config, conn, BOOK, chunks, [
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
        _row(chunks[1].id, BOOK, "Ana", "OPPOSES", "Cy", ["Ana", "Cy"],
             ["Character", "Character"]),
    ])
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    ana = [r[0] for r in _rows(
        kuzu, "MATCH (e:Entity) WHERE e.name = 'Ana' RETURN e.id")]
    assert len(ana) == 1
    pairs = _rows(
        kuzu,
        "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) WHERE e.name = 'Ana'"
        " RETURN c.id, e.id",
    )
    assert sorted(p[0] for p in pairs) == [chunks[0].id, chunks[1].id]
    assert len(pairs) == 2  # exactly one edge per chunk, not per triplet
    table = lancedb.connect(str(config.lancedb_dir)).open_table("entities")
    vectors = {r["entity_id"]: list(r["vector"]) for r in table.to_arrow().to_pylist()}
    # chunk vectors are [1,0...] and [2,0...]; the centroid covers both.
    assert vectors[ana[0]][0] == pytest.approx(1.5)
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_single_chunk_book(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, chunks, [
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
    ])
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert (report.chunks, report.rel_edges, report.entities) == (1, 1, 2)
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_all_self_referential_triplets(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, chunks, [
        _row(chunks[0].id, BOOK, "Ana", "BECOMES", "Ana", ["Ana"], ["Character"]),
        _row(chunks[0].id, BOOK, "Ana", "EXPERIENCES", "Ana", ["Ana"], ["Character"]),
    ])
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert report.entities == 1
    loops = _rows(kuzu, "MATCH (a:Entity)-[r:REL]->(b:Entity) WHERE a.id = b.id"
                        " RETURN r.predicate")
    assert sorted(loops) == [("BECOMES",), ("EXPERIENCES",)]
    assert report.mentions == 1
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_loading_book_b_leaves_book_a_intact(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    a_chunks = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a_chunks, [
        _row(a_chunks[0].id, BOOK, "Ana", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
    ])
    b_chunks = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b_chunks, [
        _row(b_chunks[0].id, BOOK_B, "Zed", "OPPOSES", "Yan", ["Zed", "Yan"],
             ["Character", "Character"]),
    ])
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    a_rel = sorted(_rows(
        kuzu, "MATCH ()-[r:REL]->() WHERE r.book_id = $b RETURN r.chunk_id,"
              " r.predicate".replace("$b", f"'{BOOK}'")))
    a_mentions = sorted(_rows(
        kuzu, "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) WHERE c.book_id = "
              f"'{BOOK}' RETURN c.id, e.id"))
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, _never)
    assert sorted(_rows(
        kuzu, "MATCH ()-[r:REL]->() WHERE r.book_id = "
              f"'{BOOK}' RETURN r.chunk_id, r.predicate")) == a_rel
    assert sorted(_rows(
        kuzu, "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) WHERE c.book_id = "
              f"'{BOOK}' RETURN c.id, e.id")) == a_mentions
    assert lance.count("chunks") == 2
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_midwrite_graph_failure_rolls_everything_back(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    before = _snapshot(kuzu, lance)
    # Retitle the book so a committed transaction would be visible, then
    # fail on the second REL CREATE — after the deletes and the Book MERGE.
    conn.execute("UPDATE books SET title = 'RETITLED' WHERE id = ?", (BOOK,))
    conn.commit()
    failing = _KuzuFailingOn(kuzu, "CREATE (s)-[:REL", nth=2)
    with pytest.raises(RuntimeError, match="injected mid-write failure"):
        await load_book(config, conn, failing, lance, BOOK, _embed, _never)
    assert _snapshot(kuzu, lance) == before  # deletes and writes both undone
    assert before["books"] == [(BOOK, "T")]  # not 'RETITLED'
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_vector_write_failure_is_loud_and_named(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    with pytest.raises(PartialLoadError, match="vector store"):
        await load_book(config, conn, kuzu, _LanceFailingOnAdd(lance), BOOK,
                        _embed, _never)
    # Honest about what is left: the graph is committed, the book's chunk
    # rows are gone from LanceDB, and a re-run repairs it.
    assert len(_rows(kuzu, "MATCH ()-[r:REL]->() RETURN r.chunk_id")) == 2
    assert lance.count("chunks") == 0
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert report.chunks == lance.count("chunks") == 2
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_missing_artifact_refuses_before_touching_stores(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    before = _snapshot(kuzu, lance)
    (config.extractions_dir / BOOK / UNIT / "triplets.parquet").unlink()
    with pytest.raises(MissingArtifactError):
        await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert _snapshot(kuzu, lance) == before
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_completed_unit_with_no_triplets(tmp_path: Path) -> None:
    """A unit can legitimately complete having extracted nothing."""
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, chunks, [])
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert (report.entities, report.rel_edges, report.mentions) == (0, 0, 0)
    assert report.chunks == lance.count("chunks") == 1  # chunks still land
    assert _rows(kuzu, "MATCH (b:Book) RETURN b.id") == [(BOOK,)]
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_subject_absent_from_entity_names_drops_the_edge(
    tmp_path: Path,
) -> None:
    """No entity to hang the edge on, so the edge is dropped, not faked."""
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, chunks, [
        _row(chunks[0].id, BOOK, "Ghost", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
    ])
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert report.rel_edges == 0
    assert _rows(kuzu, "MATCH ()-[r:REL]->() RETURN r.predicate") == []
    # The entities the row did name are still nodes, still mentioned.
    assert sorted(r[0] for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.name")) == [
        "Ana", "Bo"
    ]
    conn.close(); kuzu.close()


# --------------------------------------------------------------------------
# Fix round 1: cross-book fusion must clear adjudication (SPEC acceptance:
# "two books each containing 'the Captain' must NOT merge without
# adjudication approving it"), and dropped edges must be reported.
# --------------------------------------------------------------------------


class _AdjudicateSpy:
    """Records every adjudication call so tests can assert the count."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, str, str]] = []

    async def __call__(self, canonical, mention) -> bool:
        self.calls.append(
            (canonical.id, canonical.name, mention.name, mention.book_id)
        )
        return self.answer


def _two_captains(config, conn) -> None:
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "the Captain", "OPPOSES", "Bo",
             ["the Captain", "Bo"], ["Character", "Character"]),
    ])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, "the Captain", "LOVES", "Zed",
             ["the Captain", "Zed"], ["Character", "Character"]),
    ])


def _captains(kuzu) -> list[tuple]:
    return sorted(
        (r[0], tuple(sorted(r[1] or ())))
        for r in _rows(
            kuzu, "MATCH (e:Entity) WHERE e.name = 'the Captain'"
                  " RETURN e.id, e.books")
    )


@pytest.mark.asyncio
async def test_two_books_one_captain_refused_stay_separate(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    _two_captains(config, conn)
    spy = _AdjudicateSpy(False)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    assert spy.calls == []  # nothing persisted yet, nothing to adjudicate
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    # Task 1h: "captain" is generic in every token, so the refusal is now
    # deterministic and the model is never asked.
    assert spy.calls == []

    captains = _captains(kuzu)
    assert len(captains) == 2  # two nodes, not one
    assert [books for _id, books in captains] == [(BOOK_B,), (BOOK,)] or \
           [books for _id, books in captains] == [(BOOK,), (BOOK_B,)]
    assert {books for _id, books in captains} == {(BOOK,), (BOOK_B,)}

    # Each captain keeps its own edge.
    edges = sorted(
        (r[0], r[1], r[2])
        for r in _rows(
            kuzu, "MATCH (s:Entity)-[r:REL]->(o:Entity)"
                  " RETURN s.id, r.book_id, o.name")
    )
    assert len(edges) == 2
    by_book = {book: (sid, obj) for sid, book, obj in edges}
    assert by_book[BOOK][1] == "Bo"
    assert by_book[BOOK_B][1] == "Zed"
    assert by_book[BOOK][0] != by_book[BOOK_B][0]  # different subject nodes
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_two_books_one_captain_stay_separate_even_when_approved(
    tmp_path: Path,
) -> None:
    """Task 1h: an approving adjudicator can no longer fuse a generic name.

    This test previously asserted the opposite — that approval merged the
    two captains. The live graph showed the 3B model doing exactly that for
    `the police Inspector` (Lestrade in book one, Athelney Jones in book
    two) while refusing `Sherlock Holmes`, so the generic case is now
    decided by rule and the model's answer is not consulted at all.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    _two_captains(config, conn)
    spy = _AdjudicateSpy(True)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    assert spy.calls == []

    captains = _captains(kuzu)
    assert len(captains) == 2  # still two people
    assert {books for _id, books in captains} == {(BOOK,), (BOOK_B,)}
    edges = sorted(
        (r[0], r[1], r[2])
        for r in _rows(kuzu, "MATCH (s:Entity)-[r:REL]->(o:Entity)"
                             " RETURN s.id, r.book_id, o.name")
    )
    assert len(edges) == 2
    by_book = {book: (sid, obj) for sid, book, obj in edges}
    assert by_book[BOOK][1] == "Bo"
    assert by_book[BOOK_B][1] == "Zed"
    assert by_book[BOOK][0] != by_book[BOOK_B][0]  # two nodes, two edge sets
    conn.close(); kuzu.close()


def _two_holmeses(config, conn) -> None:
    """Both books name the same distinctive person, spelled differently.

    "Sherlock Holmes" and "Mr. Sherlock Holmes" normalize to the identical
    string `sherlock holmes`, so their ids collide across books — the exact
    pair the live graph left unmerged.
    """
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "Sherlock Holmes", "OPPOSES", "Bo",
             ["Sherlock Holmes", "Bo"], ["Character", "Character"]),
    ])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, "Mr. Sherlock Holmes", "LOVES", "Zed",
             ["Mr. Sherlock Holmes", "Zed"], ["Character", "Character"]),
    ])


def _holmeses(kuzu) -> list[tuple]:
    return sorted(
        (r[0], tuple(sorted(r[1] or ())))
        for r in _rows(
            kuzu, "MATCH (e:Entity)"
                  " WHERE e.name IN ['Sherlock Holmes', 'Mr. Sherlock Holmes']"
                  " RETURN e.id, e.books")
    )


@pytest.mark.asyncio
async def test_two_books_one_holmes_merge_without_adjudication(
    tmp_path: Path,
) -> None:
    """A distinctive cross-book homonym merges, and the model is not asked."""
    config, conn, kuzu, lance = _env(tmp_path)
    _two_holmeses(config, conn)
    spy = _AdjudicateSpy(False)  # a model that would refuse, as the 3B does
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    assert spy.calls == []
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    assert spy.calls == []

    holmeses = _holmeses(kuzu)
    assert len(holmeses) == 1  # one node, not two
    assert holmeses[0][1] == tuple(sorted((BOOK, BOOK_B)))  # books unioned

    # Both books' edges hang off the single merged node.
    edges = sorted(
        (r[0], r[1], r[2])
        for r in _rows(kuzu, "MATCH (s:Entity)-[r:REL]->(o:Entity)"
                             " RETURN s.id, r.book_id, o.name")
    )
    assert len(edges) == 2
    assert {e[0] for e in edges} == {holmeses[0][0]}
    assert {(e[1], e[2]) for e in edges} == {(BOOK, "Bo"), (BOOK_B, "Zed")}
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_merged_holmes_survives_a_reload_of_either_book(
    tmp_path: Path,
) -> None:
    """The deterministic merge is idempotent and never re-adjudicated."""
    config, conn, kuzu, lance = _env(tmp_path)
    _two_holmeses(config, conn)
    spy = _AdjudicateSpy(False)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    snapshot = _snapshot(kuzu, lance)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    assert spy.calls == []
    assert _snapshot(kuzu, lance) == snapshot
    assert len(_holmeses(kuzu)) == 1
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_ana_and_the_ana_fuse_across_books_after_task_1h(
    tmp_path: Path,
) -> None:
    """`Ana` and `The Ana` normalize identically and neither token is generic.

    Task 1h inverted this: before it, a refusing adjudicator kept them as
    two nodes. `Ana` is distinctive, so the same rule that merges
    `Sherlock Holmes` with `Mr. Sherlock Holmes` merges these, without the
    model. The round-0 defect this test was written for was a *same-book*
    over-merge; that guarantee lives in the resolver tests.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "Ana", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
    ])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, "The Ana", "OPPOSES", "Zed", ["The Ana", "Zed"],
             ["Character", "Character"]),
    ])
    spy = _AdjudicateSpy(False)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    assert spy.calls == []  # decided by rule, never by the model
    rows = sorted(
        (r[0], r[1], tuple(sorted(r[2] or ())))
        for r in _rows(kuzu, "MATCH (e:Entity) WHERE e.name IN ['Ana', 'The Ana']"
                             " RETURN e.id, e.name, e.books")
    )
    assert len(rows) == 1
    assert rows[0][2] == tuple(sorted((BOOK, BOOK_B)))  # one node, both books
    edges = sorted(
        (r[0], r[1], r[2])
        for r in _rows(kuzu, "MATCH (s:Entity)-[r:REL]->(o:Entity)"
                             " RETURN s.id, r.book_id, o.name")
    )
    assert {(e[1], e[2]) for e in edges} == {(BOOK, "Bo"), (BOOK_B, "Zed")}
    assert {e[0] for e in edges} == {rows[0][0]}
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_reloading_the_same_book_never_adjudicates(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _setup(tmp_path)
    spy = _AdjudicateSpy(False)
    first = await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    assert spy.calls == []
    snapshot = _snapshot(kuzu, lance)
    second = await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    assert spy.calls == []  # same book rewriting itself: never re-adjudicated
    assert _snapshot(kuzu, lance) == snapshot
    assert (second.entities, second.rel_edges, second.chunks) == (
        first.entities, first.rel_edges, first.chunks
    )
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_reloading_after_a_refused_merge_never_readjudicates(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    _two_captains(config, conn)
    spy = _AdjudicateSpy(False)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    assert spy.calls == []  # Task 1h: the generic refusal is deterministic
    snapshot = _snapshot(kuzu, lance)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    assert spy.calls == []  # the suffixed node is found by its own book
    assert _snapshot(kuzu, lance) == snapshot
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_dropped_edges_are_counted(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, chunks, [
        _row(chunks[0].id, BOOK, "Ghost", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Phantom", ["Ana", "Bo"],
             ["Character", "Character"]),
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
    ])
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert report.rel_edges == 1
    assert report.dropped_edges == 2  # unknown subject, then unknown object
    conn.close(); kuzu.close()


# --------------------------------------------------------------------------
# Fix round 2: the LoadReport must agree with what is actually in the stores.
# --------------------------------------------------------------------------


# These centroid/lifecycle tests need an entity that genuinely SPANS both
# books. Task 1h made "the Captain" un-mergeable by rule (every token
# generic), so they carry a distinctive shared name instead; what they
# assert — the centroid, and the book-removal lifecycle — is unchanged.
_SHARED = "Mary Morstan"


def _shared_nodes(kuzu) -> list[tuple]:
    return sorted(
        (r[0], tuple(sorted(r[1] or ())))
        for r in _rows(
            kuzu, f"MATCH (e:Entity) WHERE e.name = '{_SHARED}'"
                  " RETURN e.id, e.books")
    )


async def _load_two_shared_merged(tmp_path: Path, order: list[str]):
    """A's chunk vector is 10.0, B's is 20.0, and the name is distinctive."""
    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, _SHARED, "OPPOSES", "Bo",
             [_SHARED, "Bo"], ["Character", "Character"]),
    ], firsts=[10.0])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, _SHARED, "LOVES", "Zed",
             [_SHARED, "Zed"], ["Character", "Character"]),
    ], firsts=[20.0])
    spy = _AdjudicateSpy(True)
    for book in order:
        await load_book(config, conn, kuzu, lance, book, _embed, spy)
    return config, conn, kuzu, lance


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [[BOOK, BOOK_B], [BOOK_B, BOOK]])
async def test_merged_entity_centroid_is_the_true_mean(
    tmp_path: Path, order: list[str]
) -> None:
    config, conn, kuzu, lance = await _load_two_shared_merged(tmp_path, order)
    shared = _shared_nodes(kuzu)
    assert len(shared) == 1
    assert shared[0][1] == tuple(sorted((BOOK, BOOK_B)))
    vectors = _entity_vectors(config)
    # 10.0 from book A's chunk, 20.0 from book B's — the mean is 15.0, and
    # it must not depend on which book was loaded last.
    assert vectors[shared[0][0]][0] == pytest.approx(15.0)
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_merged_centroid_survives_a_reload_of_either_book(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance = await _load_two_shared_merged(
        tmp_path, [BOOK, BOOK_B]
    )
    shared = _shared_nodes(kuzu)[0][0]
    spy = _AdjudicateSpy(True)
    for book in (BOOK, BOOK_B, BOOK, BOOK_B):
        await load_book(config, conn, kuzu, lance, book, _embed, spy)
        assert _entity_vectors(config)[shared][0] == pytest.approx(15.0)
    assert spy.calls == []  # both books already own the node
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_reload_after_reextraction_drops_stale_entities(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0), _chunk(BOOK, 1, 1)]
    both = [
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
        _row(chunks[1].id, BOOK, "Cy", "OPPOSES", "Dee", ["Cy", "Dee"],
             ["Character", "Character"]),
    ]
    _add_book(config, conn, BOOK, chunks, both)
    first = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert first.entities == 4
    assert lance.count("entities") == 4

    # Re-extract: Cy and Dee are gone from the artifacts.
    _write_unit(config, BOOK, both[:1], [c.id for c in chunks])
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)

    assert report.entities == 2
    names = sorted(r[0] for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.name"))
    assert names == ["Ana", "Bo"]  # no orphaned Cy/Dee nodes left behind
    assert lance.count("entities") == 2
    assert _entity_vectors(config).keys() == {
        r[0] for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.id")
    }
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_shared_entity_survives_reload_with_this_book_removed(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0), _chunk(BOOK, 1, 1)]
    a_rows = [
        _row(a[0].id, BOOK, _SHARED, "OPPOSES", "Bo",
             [_SHARED, "Bo"], ["Character", "Character"]),
        _row(a[1].id, BOOK, "Ana", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
    ]
    _add_book(config, conn, BOOK, a, a_rows, firsts=[10.0, 30.0])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, _SHARED, "LOVES", "Zed",
             [_SHARED, "Zed"], ["Character", "Character"]),
    ], firsts=[20.0])
    spy = _AdjudicateSpy(True)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    shared = _shared_nodes(kuzu)[0][0]
    assert _entity_vectors(config)[shared][0] == pytest.approx(15.0)

    # Re-extract book A without the shared character.
    _write_unit(config, BOOK, a_rows[1:], [c.id for c in a], firsts=[10.0, 30.0])
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)

    survivors = sorted(
        (r[0], r[1], tuple(sorted(r[2] or ())))
        for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.id, e.name, e.books")
    )
    kept = [s for s in survivors if s[1] == _SHARED]
    assert len(kept) == 1                        # retained, not deleted
    assert kept[0][2] == (BOOK_B,)               # this book removed from books
    # Recomputed over book B's chunk alone.
    assert _entity_vectors(config)[shared][0] == pytest.approx(20.0)
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_stale_entity_removal_is_inside_the_transaction(
    tmp_path: Path,
) -> None:
    """A failure after the orphan deletes must put the orphans back."""
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0), _chunk(BOOK, 1, 1)]
    both = [
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
        _row(chunks[1].id, BOOK, "Cy", "OPPOSES", "Dee", ["Cy", "Dee"],
             ["Character", "Character"]),
    ]
    _add_book(config, conn, BOOK, chunks, both)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    before = _snapshot(kuzu, lance)
    assert len(before["entities"]) == 4

    # Re-extract without Cy/Dee, then fail after the DETACH DELETEs have run.
    _write_unit(config, BOOK, both[:1], [c.id for c in chunks])
    failing = _KuzuFailingOn(kuzu, "CREATE (s)-[:REL", nth=1)
    with pytest.raises(RuntimeError, match="injected mid-write failure"):
        await load_book(config, conn, failing, lance, BOOK, _embed, _never)
    assert _snapshot(kuzu, lance) == before  # Cy and Dee are back
    conn.close(); kuzu.close()


# --------------------------------------------------------------------------
# Final fix round, item 1: the loader must not reintroduce order-dependence.
#
# `resolve_mentions` is a function of the mention SET. `_mentions_from` used
# to hand it a set whose per-mention `context` was the FIRST row's evidence,
# so permuting a unit's triplet rows — which a `--resume` rerun or a changed
# `work_unit_size` legitimately does — changed the resolver's sort key, the
# pass-2 embedding text and the pass-3 prompt, and therefore the graph.
# --------------------------------------------------------------------------

# Two evidences with almost no vocabulary in common. Under the token-set
# embedder below, a mention carrying `_SHARED_EV` sits above the resolver's
# same-book floor against the Captain (whose only evidence is `_SHARED_EV`),
# and one carrying `_OTHER_EV` sits below its reject floor — so which of a
# row's evidences becomes an entity's context decides whether two entities
# merge, with no LLM anywhere in the loop.
_SHARED_EV = (
    "the wind tore across the open deck while ropes snapped and sails"
    " cracked overhead"
)
_OTHER_EV = (
    "a quiet garden of flowers where tea and cake waited on a painted table"
)


def _content_vector(text: str) -> list[float]:
    """A deterministic, purely content-derived embedding: a token indicator.

    cosine(a, b) is then |A∩B| / sqrt(|A|·|B|), so a fixture can place a
    pair of mentions deliberately above or below a resolver floor without
    calling a model.
    """
    vector = [0.0] * EMBED_DIM
    for token in re.findall(r"\w+", text.lower()):
        vector[int(hashlib.sha1(token.encode()).hexdigest(), 16) % EMBED_DIM] = 1.0
    return vector


async def _content_embed(texts):
    return [_content_vector(text) for text in texts]


def _chunk_bridge(config) -> list[tuple[str, list[str]]]:
    """Each chunk's LanceDB `entity_ids`, the Phase 3 retrieval filter."""
    table = lancedb.connect(str(config.lancedb_dir)).open_table("chunks")
    return sorted(
        (row["chunk_id"], sorted(row["entity_ids"]))
        for row in table.to_arrow().to_pylist()
    )


def _order_fixture(chunks):
    """Four rows over one book. Vane is named by two rows carrying different
    evidence, so the old first-row-wins context was permutation-dependent."""
    return [
        _row(chunks[0].id, BOOK, "the Captain", "COMMANDS", "Ship",
             ["the Captain", "Ship"], ["Character", "Vessel"],
             evidence=_SHARED_EV),
        _row(chunks[1].id, BOOK, "Vane", "OWNS", "Sword",
             ["Vane", "Sword"], ["Character", "Weapon"], evidence=_SHARED_EV),
        _row(chunks[2].id, BOOK, "Vane", "LIKES", "Tea",
             ["Vane", "Tea"], ["Character", "Drink"], evidence=_OTHER_EV),
        _row(chunks[3].id, BOOK, "Anna", "READS", "Book",
             ["Anna", "Book"], ["Character", "Tome"],
             evidence="a still page turned in the lamplit study"),
    ]


@pytest.mark.asyncio
async def test_triplet_row_order_never_changes_the_graph(tmp_path: Path) -> None:
    """All 24 orderings of one triplet set must load to one identical graph."""
    snapshots: list[tuple[tuple[int, ...], dict]] = []
    for index, permutation in enumerate(itertools.permutations(range(4))):
        config, conn, kuzu, lance = _env(tmp_path / f"perm{index}")
        chunks = [_chunk(BOOK, 1, i) for i in range(4)]
        rows = _order_fixture(chunks)
        _add_book(config, conn, BOOK, chunks, [rows[i] for i in permutation])
        report = await load_book(
            config, conn, kuzu, lance, BOOK, _content_embed, _never
        )
        snapshot = _snapshot(kuzu, lance)
        snapshot["bridge"] = _chunk_bridge(config)
        snapshot["report"] = (report.entities, report.alias_links,
                              report.rel_edges, report.mentions,
                              report.dropped_edges)
        snapshots.append((permutation, snapshot))
        conn.close(); kuzu.close()

    baseline = snapshots[0][1]
    differing = [p for p, s in snapshots if s != baseline]
    assert not differing, (
        f"{len(differing)} of {len(snapshots)} row orderings produced a"
        f" different graph, e.g. {differing[0]}:\n"
        f"  baseline entities: {baseline['entities']}\n"
        f"  variant  entities: "
        f"{dict(snapshots)[differing[0]]['entities']}"
    )


# --------------------------------------------------------------------------
# Final fix round, item 2: a REL edge's endpoints must be MENTIONed by the
# chunk the edge hangs on. `by_surface` is book-global, so a row whose
# subject is absent from its OWN entity_names still gets an edge; the
# endpoint is already resolved, so the mention is added rather than the
# faithfully-extracted edge dropped.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edge_endpoints_are_always_mentioned_by_their_chunk(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0), _chunk(BOOK, 1, 1)]
    _add_book(config, conn, BOOK, chunks, [
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Cy", ["Ana", "Cy"],
             ["Character", "Character"]),
        # Chunk 1 names only Cy, but still carries an Ana -> Cy edge.
        _row(chunks[1].id, BOOK, "Ana", "LOVES", "Cy", ["Cy"], ["Character"]),
    ])
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert (report.rel_edges, report.dropped_edges) == (2, 0)

    mentions = set(_rows(
        kuzu, "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) RETURN c.id, e.id"))
    edges = _rows(
        kuzu,
        "MATCH (a:Entity)-[r:REL]->(b:Entity) RETURN a.id, b.id, r.chunk_id",
    )
    assert len(edges) == 2
    for subject_id, object_id, chunk_id in edges:
        assert (chunk_id, subject_id) in mentions, "subject not MENTIONed"
        assert (chunk_id, object_id) in mentions, "object not MENTIONed"
    assert report.mentions == len(mentions)

    # The same coverage must reach LanceDB, or entity-filtered vector
    # retrieval cannot return the chunk that evidences the edge.
    ids = {name: eid for name, eid in _rows(
        kuzu, "MATCH (e:Entity) RETURN e.name, e.id")}
    bridge = dict(_chunk_bridge(config))
    assert bridge[chunks[1].id] == sorted([ids["Ana"], ids["Cy"]])
    conn.close(); kuzu.close()


# Task 1d: reject non-entities at load time. `dropped_mentions` must count
# exactly the rejected names, not be trusted on faith.


@pytest.mark.asyncio
async def test_non_entity_mentions_are_dropped_and_counted(tmp_path: Path) -> None:
    """"the box" and "his hands" are definite descriptions, not entities.

    Both are listed among the row's entity_names alongside the real
    subject/object pair (Ana, Bo), so this isolates dropped_mentions from
    dropped_edges: the edge still resolves cleanly because its own
    endpoints are real names, while the two junk names are rejected.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, chunks, [
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Bo",
             ["Ana", "Bo", "the box", "his hands"],
             ["Character", "Character", "Object", "Concept"]),
    ])
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert report.dropped_mentions == 2   # "the box" and "his hands"
    assert report.rel_edges == 1          # Ana -> Bo still resolves cleanly
    assert report.dropped_edges == 0
    assert report.entities == 2
    names = sorted(r[0] for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.name"))
    assert names == ["Ana", "Bo"]          # no node minted for either junk name
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_dropped_mentions_counts_exactly_the_rejections(
    tmp_path: Path,
) -> None:
    """Build a fixture with a known number of rejects and count independently.

    Rather than trusting the implementation, this test re-derives the
    expected count from the raw fixture data using the same predicate the
    loader uses, and asserts the report agrees.
    """
    from lore.resolve.entityhood import is_probable_entity
    from lore.resolve.normalize import normalize_name

    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [_chunk(BOOK, 1, 0), _chunk(BOOK, 1, 1)]
    rows = [
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Bo",
             ["Ana", "Bo", "the weather", "my curiosity"],
             ["Character", "Character", "Concept", "Concept"]),
        _row(chunks[1].id, BOOK, "Cy", "OPPOSES", "Dee",
             ["Cy", "Dee", "the pavement"],
             ["Character", "Character", "Object"]),
    ]
    _add_book(config, conn, BOOK, chunks, rows)

    expected_drops = sum(
        1
        for row in rows
        for name in row["entity_names"]
        if not normalize_name(name) or not is_probable_entity(name)
    )
    assert expected_drops == 3  # sanity: the fixture names 3 junk mentions

    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert report.dropped_mentions == expected_drops == 3
    conn.close(); kuzu.close()


# --------------------------------------------------------------------------
# Task 1h self-review probe: the deterministic cross-book rule meets the
# `claimed` set. The live graph contains four nodes that normalize to
# `sherlock holmes` ("Sherlock Holmes" and "Mr. Sherlock Holmes", twice
# over), so a single load CAN present two candidates sharing one base while
# another book already holds that base id. Nothing in the brief lists this.
# --------------------------------------------------------------------------


def _candidate(name: str, slot: int = 0, kind: str = "Character") -> "object":
    """A candidate at position `slot` of its own base's collision chain.

    `slot=1` is what `resolve_mentions` mints for a second, distinct entity
    of the same normalized name inside ONE book, so a load can legitimately
    hand `_reconcile_ids` two candidates that share a base but not an id.
    """
    from lore.graph.loader import _NodeCandidate
    from lore.resolve.normalize import normalize_name
    from lore.resolve.resolver import entity_id_chain

    base = normalize_name(name)
    cid = next(itertools.islice(entity_id_chain(base, kind), slot, slot + 1))
    return _NodeCandidate(id=cid, base=base, name=name, kind=kind,
                          aliases=(base,), books=())


@pytest.mark.asyncio
async def test_probe_two_same_base_candidates_meet_one_foreign_node(
    tmp_path: Path,
) -> None:
    from lore.graph.loader import _reconcile_ids

    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "Sherlock Holmes", "OPPOSES", "Bo",
             ["Sherlock Holmes", "Bo"], ["Character", "Character"]),
    ])
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    persisted_id = _rows(
        kuzu, "MATCH (e:Entity) WHERE e.name = 'Sherlock Holmes' RETURN e.id"
    )[0][0]

    first = _candidate("Sherlock Holmes")
    second = _candidate("Mr. Sherlock Holmes", slot=1)
    assert first.base == second.base == "sherlock holmes"
    id_for, _ = await _reconcile_ids(kuzu, BOOK_B, [first, second])

    # One of them fuses into book A's node; the other takes a free slot in
    # the chain rather than stealing the same id or crashing.
    chosen = {id_for[first.id], id_for[second.id]}
    assert len(chosen) == 2
    assert persisted_id in chosen
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_two_same_base_generic_candidates_take_free_slots(
    tmp_path: Path,
) -> None:
    from lore.graph.loader import _reconcile_ids

    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "the Captain", "OPPOSES", "Bo",
             ["the Captain", "Bo"], ["Character", "Character"]),
    ])
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    persisted_id = _rows(
        kuzu, "MATCH (e:Entity) WHERE e.name = 'the Captain' RETURN e.id"
    )[0][0]

    first = _candidate("the Captain")
    second = _candidate("Captain", slot=1)
    assert first.base == second.base == "captain"
    id_for, _ = await _reconcile_ids(kuzu, BOOK_B, [first, second])

    chosen = {id_for[first.id], id_for[second.id]}
    assert len(chosen) == 2
    assert persisted_id not in chosen  # generic: book A's node is untouched
    conn.close(); kuzu.close()


# --------------------------------------------------------------------------
# Task 1i defect 1: cross-book merging used to fire only on IDENTICAL names.
# `Watson` (book one) and `Dr. John Watson` (book two) normalize to `watson`
# and `john watson`, so their content-derived ids never collided and
# `_reconcile_ids` never considered them. The same-book path got a token
# subset rule in Task 1g; these tests extend it to the cross-book path.


def _two_watsons(config, conn, *, kind_a="Character", kind_b="Character",
                 name_a="Dr. John Watson", name_b="Watson") -> None:
    """Book A names him in full, book B by surname alone."""
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, name_a, "OPPOSES", "Bo",
             [name_a, "Bo"], [kind_a, "Character"]),
    ])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, name_b, "LOVES", "Zed",
             [name_b, "Zed"], [kind_b, "Character"]),
    ])


def _watsons(kuzu) -> list[tuple]:
    return sorted(
        (r[0], r[1], tuple(sorted(r[2] or ())), tuple(sorted(r[3] or ())))
        for r in _rows(
            kuzu,
            "MATCH (e:Entity) WHERE e.name IN ['Dr. John Watson', 'Watson']"
            " RETURN e.id, e.name, e.books, e.aliases",
        )
    )


@pytest.mark.asyncio
async def test_a_cross_book_name_variant_merges(tmp_path: Path) -> None:
    """`watson` nests inside `john watson`, both distinctive: one entity."""
    config, conn, kuzu, lance = _env(tmp_path)
    _two_watsons(config, conn)
    spy = _AdjudicateSpy(False)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    assert spy.calls == []  # decided by rule, never by the 3B model

    watsons = _watsons(kuzu)
    assert len(watsons) == 1  # one node, not two
    node_id, name, books, aliases = watsons[0]
    assert books == tuple(sorted((BOOK, BOOK_B)))
    assert name == "Dr. John Watson"  # the longest raw surface form
    assert aliases == ("john watson", "watson")

    edges = sorted(
        (r[0], r[1], r[2])
        for r in _rows(kuzu, "MATCH (s:Entity)-[r:REL]->(o:Entity)"
                             " RETURN s.id, r.book_id, o.name")
    )
    assert len(edges) == 2
    assert {e[0] for e in edges} == {node_id}  # both books' edges on one node
    assert {(e[1], e[2]) for e in edges} == {(BOOK, "Bo"), (BOOK_B, "Zed")}
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_a_generic_cross_book_name_variant_does_not_merge(
    tmp_path: Path,
) -> None:
    """`inspector` nests inside `police inspector`, but both are generic.

    Lestrade in one novel and Athelney Jones in the other are two people;
    the subset shape must not override the Task 1h stoplist.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "the police Inspector", "OPPOSES", "Bo",
             ["the police Inspector", "Bo"], ["Character", "Character"]),
    ])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, "Inspector", "LOVES", "Zed",
             ["Inspector", "Zed"], ["Character", "Character"]),
    ])
    spy = _AdjudicateSpy(True)  # even an approving model cannot fuse these
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    assert spy.calls == []

    rows = sorted(
        (r[0], tuple(sorted(r[1] or ())))
        for r in _rows(
            kuzu,
            "MATCH (e:Entity) WHERE e.name IN ['the police Inspector',"
            " 'Inspector'] RETURN e.id, e.books")
    )
    assert len(rows) == 2  # two people, two nodes
    assert {books for _id, books in rows} == {(BOOK,), (BOOK_B,)}
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_a_cross_book_name_variant_of_another_kind_does_not_merge(
    tmp_path: Path,
) -> None:
    """A Place named `Watson` is not the Character `Dr. John Watson`."""
    config, conn, kuzu, lance = _env(tmp_path)
    _two_watsons(config, conn, kind_b="Place")
    spy = _AdjudicateSpy(True)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    assert spy.calls == []

    watsons = _watsons(kuzu)
    assert len(watsons) == 2
    assert {books for _id, _n, books, _a in watsons} == {(BOOK,), (BOOK_B,)}
    conn.close(); kuzu.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [[BOOK, BOOK_B], [BOOK_B, BOOK]])
async def test_cross_book_variant_merge_is_independent_of_load_order(
    tmp_path: Path, order: list[str],
) -> None:
    """Whichever book loads first, the graph says the same thing.

    Everything a caller can read off the entity — how many nodes there are,
    the display name, the kind, the books, the alias set and which node each
    book's edges hang on — is a function of the two books' mentions and not
    of which book was loaded first. The node's *id* is the one exception and
    is asserted in the probe below.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    _two_watsons(config, conn)
    for book_id in order:
        await load_book(config, conn, kuzu, lance, book_id, _embed, _never)

    watsons = _watsons(kuzu)
    assert len(watsons) == 1
    node_id, name, books, aliases = watsons[0]
    assert (name, books, aliases) == (
        "Dr. John Watson", tuple(sorted((BOOK, BOOK_B))), ("john watson", "watson")
    )
    edges = sorted(
        (r[0], r[1], r[2])
        for r in _rows(kuzu, "MATCH (s:Entity)-[r:REL]->(o:Entity)"
                             " RETURN s.id, r.book_id, o.name")
    )
    assert {e[0] for e in edges} == {node_id}
    assert {(e[1], e[2]) for e in edges} == {(BOOK, "Bo"), (BOOK_B, "Zed")}
    conn.close(); kuzu.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [[BOOK, BOOK_B], [BOOK_B, BOOK]])
async def test_a_variant_merge_survives_a_reload_of_either_book(
    tmp_path: Path, order: list[str],
) -> None:
    """The merged node is found again on reload, not split back in two.

    A variant merge adopts an id derived from the OTHER book's name, so the
    re-loading book no longer mints that id from its own. Without a way back
    to the node it just merged into, a second `lore load` would mint a fresh
    node and strip this book from the merged one's `books`.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    _two_watsons(config, conn)
    for book_id in order:
        await load_book(config, conn, kuzu, lance, book_id, _embed, _never)
    snapshot = _snapshot(kuzu, lance)
    for book_id in list(reversed(order)) + order:
        await load_book(config, conn, kuzu, lance, book_id, _embed, _never)
    assert _snapshot(kuzu, lance) == snapshot
    assert len(_watsons(kuzu)) == 1
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_a_variant_pair_that_nests_with_only_some_aliases_stays_split(
    tmp_path: Path,
) -> None:
    """Task 1g's every-alias requirement, lifted to the cross-book path.

    Book A's entity carries two surface forms. Book B's `Sholto` nests with
    one of them and not the other, so it is NOT the same entity — this is
    the shape that chained `mary` -> `mary morstan` -> `morstan` -> Captain
    Morstan on the live corpus when the rule was pairwise.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0), _chunk(BOOK, 1, 1)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "Bartholomew Sholto", "OPPOSES", "Bo",
             ["Bartholomew Sholto", "Bo"], ["Character", "Character"]),
        _row(a[1].id, BOOK, "Bartholomew", "OPPOSES", "Bo",
             ["Bartholomew", "Bo"], ["Character", "Character"]),
    ])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, "Sholto", "LOVES", "Zed",
             ["Sholto", "Zed"], ["Character", "Character"]),
    ])
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, _never)

    rows = sorted(
        (r[0], tuple(sorted(r[1] or ())))
        for r in _rows(
            kuzu,
            "MATCH (e:Entity) WHERE e.name IN ['Bartholomew Sholto',"
            " 'Bartholomew', 'Sholto'] RETURN e.id, e.books")
    )
    assert {books for _id, books in rows} == {(BOOK,), (BOOK_B,)}
    assert any(books == (BOOK_B,) for _id, books in rows)
    conn.close(); kuzu.close()


# --------------------------------------------------------------------------
# Task 1i defect 2: Gutenberg header / licence chunks minted entities.


_GUTENBERG_LICENCE = (
    "PLEASE READ THIS BEFORE YOU DISTRIBUTE OR USE THIS WORK Section 1. "
    "1.A. 1.B. YOU AGREE THAT THE FOUNDATION, THE TRADEMARK OWNER, AND ANY "
    "DISTRIBUTOR UNDER THIS AGREEMENT WILL NOT BE LIABLE TO YOU. The "
    "Foundation is committed to complying with the laws regulating charities "
    "and charitable donations in all 50 states of the United States."
)


@pytest.mark.asyncio
async def test_gutenberg_boilerplate_chunks_mint_no_entities(
    tmp_path: Path,
) -> None:
    """The licence chunk's triplets never enter the graph at all."""
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [
        _chunk(BOOK, 1, 0, text="Ana met Bo in the hall."),
        _chunk(BOOK, 2, 0, text=_GUTENBERG_LICENCE),
    ]
    _add_book(config, conn, BOOK, chunks, [
        _row(chunks[0].id, BOOK, "Ana", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
        _row(chunks[1].id, BOOK, "The Foundation", "POSSESSES", "the IRS",
             ["The Foundation", "the IRS", "TRADEMARK_OWNER", "DISTRIBUTOR",
              "FOUNDATION", "Arthur Conan Doyle",
              "50 states of the United States"],
             ["Character", "Object", "Place", "Place", "Place", "Faction",
              "Object"]),
    ])
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)

    names = {r[0] for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.name")}
    assert names == {"Ana", "Bo"}
    for banned in ("The Foundation", "the IRS", "TRADEMARK_OWNER",
                   "DISTRIBUTOR", "FOUNDATION", "Arthur Conan Doyle",
                   "50 states of the United States"):
        assert banned not in names
    # ...and neither do its edges.
    assert report.rel_edges == 1
    assert _rows(kuzu, "MATCH ()-[r:REL]->() RETURN r.predicate") == [("LOVES",)]
    # The chunk itself is still the registry's faithful record of the file.
    assert report.boilerplate_chunks == 1
    assert len(_rows(kuzu, "MATCH (c:Chunk) RETURN c.id")) == 2
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_boilerplate_does_not_reach_across_to_ordinary_chunks(
    tmp_path: Path,
) -> None:
    """A name the licence also uses still survives from ordinary prose.

    The rule is keyed on the passage, not on the name: `The Foundation` in a
    story chunk is an ordinary entity.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    chunks = [
        _chunk(BOOK, 1, 0, text="The Foundation stone was laid by Ana."),
        _chunk(BOOK, 2, 0, text=_GUTENBERG_LICENCE),
    ]
    _add_book(config, conn, BOOK, chunks, [
        _row(chunks[0].id, BOOK, "The Foundation", "LOVES", "Ana",
             ["The Foundation", "Ana"], ["Faction", "Character"]),
        _row(chunks[1].id, BOOK, "The Foundation", "POSSESSES", "the IRS",
             ["The Foundation", "the IRS"], ["Faction", "Object"]),
    ])
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    names = {r[0] for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.name")}
    assert names == {"The Foundation", "Ana"}
    assert "the IRS" not in names
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_a_cross_book_surname_matching_two_characters_merges_with_neither(
    tmp_path: Path,
) -> None:
    """The Sholtos again, this time across the persistence seam.

    Book A names the two sons; book B names their father, whose honorific
    `normalize_name` strips down to the bare `Sholto`. It nests inside both
    sons, so it is a family and not a person: three nodes, not one.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0), _chunk(BOOK, 1, 1)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "Thaddeus Sholto", "OPPOSES", "Bo",
             ["Thaddeus Sholto", "Bo"], ["Character", "Character"]),
        _row(a[1].id, BOOK, "Bartholomew Sholto", "OPPOSES", "Bo",
             ["Bartholomew Sholto", "Bo"], ["Character", "Character"]),
    ])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, "Major Sholto", "LOVES", "Zed",
             ["Major Sholto", "Zed"], ["Character", "Character"]),
    ])
    spy = _AdjudicateSpy(True)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)

    sholtos = sorted(
        (r[0], r[1], tuple(sorted(r[2] or ())))
        for r in _rows(
            kuzu,
            "MATCH (e:Entity) WHERE e.name IN ['Thaddeus Sholto',"
            " 'Bartholomew Sholto', 'Major Sholto'] RETURN e.id, e.name, e.books")
    )
    assert len(sholtos) == 3
    assert {name for _id, name, _b in sholtos} == {
        "Thaddeus Sholto", "Bartholomew Sholto", "Major Sholto"
    }
    # No node claims both books.
    assert all(len(books) == 1 for _id, _n, books in sholtos)
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_two_cross_book_candidates_matching_one_node_merge_with_neither(
    tmp_path: Path,
) -> None:
    """The other direction of the same ambiguity.

    Book A has the bare `Major Sholto`; book B has both sons. Each son
    matches the persisted surname on its own, so a rule that only counted
    matches per CANDIDATE would give the node to whichever son was reached
    first. Uniqueness has to hold both ways.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "Major Sholto", "OPPOSES", "Bo",
             ["Major Sholto", "Bo"], ["Character", "Character"]),
    ])
    b = [_chunk(BOOK_B, 1, 0), _chunk(BOOK_B, 1, 1)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, "Thaddeus Sholto", "LOVES", "Zed",
             ["Thaddeus Sholto", "Zed"], ["Character", "Character"]),
        _row(b[1].id, BOOK_B, "Bartholomew Sholto", "LOVES", "Zed",
             ["Bartholomew Sholto", "Zed"], ["Character", "Character"]),
    ])
    spy = _AdjudicateSpy(True)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)

    rows = sorted(
        (r[1], tuple(sorted(r[2] or ())))
        for r in _rows(
            kuzu,
            "MATCH (e:Entity) WHERE e.name IN ['Thaddeus Sholto',"
            " 'Bartholomew Sholto', 'Major Sholto'] RETURN e.id, e.name, e.books")
    )
    assert len(rows) == 3
    assert all(len(books) == 1 for _n, books in rows)
    conn.close(); kuzu.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted_name, incoming_name", [
    # Persisted generic, incoming distinctive.
    ("the police Inspector", "Inspector Lestrade"),
    # Persisted distinctive, incoming generic.
    ("Inspector Lestrade", "the police Inspector"),
])
async def test_a_variant_pair_with_one_generic_side_does_not_merge(
    tmp_path: Path, persisted_name: str, incoming_name: str,
) -> None:
    """`police inspector` nests inside `lestrade`? No — but `inspector` does.

    `normalize_name` leaves `the police Inspector` as `police inspector` and
    strips nothing from `Inspector Lestrade`, so their tokens do not nest and
    this pair never reaches the rule. The case that DOES reach it is a bare
    generic against a name containing it, so both directions are asserted
    below with `Inspector` itself.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    _two_watsons(config, conn, name_a=persisted_name, name_b=incoming_name)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, _never)
    rows = sorted(
        (r[1], tuple(sorted(r[2] or ())))
        for r in _rows(
            kuzu, "MATCH (e:Entity) WHERE e.name IN $names"
                  " RETURN e.id, e.name, e.books",
        )
        if r[1] in (persisted_name, incoming_name)
    )
    assert len(rows) == 2
    assert {books for _n, books in rows} == {(BOOK,), (BOOK_B,)}
    conn.close(); kuzu.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted_name, incoming_name", [
    ("Inspector", "Inspector Lestrade"),   # persisted generic
    ("Inspector Lestrade", "Inspector"),   # incoming generic
])
async def test_a_bare_generic_never_merges_into_a_distinctive_name(
    tmp_path: Path, persisted_name: str, incoming_name: str,
) -> None:
    """`inspector` nests inside `inspector lestrade`, and must not merge.

    The Task 1h stoplist is applied to BOTH sides, not only to the incoming
    candidate: whichever book loads first, a bare role word can name a
    different person in the other novel.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    _two_watsons(config, conn, name_a=persisted_name, name_b=incoming_name)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, _never)
    rows = sorted(
        (r[1], tuple(sorted(r[2] or ())))
        for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.id, e.name, e.books")
        if r[1] in ("Inspector", "Inspector Lestrade")
    )
    assert len(rows) == 2
    assert {books for _n, books in rows} == {(BOOK,), (BOOK_B,)}
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_probe_the_variant_merge_adopts_the_first_loaded_books_id(
    tmp_path: Path,
) -> None:
    """The one thing a variant merge CANNOT make order-independent.

    An identical-name cross-book merge (Task 1h) is order-free because both
    books hash to the same id. A VARIANT merge cannot be: the two names hash
    differently, and the second book adopts the id the first one minted,
    because a Kuzu primary key cannot be moved without recreating the node
    and every edge another book already hung on it.

    So the id is pinned here rather than left to be discovered later. Every
    other property of the node — name, kind, books, aliases, which edges
    hang on it — is order-independent and asserted in
    `test_cross_book_variant_merge_is_independent_of_load_order`.
    """
    ids = {}
    for order in ([BOOK, BOOK_B], [BOOK_B, BOOK]):
        root = tmp_path / order[0]
        root.mkdir()
        config, conn, kuzu, lance = _env(root)
        _two_watsons(config, conn)
        for book_id in order:
            await load_book(config, conn, kuzu, lance, book_id, _embed, _never)
        watsons = _watsons(kuzu)
        assert len(watsons) == 1
        ids[order[0]] = watsons[0][0]
        conn.close(); kuzu.close()

    assert ids[BOOK] == entity_id("john watson", "Character")   # A minted it
    assert ids[BOOK_B] == entity_id("watson", "Character")      # B minted it
    assert ids[BOOK] != ids[BOOK_B]


# --------------------------------------------------------------------------
# Task 1j: the ambiguous residue reaches a pending store, and a cached
# answer merges it on the next load. The API is never called from here —
# `lore disambiguate` is a separate, deliberate command.


def _setup_ambiguous(tmp_path: Path):
    """A book whose bare surname nests inside two different full names.

    `Major Sholto` normalizes to `sholto`, which is a subset of both
    `bartholomew sholto` and `thaddeus sholto` — the murdered father and
    his two sons. Task 1i refuses that merge; Task 1j records it.
    """
    config = LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1, scene_seq=0,
              seq=i, text=f"passage {i}")
        for i in range(2)
    ]
    ingest_book(conn, book_id=BOOK, title="Sign", author="ACD", source="upload",
                file_path="/x/b.epub", chunks=chunks)
    d = config.extractions_dir / BOOK / UNIT
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({
            "chunk_id": [f"{BOOK}:1:0", f"{BOOK}:1:1"],
            "book_id": [BOOK, BOOK],
            "subject": ["Major Sholto", "Thaddeus Sholto"],
            "predicate": ["FATHER_OF", "BROTHER_OF"],
            "object": ["Bartholomew Sholto", "Bartholomew Sholto"],
            "evidence": ["a: he had been dead for years",
                         "b: the twin brother"],
            "confidence": [0.9, 0.8],
            "entity_names": [["Major Sholto", "Bartholomew Sholto"],
                             ["Thaddeus Sholto", "Bartholomew Sholto"]],
            "entity_kinds": [["Character", "Character"],
                             ["Character", "Character"]],
            "extractor_model": ["m", "m"],
            "run_id": [UNIT, UNIT],
        }),
        d / "triplets.parquet",
    )
    pq.write_table(
        pa.table({
            "chunk_id": [f"{BOOK}:1:0", f"{BOOK}:1:1"],
            "vector": [[0.0] * EMBED_DIM, [0.0] * EMBED_DIM],
            "embed_model": ["e", "e"],
        }),
        d / "embeddings.parquet",
    )
    set_unit_state(conn, BOOK, UNIT, "completed")
    return config, conn, KuzuStore(config.kuzu_dir / "graph"), LanceStore(
        config.lancedb_dir
    )


@pytest.mark.asyncio
async def test_a_load_without_a_store_touches_no_disambiguation_state(
    tmp_path: Path
) -> None:
    # `lore process` with no key and no config must behave exactly as it did
    # before Task 1j: three entities, and not so much as a directory made.
    config, conn, kuzu, lance = _setup_ambiguous(tmp_path)
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    assert report.entities == 3
    assert not (config.data_dir / "disambiguation").exists()


@pytest.mark.asyncio
async def test_a_load_records_the_ambiguous_question(tmp_path: Path) -> None:
    from lore.resolve.disambiguation import DisambiguationStore

    config, conn, kuzu, lance = _setup_ambiguous(tmp_path)
    store = DisambiguationStore(config.disambiguation_dir)
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never,
                             disambiguation=store)
    assert report.entities == 3, "recording changes nothing about the load"

    # ONE question, not one per refusal. The bare `sholto` is refused
    # against Bartholomew, and `thaddeus sholto` is refused against the
    # bare node that refusal minted — two pairwise events, one question,
    # both sons offered. Asking pairwise would get "yes" to each son and
    # fuse the father into both.
    pending = DisambiguationStore(config.disambiguation_dir).pending()
    assert len(pending) == 1
    question = pending[0]
    assert question.mention == "Major Sholto"
    assert question.book_id == BOOK
    assert question.evidence == "a: he had been dead for years"
    assert sorted(c.name for c in question.candidates) == [
        "Bartholomew Sholto", "Thaddeus Sholto"
    ]
    assert sorted(c.id for c in question.candidates) == sorted([
        entity_id("bartholomew sholto", "Character"),
        entity_id("thaddeus sholto", "Character"),
    ])


@pytest.mark.asyncio
async def test_a_cached_answer_merges_on_the_next_load(tmp_path: Path) -> None:
    from lore.resolve.disambiguation import DisambiguationStore

    config, conn, kuzu, lance = _setup_ambiguous(tmp_path)
    store = DisambiguationStore(config.disambiguation_dir)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never,
                    disambiguation=store)

    # What `lore disambiguate` would have written, without any API call:
    # ONE answer naming ONE of the two sons.
    question = DisambiguationStore(config.disambiguation_dir).pending()[0]
    DisambiguationStore(config.disambiguation_dir).save_answers(
        {question.key: entity_id("bartholomew sholto", "Character")}
    )

    report = await load_book(
        config, conn, kuzu, lance, BOOK, _embed, _never,
        disambiguation=DisambiguationStore(config.disambiguation_dir),
    )
    assert report.entities == 2
    aliases = {
        r[0]: sorted(r[1] or ())
        for r in kuzu.execute("MATCH (e:Entity) RETURN e.name, e.aliases")
    }
    assert aliases["Bartholomew Sholto"] == ["bartholomew sholto", "sholto"]
    # The property the pairwise version broke: the OTHER son is untouched.
    assert aliases["Thaddeus Sholto"] == ["thaddeus sholto"]
    # `Major Sholto` survives only as a surface form pointing at the
    # canonical, which is what every merged alias does in this graph.
    links = [
        tuple(r) for r in kuzu.execute(
            "MATCH (a:Entity)-[:ALIAS_OF]->(c:Entity) RETURN a.name, c.name"
        )
    ]
    assert links == [("Major Sholto", "Bartholomew Sholto")]
    # The merge removed the bare node, so the second question no longer
    # arises at all; the first is answered. Nothing is left to ask.
    remaining = DisambiguationStore(config.disambiguation_dir)
    assert remaining.unanswered(remaining.pending()) == []


@pytest.mark.asyncio
async def test_a_reload_replaces_this_books_pending_questions(
    tmp_path: Path
) -> None:
    from lore.resolve.disambiguation import DisambiguationStore

    config, conn, kuzu, lance = _setup_ambiguous(tmp_path)
    stale = DisambiguationStore(config.disambiguation_dir)
    (config.disambiguation_dir / "pending").mkdir(parents=True, exist_ok=True)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, _never,
                    disambiguation=stale)
    assert len(DisambiguationStore(config.disambiguation_dir).pending()) == 1

    # Answer it, reload: the file is rewritten rather than appended to, and
    # the question is settled — so it never costs quota again.
    question = DisambiguationStore(config.disambiguation_dir).pending()[0]
    DisambiguationStore(config.disambiguation_dir).save_answers(
        {question.key: entity_id("bartholomew sholto", "Character")}
    )
    await load_book(
        config, conn, kuzu, lance, BOOK, _embed, _never,
        disambiguation=DisambiguationStore(config.disambiguation_dir),
    )
    fresh = DisambiguationStore(config.disambiguation_dir)
    assert [q.mention for q in fresh.pending()] == ["Major Sholto"]
    assert fresh.unanswered(fresh.pending()) == []


@pytest.mark.asyncio
async def test_a_corrupt_answer_cache_does_not_break_a_load(tmp_path: Path) -> None:
    from lore.resolve.disambiguation import DisambiguationStore

    config, conn, kuzu, lance = _setup_ambiguous(tmp_path)
    config.disambiguation_dir.mkdir(parents=True, exist_ok=True)
    (config.disambiguation_dir / "answers.json").write_text('{"a": ')
    report = await load_book(
        config, conn, kuzu, lance, BOOK, _embed, _never,
        disambiguation=DisambiguationStore(config.disambiguation_dir),
    )
    assert report.entities == 3


# --------------------------------------------------------------------------
# Task 1k: a definite description must not swallow a name across books
# either. Three of the eleven cross-book entities on the rebuilt graph were
# of this shape — `foreigners in England` had absorbed `England`, which is a
# node real questions are asked about.


@pytest.mark.asyncio
async def test_a_cross_book_description_does_not_swallow_the_place(
    tmp_path: Path,
) -> None:
    """`england` nests inside `foreigners in england`; they are not one place."""
    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "England", "CONTAINS", "Bo",
             ["England", "Bo"], ["Place", "Character"]),
    ])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, "foreigners in England", "OPPOSES", "Zed",
             ["foreigners in England", "Zed"], ["Place", "Character"]),
    ])
    spy = _AdjudicateSpy(True)  # even an approving model must not fuse these
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)

    rows = sorted(
        (r[0], r[1], tuple(sorted(r[2] or ())))
        for r in _rows(
            kuzu,
            "MATCH (e:Entity) WHERE e.name IN ['England', 'foreigners in England']"
            " RETURN e.id, e.name, e.books")
    )
    assert len(rows) == 2, "England must survive as a node of its own"
    assert {name for _id, name, _books in rows} == {
        "England", "foreigners in England",
    }
    assert {books for _id, _name, books in rows} == {(BOOK,), (BOOK_B,)}
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_a_cross_book_possessive_description_does_not_swallow_the_name(
    tmp_path: Path,
) -> None:
    """The possessive is only visible on the RAW surface form.

    `Lucy Ferrier's father` normalizes to `lucy ferrier father`, which nests
    around `lucy ferrier` and looks like an ordinary three-token name.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "Lucy Ferrier", "LOVES", "Bo",
             ["Lucy Ferrier", "Bo"], ["Character", "Character"]),
    ])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, "Lucy Ferrier’s father", "OPPOSES", "Zed",
             ["Lucy Ferrier’s father", "Zed"], ["Character", "Character"]),
    ])
    spy = _AdjudicateSpy(True)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)

    rows = sorted(
        (r[1], tuple(sorted(r[2] or ())))
        for r in _rows(
            kuzu,
            "MATCH (e:Entity) WHERE e.name IN ['Lucy Ferrier',"
            " 'Lucy Ferrier’s father'] RETURN e.id, e.name, e.books")
    )
    assert len(rows) == 2
    assert rows == [("Lucy Ferrier", (BOOK,)),
                    ("Lucy Ferrier’s father", (BOOK_B,))]
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_the_cross_book_guard_leaves_watson_alone(tmp_path: Path) -> None:
    """The guard must not cost the one merge the cross-book rule exists for.

    Same body as `test_a_cross_book_name_variant_merges`, restated here so a
    regression in the Task 1k guard names itself.
    """
    config, conn, kuzu, lance = _env(tmp_path)
    _two_watsons(config, conn)
    spy = _AdjudicateSpy(False)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    watsons = _watsons(kuzu)
    assert len(watsons) == 1
    assert watsons[0][1] == "Dr. John Watson"
    assert watsons[0][3] == ("john watson", "watson")
    conn.close(); kuzu.close()


def test_no_loader_query_is_built_by_interpolation() -> None:
    """Spec 2.2: values bind as parameters, and query TEXT is never assembled.

    An f-string or `.format` over Cypher is where a value can slip into
    structure. Every loader query must be a literal the reader can see whole.
    """
    import ast
    import inspect

    from lore.graph import loader

    tree = ast.parse(inspect.getsource(loader))
    cypher = re.compile(r"\b(MATCH|MERGE|CREATE|WHERE|RETURN|DELETE|SET)\b")
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            literal = "".join(
                part.value for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if cypher.search(literal):
                offenders.append((node.lineno, literal))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
                and isinstance(node.func.value, ast.Constant)
                and cypher.search(str(node.func.value.value))):
            offenders.append((node.lineno, node.func.value.value))
    assert offenders == []
