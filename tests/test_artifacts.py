from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from lore.config import LoreConfig
from lore.db.lance_store import EMBED_DIM
from lore.db.sqlite import open_db
from lore.graph.artifacts import completed_unit_dirs, read_embeddings, read_triplets
from lore.ingest.registry import ingest_book
from lore.orchestrator.run import set_unit_state

BOOK = "abcdef0123456789"


def _config(tmp_path: Path) -> LoreConfig:
    return LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})


def _seed_book(conn) -> None:
    # jobs.book_id is a foreign key into books(id); a job row can't be
    # inserted until the parent book exists.
    ingest_book(
        conn, book_id=BOOK, title="T", author=None,
        source="upload", file_path="/x/b.epub", chunks=[],
    )


def _unit(config: LoreConfig, unit_id: str, chunk_id: str) -> Path:
    d = config.extractions_dir / BOOK / unit_id
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"chunk_id": [chunk_id], "subject": ["A"], "predicate": ["OPPOSES"]}),
        d / "triplets.parquet",
    )
    pq.write_table(
        pa.table({"chunk_id": [chunk_id], "vector": [[0.0] * EMBED_DIM]}),
        d / "embeddings.parquet",
    )
    return d


def test_only_completed_units_are_returned(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed_book(conn)
    _unit(config, f"{BOOK}-u0000-aaaa", "c0")
    _unit(config, f"{BOOK}-u0001-bbbb", "c1")
    set_unit_state(conn, BOOK, f"{BOOK}-u0000-aaaa", "completed")
    set_unit_state(conn, BOOK, f"{BOOK}-u0001-bbbb", "failed")

    dirs = completed_unit_dirs(conn, config, BOOK)
    assert [d.name for d in dirs] == [f"{BOOK}-u0000-aaaa"]
    conn.close()


def test_artifacts_present_without_a_completed_job_are_ignored(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _unit(config, f"{BOOK}-u0000-aaaa", "c0")  # files exist, no job row at all
    assert completed_unit_dirs(conn, config, BOOK) == []
    conn.close()


def test_reads_rows_across_units(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed_book(conn)
    for i, chunk in enumerate(["c0", "c1"]):
        unit_id = f"{BOOK}-u{i:04d}-xxxx"
        _unit(config, unit_id, chunk)
        set_unit_state(conn, BOOK, unit_id, "completed")
    dirs = completed_unit_dirs(conn, config, BOOK)
    assert {r["chunk_id"] for r in read_triplets(dirs)} == {"c0", "c1"}
    assert len(read_embeddings(dirs)) == 2
    conn.close()
