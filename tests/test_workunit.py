import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lore.ingest.models import Chunk
from lore.orchestrator.workunit import (
    OutputError,
    pack_units,
    validate_output,
    write_unit,
)

BOOK = "abcdef0123456789"


def _chunks(n: int) -> list[Chunk]:
    return [
        Chunk(
            id=f"{BOOK}:1:{i}",
            book_id=BOOK,
            chapter_seq=1,
            scene_seq=0,
            seq=i,
            text=f"chunk text {i}",
        )
        for i in range(n)
    ]


def test_pack_units_splits_and_labels() -> None:
    units = pack_units(BOOK, _chunks(5), size=2)
    # Unit ids now carry a content fingerprint (FIX 5): {book_id}-u{index}-{hash8}.
    for index, unit in enumerate(units):
        assert unit.unit_id.startswith(f"{BOOK}-u{index:04d}-")
        suffix = unit.unit_id.removeprefix(f"{BOOK}-u{index:04d}-")
        assert len(suffix) == 8
        int(suffix, 16)  # hex fingerprint
    assert [len(u.chunks) for u in units] == [2, 2, 1]
    assert units[0].chunks[0].id == f"{BOOK}:1:0"


def test_pack_units_is_deterministic() -> None:
    assert pack_units(BOOK, _chunks(7), size=3) == pack_units(BOOK, _chunks(7), size=3)


def test_pack_units_ids_change_when_the_chunk_set_at_an_index_changes() -> None:
    # The whole point of content-addressing (FIX 5): changing work_unit_size
    # reshuffles which chunks land in unit index 0, so a stale `completed`
    # row for the old u0000 must not silently vouch for the new chunk set.
    units_a = pack_units(BOOK, _chunks(10), size=5)
    units_b = pack_units(BOOK, _chunks(10), size=3)
    assert units_a[0].unit_id != units_b[0].unit_id


def test_pack_units_ids_are_unique_per_unit() -> None:
    units = pack_units(BOOK, _chunks(9), size=3)
    assert len({u.unit_id for u in units}) == len(units)


def test_write_unit_emits_jsonl_and_manifest(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    unit_dir = write_unit(unit, tmp_path)
    rows = [
        json.loads(line)
        for line in (unit_dir / "unit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert [r["chunk_id"] for r in rows] == [c.id for c in unit.chunks]
    assert rows[0]["text"] == "chunk text 0"
    manifest = json.loads((unit_dir / "unit-manifest.json").read_text())
    assert manifest["unit_id"] == unit.unit_id
    assert manifest["book_id"] == BOOK
    assert manifest["chunk_count"] == 3


def test_write_unit_ships_the_lore_package(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(1), size=10)[0]
    unit_dir = write_unit(unit, tmp_path, package_root=Path("lore"))
    assert (unit_dir / "lore" / "extract" / "runner.py").is_file()
    assert (unit_dir / "lore" / "extract" / "prompts.py").is_file()


def _write_outputs(
    out_dir: Path,
    chunk_ids: list[str],
    *,
    chunks_in: int,
    chunks_ok: int,
    chunks_dead: int = 0,
    embedding_chunk_ids: list[str] | None = None,
    vector_dim: int = 768,
    write_dead_letter: bool = True,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"chunk_id": chunk_ids, "subject": ["s"] * len(chunk_ids)}),
        out_dir / "triplets.parquet",
    )
    embed_ids = chunk_ids if embedding_chunk_ids is None else embedding_chunk_ids
    pq.write_table(
        pa.table(
            {"chunk_id": embed_ids, "vector": [[0.0] * vector_dim] * len(embed_ids)}
        ),
        out_dir / "embeddings.parquet",
    )
    if write_dead_letter:
        (out_dir / "dead_letter.jsonl").write_text("")
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "unit_id": f"{BOOK}-u0000-deadbeef",
                "chunks_in": chunks_in,
                "chunks_ok": chunks_ok,
                "chunks_dead": chunks_dead,
                "gpu_seconds": 120.0,
                "model": "Qwen/Qwen2.5-7B-Instruct-AWQ",
            }
        )
    )


def test_validate_output_accepts_a_complete_unit(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    ids = [c.id for c in unit.chunks]
    _write_outputs(tmp_path, ids, chunks_in=3, chunks_ok=3)
    summary = validate_output(unit, tmp_path)
    assert summary.chunks_ok == 3
    assert summary.gpu_seconds == 120.0
    assert summary.model == "Qwen/Qwen2.5-7B-Instruct-AWQ"


def test_validate_output_rejects_missing_artifacts(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    with pytest.raises(OutputError, match="missing"):
        validate_output(unit, tmp_path)


def test_validate_output_rejects_unaccounted_chunks(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    ids = [c.id for c in unit.chunks]
    _write_outputs(tmp_path, ids, chunks_in=3, chunks_ok=2)  # 2 + 0 != 3
    with pytest.raises(OutputError, match="accounted"):
        validate_output(unit, tmp_path)


def test_validate_output_rejects_foreign_chunk_ids(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    ids = [c.id for c in unit.chunks]
    # All real ids present (so this isolates the foreign-id check from the
    # missing-id check) plus one id that belongs to a different unit.
    _write_outputs(tmp_path, ids + ["someone-elses:1:0"], chunks_in=3, chunks_ok=3)
    with pytest.raises(OutputError, match="foreign"):
        validate_output(unit, tmp_path)


def test_validate_output_rejects_missing_embedding_rows(tmp_path: Path) -> None:
    # Both artifacts only contain the first chunk id, but the manifest
    # self-reports a self-consistent (and wrong) 3/3/0. Without an
    # expected-minus-actual check on embeddings this silently loses work.
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    ids = [c.id for c in unit.chunks]
    _write_outputs(tmp_path, [ids[0]], chunks_in=3, chunks_ok=3)
    with pytest.raises(OutputError, match="missing"):
        validate_output(unit, tmp_path)


def test_validate_output_accepts_a_chunk_with_no_triplets(tmp_path: Path) -> None:
    # A chunk of pure scene-setting prose can legitimately yield zero
    # triplets. embeddings.parquet must still cover every chunk, but
    # triplets.parquet is allowed to be short.
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    ids = [c.id for c in unit.chunks]
    _write_outputs(
        tmp_path,
        ids[:2],
        chunks_in=3,
        chunks_ok=3,
        embedding_chunk_ids=ids,
    )
    summary = validate_output(unit, tmp_path)
    assert summary.chunks_ok == 3


def test_validate_output_rejects_a_unit_missing_its_dead_letter_file(
    tmp_path: Path,
) -> None:
    # A unit claiming chunks_dead > 0 without its dead-letter file cannot
    # be trusted: the dead-lettered chunk's raw_output/error is simply gone.
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    ids = [c.id for c in unit.chunks]
    _write_outputs(
        tmp_path, ids, chunks_in=3, chunks_ok=2, chunks_dead=1,
        write_dead_letter=False,
    )
    with pytest.raises(OutputError, match="missing"):
        validate_output(unit, tmp_path)


def test_validate_output_rejects_a_wrong_dimension_vector(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    ids = [c.id for c in unit.chunks]
    _write_outputs(tmp_path, ids, chunks_in=3, chunks_ok=3, vector_dim=384)
    with pytest.raises(OutputError, match="384"):
        validate_output(unit, tmp_path)
