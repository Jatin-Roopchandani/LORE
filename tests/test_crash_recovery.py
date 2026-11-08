"""A kill mid-write (WSL under memory pressure, a power cut) must never leave
a book that cannot be resumed.

What a hard kill leaves behind is simulated directly: 0-byte and truncated
part, parquet and manifest files, a unit directory gone while its job row
says completed, and a crash between a temp file's write and its rename.
The next run must redo exactly the broken unit, nothing else, and finish.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lore import fs
from lore.graph import artifacts
from lore.graph.artifacts import (
    DamagedUnitsError,
    MissingArtifactError,
    completed_unit_dirs,
    read_embeddings,
    read_triplets,
    unit_problem,
)
from lore.ingest.models import Chunk
from lore.orchestrator import jobs
from lore.orchestrator.extract_stage import ExtractStage
from lore.orchestrator.local import parts_dir, parts_done
from lore.orchestrator.pipeline import run_book
from lore.orchestrator.run import run_extraction, unit_state
from lore.orchestrator.workunit import pack_units, validate_output
from tests.test_extract_stage import BOOK, Server, _setup

N = 6  # three units of two chunks


def _chunks(n=N):
    return [Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
                  scene_seq=0, seq=i, text=f"text {i}") for i in range(n)]


UNITS = pack_units(BOOK, _chunks(), 2)


def _dest(config, unit) -> Path:
    return config.extractions_dir / BOOK / unit.unit_id


def _processed(tmp_path, name="a"):
    config, conn = _setup(tmp_path, N, name)
    job = run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=Server().factory())])
    assert job.state == "done"
    return config, conn


def _fingerprint(directory: Path) -> dict:
    return {p.relative_to(directory).as_posix(): (p.stat().st_ino, p.stat().st_mtime_ns)
            for p in sorted(directory.rglob("*")) if p.is_file()}


def _damage(path: Path, how: str) -> None:
    if how == "empty":
        path.write_bytes(b"")
    elif how == "truncated":
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
    elif how == "missing":
        path.unlink()
    else:  # pragma: no cover
        raise ValueError(how)


def _rows(config):
    import pyarrow.parquet as pq
    out = {}
    for unit in UNITS:
        d = _dest(config, unit)
        out[unit.unit_id] = (pq.read_table(d / "triplets.parquet").to_pylist(),
                             pq.read_table(d / "embeddings.parquet").to_pylist(),
                             json.loads((d / "manifest.json").read_text())["chunks_ok"])
    return out


def _no_completed_but_broken(config, conn) -> None:
    for unit in UNITS:
        if unit_state(conn, unit.unit_id) == "completed":
            assert unit_problem(_dest(config, unit)) is None, unit.unit_id


# --------------------------------------------------------------------------
# Unit artifacts damaged after the unit was marked completed


@pytest.mark.parametrize("name,how", [
    ("triplets.parquet", "empty"), ("triplets.parquet", "truncated"),
    ("triplets.parquet", "missing"),
    ("embeddings.parquet", "empty"), ("embeddings.parquet", "truncated"),
    ("manifest.json", "empty"), ("manifest.json", "truncated"),
    ("dead_letter.jsonl", "missing"),
])
def test_a_damaged_artifact_redoes_exactly_that_unit(tmp_path, name, how) -> None:
    config, conn = _processed(tmp_path)
    broken, others = UNITS[1], (UNITS[0], UNITS[2])
    before = {u.unit_id: _fingerprint(_dest(config, u)) for u in others}
    _damage(_dest(config, broken) / name, how)
    assert unit_problem(_dest(config, broken)) is not None

    again = Server()
    job = run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=again.factory())])

    assert job.state == "done"
    assert again.answered == []  # its parts were intact: no request repeated
    validate_output(broken, _dest(config, broken))
    assert unit_problem(_dest(config, broken)) is None
    assert {u.unit_id: _fingerprint(_dest(config, u)) for u in others} == before
    assert all(unit_state(conn, u.unit_id) == "completed" for u in UNITS)


def test_a_missing_unit_directory_is_re_extracted(tmp_path) -> None:
    config, conn = _processed(tmp_path)
    import shutil
    shutil.rmtree(_dest(config, UNITS[2]))

    again = Server()
    job = run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=again.factory())])
    assert job.state == "done"
    assert sorted(again.answered) == ["text 4", "text 5"]  # that unit's chunks only
    validate_output(UNITS[2], _dest(config, UNITS[2]))


@pytest.mark.parametrize("how", ["empty", "truncated"])
def test_damaged_parts_are_requested_again_and_nothing_else(tmp_path, how) -> None:
    config, conn = _processed(tmp_path)
    dest = _dest(config, UNITS[1])
    _damage(dest / "embeddings.parquet", "empty")
    _damage(parts_dir(dest) / f"{BOOK}:1:3.json", how)

    again = Server()
    job = run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=again.factory())])
    assert job.state == "done"
    assert again.answered == ["text 3"]
    assert parts_done(dest, UNITS[1]) == 2


def test_the_resume_flag_of_lore_extract_also_heals(tmp_path) -> None:
    config, conn = _processed(tmp_path)
    _damage(_dest(config, UNITS[0]) / "triplets.parquet", "empty")
    again = Server()
    report = run_extraction(config, conn, BOOK, resume=True,
                            gemini_tier_factory=again.factory())
    assert (report.units_completed, report.units_failed) == (1, 0)
    assert again.answered == []
    validate_output(UNITS[0], _dest(config, UNITS[0]))


# --------------------------------------------------------------------------
# Parts damaged while the unit is still in progress


@pytest.mark.parametrize("how", ["empty", "truncated"])
def test_a_damaged_part_mid_unit_never_crashes_the_resume(tmp_path, how) -> None:
    config, conn = _setup(tmp_path, N, "a")
    first = Server(limit=3)  # unit 0 whole, one chunk of unit 1
    job = run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=first.factory())])
    assert job.state == "waiting_quota"
    dest = _dest(config, UNITS[1])
    _damage(parts_dir(dest) / f"{BOOK}:1:2.json", how)
    assert parts_done(dest, UNITS[1]) == 0

    again = Server()
    job = run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=again.factory())],
                   force=True)
    assert job.state == "done"
    assert sorted(again.answered) == ["text 2", "text 3", "text 4", "text 5"]


# --------------------------------------------------------------------------
# A kill between a temp file's write and its rename


@pytest.mark.parametrize("crash_at", [1, 2, 3, 6, 7, 9, 12, 15])
def test_a_crash_between_write_and_rename_resumes_cleanly(
    tmp_path, monkeypatch, crash_at
) -> None:
    config, conn = _setup(tmp_path, N, "a")
    real_commit = fs._commit
    calls = {"n": 0}

    def commit(temporary, path):
        calls["n"] += 1
        if calls["n"] == crash_at:
            raise SystemExit("killed")  # not an Exception: nothing catches it
        real_commit(temporary, path)

    monkeypatch.setattr(fs, "_commit", commit)
    with pytest.raises(SystemExit):
        run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=Server().factory())])
    monkeypatch.setattr(fs, "_commit", real_commit)

    _no_completed_but_broken(config, conn)
    # The temp the kill left behind is never read as a part or an artifact.
    leftovers = [p for p in config.extractions_dir.rglob("*.tmp")]
    assert all(fs.is_temp(p) for p in leftovers)

    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    fs._cleaned.clear()
    job = run_book(conn, config, BOOK,
                   [ExtractStage(gemini_tier_factory=Server().factory())], now=later)
    assert job.state == "done"
    _no_completed_but_broken(config, conn)

    config2, _ = _processed(tmp_path, "clean")
    assert _rows(config) == _rows(config2)


def test_a_leftover_temp_is_cleaned_by_the_next_write(tmp_path, monkeypatch) -> None:
    config, conn = _setup(tmp_path, N, "a")
    folder = parts_dir(_dest(config, UNITS[0]))
    folder.mkdir(parents=True)
    stale = folder / f"{BOOK}:1:0.json.999999999.0badcafe.tmp"
    stale.write_text('{"extraction": nu')
    fs._cleaned.clear()
    job = run_book(conn, config, BOOK, [ExtractStage(gemini_tier_factory=Server().factory())])
    assert job.state == "done"
    assert not stale.exists()


# --------------------------------------------------------------------------
# The loader's side: a completed row whose files disagree is demoted


@pytest.mark.parametrize("damage", ["dir", "empty", "truncated"])
def test_the_loader_demotes_a_broken_completed_unit(tmp_path, damage) -> None:
    config, conn = _processed(tmp_path)
    dest = _dest(config, UNITS[1])
    if damage == "dir":
        import shutil
        shutil.rmtree(dest)
    else:
        _damage(dest / "embeddings.parquet", damage)

    with pytest.raises(DamagedUnitsError) as caught:
        completed_unit_dirs(conn, config, BOOK)
    assert isinstance(caught.value, MissingArtifactError)
    assert caught.value.unit_ids == [UNITS[1].unit_id]
    assert unit_state(conn, UNITS[1].unit_id) == "failed"
    assert unit_state(conn, UNITS[0].unit_id) == "completed"
    # Demoted, it no longer blocks the read; the next extraction redoes it.
    assert len(completed_unit_dirs(conn, config, BOOK)) == 2


def test_an_unreadable_parquet_in_the_read_is_demoted_too(tmp_path, monkeypatch) -> None:
    config, conn = _processed(tmp_path)
    dirs = completed_unit_dirs(conn, config, BOOK)
    real = artifacts.pq.read_table

    def flaky(path, *a, **k):
        if UNITS[0].unit_id in str(path):
            raise OSError("Parquet file size is 0 bytes")
        return real(path, *a, **k)

    monkeypatch.setattr(artifacts.pq, "read_table", flaky)
    with pytest.raises(DamagedUnitsError) as caught:
        read_triplets(dirs, conn=conn)
    assert caught.value.unit_ids == [UNITS[0].unit_id]
    assert unit_state(conn, UNITS[0].unit_id) == "failed"


class _Load:
    """The load stage's reads, without Kuzu or LanceDB."""

    name = "load"

    def __init__(self):
        self.loaded: list[int] = []

    def total(self, ctx):
        return 0

    def run(self, ctx, job_ctl):
        dirs = completed_unit_dirs(ctx.conn, ctx.config, ctx.book_id)
        triplets = read_triplets(dirs, conn=ctx.conn)
        embeddings = read_embeddings(dirs, conn=ctx.conn)
        assert len(dirs) == len(UNITS)
        self.loaded.append(len(embeddings))
        assert triplets


def test_end_to_end_a_truncated_unit_mid_load_is_re_extracted_and_the_book_finishes(
    tmp_path,
) -> None:
    """The morning of 2026-11-07: killed mid-processing, a unit's parquet
    and parts came back empty and its job row still said completed."""
    config, conn = _setup(tmp_path, N, "a")
    load = _Load()
    job = run_book(conn, config, BOOK,
                   [ExtractStage(gemini_tier_factory=Server().factory()), load])
    assert job.state == "done" and load.loaded == [N]

    dest = _dest(config, UNITS[1])
    _damage(dest / "triplets.parquet", "empty")
    _damage(dest / "embeddings.parquet", "truncated")
    for part in parts_dir(dest).glob("*.json"):
        _damage(part, "empty")
    # Interrupted during the load: the next run resumes AT the load.
    jobs.finish_stage(conn, BOOK, "load")

    again = Server()
    job = run_book(conn, config, BOOK,
                   [ExtractStage(gemini_tier_factory=again.factory()), load])
    assert job.state == "done", job.stopped_reason
    assert sorted(again.answered) == ["text 2", "text 3"]
    assert load.loaded == [N, N]
    _no_completed_but_broken(config, conn)


def test_without_an_extract_stage_the_job_fails_and_continue_re_extracts(tmp_path) -> None:
    config, conn = _processed(tmp_path)
    _damage(_dest(config, UNITS[0]) / "triplets.parquet", "empty")
    jobs.finish_stage(conn, BOOK, "load")

    job = run_book(conn, config, BOOK, [_Load()])
    assert job.state == "failed"
    assert job.stage == "extract"
    assert "re-extract" in job.stopped_reason

    again = Server()
    job = run_book(conn, config, BOOK,
                   [ExtractStage(gemini_tier_factory=again.factory()), _Load()])
    assert job.state == "done"
    assert again.answered == []


def test_unit_problem_accepts_a_healthy_unit_and_an_empty_dead_letter_file(tmp_path) -> None:
    config, conn = _processed(tmp_path)
    dest = _dest(config, UNITS[0])
    assert (dest / "dead_letter.jsonl").stat().st_size == 0
    assert unit_problem(dest) is None
    assert os.listdir(dest)


# --------------------------------------------------------------------------
# JSON stores: a damaged file is "not cached" for that file only, and the
# next save repairs it


DAMAGED = [b"", b'{"answers": {"k": tr', b"\x00\x00\x00\x00", b"null"]


@pytest.mark.parametrize("content", DAMAGED)
def test_damaged_beats_and_codex_caches_read_as_not_cached(tmp_path, content) -> None:
    from lore.beats import build as beats
    from lore.codex import build as codex

    chunks = _chunks(2)
    good = tmp_path / "beats" / "1-0.json"
    beats.write_cached(good, chunks, beats.BeatsAnswer(beats=[]))
    bad = tmp_path / "beats" / "2-0.json"
    bad.write_bytes(content)
    assert beats.read_cached(bad, chunks) is None
    assert beats.read_cached(good, chunks) == beats.BeatsAnswer(beats=[])
    beats.write_cached(bad, chunks, beats.BeatsAnswer(beats=[]))
    assert beats.read_cached(bad, chunks) == beats.BeatsAnswer(beats=[])

    answer = codex.CodexAnswer(summary="s", statements=[])
    path = tmp_path / "codex" / "e-h.json"
    path.parent.mkdir()
    path.write_bytes(content)
    path.with_suffix(".attempts").write_bytes(content)
    assert codex.read_cached(path, "h") is None
    assert codex.read_attempts(path, "h") == 0
    codex.write_cached(path, "h", answer)
    codex.write_attempts(path, "h", 1)
    assert codex.read_cached(path, "h") == answer
    assert codex.read_attempts(path, "h") == 1


@pytest.mark.parametrize("content", DAMAGED)
def test_damaged_answer_files_read_as_not_cached(tmp_path, content) -> None:
    from lore.graph.adjudicate import AdjudicationCache
    from lore.resolve.disambiguation import DisambiguationStore
    from lore.resolve.narrator import NarratorAnswer, NarratorCache, SpeakerAnswers, SpeakerCache

    store = DisambiguationStore(tmp_path / "disambiguation")
    store.answers_path.parent.mkdir(parents=True)
    store.answers_path.write_bytes(content)
    store.synonym_answers_path.write_bytes(content)
    store.pending_dir.mkdir()
    (store.pending_dir / "x.json").write_bytes(content)
    assert store.answers() == {} and store.synonym_answers() == {}
    assert store.pending() == []
    store.save_answers({"q": "e1"})
    assert DisambiguationStore(store.directory).answers() == {"q": "e1"}

    adjudication = AdjudicationCache(tmp_path / "adjudication")
    adjudication.directory.mkdir()
    adjudication.path.write_bytes(content)
    assert adjudication.get("k") is None
    adjudication.put("k", True, "m")
    assert AdjudicationCache(adjudication.directory).get("k") is True

    for cache, value in ((NarratorCache(tmp_path / "narrator"),
                          NarratorAnswer(first_person=True, narrator_name="Watson")),
                         (SpeakerCache(tmp_path / "speakers"), SpeakerAnswers(speakers=[]))):
        cache.directory.mkdir()
        cache.path.write_bytes(content)
        assert cache.get("k") is None
        cache.put("k", value, "m")
        assert cache.get("k") == value


def test_every_json_store_writes_through_lore_fs(tmp_path, monkeypatch) -> None:
    """Durable (fsynced) writes: each store goes through lore.fs."""
    from lore.beats import build as beats
    from lore.codex import build as codex
    from lore.graph.adjudicate import AdjudicationCache
    from lore.resolve.disambiguation import DisambiguationStore
    from lore.resolve.narrator import NarratorAnswer, NarratorCache

    written: list[str] = []
    real = fs.atomic_write_bytes
    monkeypatch.setattr(fs, "atomic_write_bytes",
                        lambda path, data: (written.append(Path(path).name), real(path, data))[1])
    beats.write_cached(tmp_path / "b.json", _chunks(1), beats.BeatsAnswer(beats=[]))
    codex.write_cached(tmp_path / "c.json", "h", codex.CodexAnswer(summary="s", statements=[]))
    codex.write_attempts(tmp_path / "c.json", "h", 1)
    DisambiguationStore(tmp_path / "d").save_answers({"q": "e"})
    AdjudicationCache(tmp_path / "a").put("k", False, None)
    NarratorCache(tmp_path / "n").put("k", NarratorAnswer(first_person=False), None)
    assert written == ["b.json", "c.json", "c.attempts", "answers.json",
                       "answers.json", "answers.json"]
