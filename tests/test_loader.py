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
    assert len(spy.calls) == 1  # the homonym, and only the homonym

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
async def test_two_books_one_captain_approved_merges(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    _two_captains(config, conn)
    spy = _AdjudicateSpy(True)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    assert len(spy.calls) == 1

    captains = _captains(kuzu)
    assert len(captains) == 1
    assert captains[0][1] == tuple(sorted((BOOK, BOOK_B)))  # books unioned
    edges = _rows(kuzu, "MATCH (s:Entity)-[r:REL]->(o:Entity)"
                        " RETURN s.id, r.book_id, o.name")
    assert {e[0] for e in edges} == {captains[0][0]}  # both edges on one node
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_ana_and_the_ana_do_not_fuse_across_books(tmp_path: Path) -> None:
    """The case reproduced in the round-0 report, asserted the right way."""
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
    rows = sorted(
        (r[0], r[1], tuple(sorted(r[2] or ())))
        for r in _rows(kuzu, "MATCH (e:Entity) WHERE e.name IN ['Ana', 'The Ana']"
                             " RETURN e.id, e.name, e.books")
    )
    assert len(rows) == 2
    assert {(name, books) for _id, name, books in rows} == {
        ("Ana", (BOOK,)), ("The Ana", (BOOK_B,)),
    }
    assert rows[0][0] != rows[1][0]  # genuinely distinct ids
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
    assert len(spy.calls) == 1
    snapshot = _snapshot(kuzu, lance)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    assert len(spy.calls) == 1  # the suffixed node is found by its own book
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


async def _load_two_captains_merged(tmp_path: Path, order: list[str]):
    """A's chunk vector is 10.0, B's is 20.0, and the merge is approved."""
    config, conn, kuzu, lance = _env(tmp_path)
    a = [_chunk(BOOK, 1, 0)]
    _add_book(config, conn, BOOK, a, [
        _row(a[0].id, BOOK, "the Captain", "OPPOSES", "Bo",
             ["the Captain", "Bo"], ["Character", "Character"]),
    ], firsts=[10.0])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, "the Captain", "LOVES", "Zed",
             ["the Captain", "Zed"], ["Character", "Character"]),
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
    config, conn, kuzu, lance = await _load_two_captains_merged(tmp_path, order)
    captains = _captains(kuzu)
    assert len(captains) == 1
    assert captains[0][1] == tuple(sorted((BOOK, BOOK_B)))
    vectors = _entity_vectors(config)
    # 10.0 from book A's chunk, 20.0 from book B's — the mean is 15.0, and
    # it must not depend on which book was loaded last.
    assert vectors[captains[0][0]][0] == pytest.approx(15.0)
    conn.close(); kuzu.close()


@pytest.mark.asyncio
async def test_merged_centroid_survives_a_reload_of_either_book(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance = await _load_two_captains_merged(
        tmp_path, [BOOK, BOOK_B]
    )
    captain = _captains(kuzu)[0][0]
    spy = _AdjudicateSpy(True)
    for book in (BOOK, BOOK_B, BOOK, BOOK_B):
        await load_book(config, conn, kuzu, lance, book, _embed, spy)
        assert _entity_vectors(config)[captain][0] == pytest.approx(15.0)
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
        _row(a[0].id, BOOK, "the Captain", "OPPOSES", "Bo",
             ["the Captain", "Bo"], ["Character", "Character"]),
        _row(a[1].id, BOOK, "Ana", "LOVES", "Bo", ["Ana", "Bo"],
             ["Character", "Character"]),
    ]
    _add_book(config, conn, BOOK, a, a_rows, firsts=[10.0, 30.0])
    b = [_chunk(BOOK_B, 1, 0)]
    _add_book(config, conn, BOOK_B, b, [
        _row(b[0].id, BOOK_B, "the Captain", "LOVES", "Zed",
             ["the Captain", "Zed"], ["Character", "Character"]),
    ], firsts=[20.0])
    spy = _AdjudicateSpy(True)
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)
    await load_book(config, conn, kuzu, lance, BOOK_B, _embed, spy)
    captain = _captains(kuzu)[0][0]
    assert _entity_vectors(config)[captain][0] == pytest.approx(15.0)

    # Re-extract book A without the Captain.
    _write_unit(config, BOOK, a_rows[1:], [c.id for c in a], firsts=[10.0, 30.0])
    await load_book(config, conn, kuzu, lance, BOOK, _embed, spy)

    survivors = sorted(
        (r[0], r[1], tuple(sorted(r[2] or ())))
        for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.id, e.name, e.books")
    )
    captains = [s for s in survivors if s[1] == "the Captain"]
    assert len(captains) == 1                    # retained, not deleted
    assert captains[0][2] == (BOOK_B,)           # this book removed from books
    # Recomputed over book B's chunk alone.
    assert _entity_vectors(config)[captain][0] == pytest.approx(20.0)
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
