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


def _add_book(config, conn, book_id, chunks, triplet_rows, *, embedded=None,
              title=None):
    unit = f"{book_id}-u0000-aaaa"
    ingest_book(conn, book_id=book_id, title=title or f"title-{book_id}",
                author="A", source="upload", file_path=f"/x/{book_id}.epub",
                chunks=chunks)
    d = config.extractions_dir / book_id / unit
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(triplet_rows, schema=_TRIPLET_SCHEMA),
        d / "triplets.parquet",
    )
    ids = [c.id for c in chunks] if embedded is None else embedded
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"chunk_id": cid,
                 "vector": [float(i + 1)] + [0.0] * (EMBED_DIM - 1),
                 "embed_model": "e"}
                for i, cid in enumerate(ids)
            ],
            schema=_EMBED_SCHEMA,
        ),
        d / "embeddings.parquet",
    )
    set_unit_state(conn, book_id, unit, "completed")
    return unit


def _chunk(book_id, chapter, seq, text="passage"):
    return Chunk(id=f"{book_id}:{chapter}:{seq}", book_id=book_id,
                 chapter_seq=chapter, scene_seq=0, seq=seq, text=text)


def _rows(kuzu, cypher):
    return [tuple(r) for r in kuzu.execute(cypher)]


def _snapshot(kuzu, lance) -> dict:
    return {
        "entities": sorted(_rows(kuzu, "MATCH (e:Entity) RETURN e.id, e.name, e.kind")),
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
