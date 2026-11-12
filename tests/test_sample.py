"""The sample library: making a one-book template from a library, and copying
it into new and empty libraries.

The source library is test_codex_move's (books ONE and TWO in World A, both
processed with beats and codex; invented names), plus a book THREE in a world
of its own. Real SQLite, Kuzu and LanceDB in tmp dirs; no model is asked.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from pathlib import Path

import lancedb
import pytest

from lore import sample, worlds
from lore.codex import build
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import EMBED_DIM
from lore.db.sqlite import open_db
from lore.graph.loader import check_world_invariant
from lore.orchestrator import jobs
from lore.orchestrator.pipeline import run_book
from tests.test_codex_move import CodexClient, _stages
from tests.test_book_remove import FakeGemini, _build, _cache_beats, ONE, TWO
from tests.test_loader import _add_book, _chunk, _row

THREE = "fedcba9876543210"
CHAR = ["Character", "Character"]


@pytest.fixture(autouse=True)
def _low_threshold(monkeypatch):
    monkeypatch.setattr(build, "MIN_MENTION_CHUNKS", 1)
    monkeypatch.delenv(sample.ENV_SAMPLE, raising=False)


async def _embed(texts):
    return [[0.0] * EMBED_DIM for _ in texts]


async def _aclose():
    pass


def _refresh():
    return sample.cached_codex(embed_factory=lambda cfg: (_embed, _aclose))


@pytest.fixture(scope="module")
def _built(tmp_path_factory) -> Path:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(build, "MIN_MENTION_CHUNKS", 1)
        return _build_source(tmp_path_factory.mktemp("built"))


@pytest.fixture
def source(_built: Path, tmp_path: Path) -> Path:
    """A fresh copy of the built library, its stored files pointing into it."""
    data = tmp_path / "src" / "data"
    shutil.copytree(_built, data)
    conn = sqlite3.connect(data / "lore.db")
    for book, path in conn.execute("SELECT id, file_path FROM books").fetchall():
        conn.execute("UPDATE books SET file_path = ? WHERE id = ?",
                     (str(data / "inbox" / Path(path).name), book))
    conn.commit()
    conn.close()
    return data


def _build_source(tmp_path: Path) -> Path:
    """A three-book library, closed, with a reader's personal state in it."""
    config, conn, kuzu, lance = _build(tmp_path)
    gemini, client = FakeGemini(), CodexClient()
    c3 = [_chunk(THREE, 1, 0, "Ilsa Morrow sailed the grey ship past the northern light alone."),
          _chunk(THREE, 1, 1, "Captain Odo Prane watched Ilsa Morrow from the harbour wall.")]
    _add_book(config, conn, THREE, c3, [
        _row(c3[0].id, THREE, "Ilsa Morrow", "SAILS", "Odo Prane",
             ["Ilsa Morrow", "Odo Prane"], CHAR),
        _row(c3[1].id, THREE, "Odo Prane", "WATCHES", "Ilsa Morrow",
             ["Odo Prane", "Ilsa Morrow"], CHAR),
    ], world="World B")
    _cache_beats(config, conn, THREE, "Ilsa Morrow", "sailed the grey ship past the northern light")
    for book in (ONE, TWO, THREE):
        job = run_book(conn, config, book, _stages(config, kuzu, lance, gemini, client))
        assert job.state == jobs.DONE, job.stopped_reason
    kuzu.close()
    data = config.data_dir
    # Uploaded files, stored under the library's inbox by absolute path.
    for book in (ONE, TWO, THREE):
        stored = data / "inbox" / f"{book}-stored.epub"
        stored.write_bytes(b"epub " + book.encode())
        conn.execute("UPDATE books SET file_path = ? WHERE id = ?", (str(stored), book))
    world_a = worlds.world_of(conn, ONE).id
    conn.execute("INSERT INTO reading_positions (world_id, book_id, chapter) VALUES (?, ?, 1)",
                 (world_a, ONE))
    conn.execute("INSERT INTO gemini_ledger (day, model, count) VALUES ('2026-11-01', 'm', 7)")
    conn.execute("INSERT INTO quota_ledger (run_ref, gpu_hours_est, week)"
                 " VALUES ('r', 1.0, '2026-W44')")
    conn.execute("INSERT INTO stage_timings (stage, seconds, chunks) VALUES ('load', 3, 2)")
    conn.execute("UPDATE book_jobs SET provider = 'gemini'")
    conn.execute("UPDATE books SET status = 'graphed'")  # as the runner's load marks it
    conn.execute("INSERT INTO jobs (kind, book_id, state, detail) VALUES"
                 " ('extract', ?, 'failed', 'x')", (ONE,))
    conn.commit()
    conn.close()
    (data / "narrators").mkdir(exist_ok=True)
    (data / "narrators" / "answers.json").write_text(json.dumps(
        {f"{b}:abc": {"first_person": False, "narrator_name": None} for b in (ONE, TWO)}))
    (data / "speakers").mkdir(exist_ok=True)
    (data / "speakers" / "answers.json").write_text(json.dumps(
        {f"{c}:sha256=1": {"speakers": []} for c in (f"{ONE}:1:0", f"{TWO}:1:0")}))
    pending = data / "disambiguation" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / f"{TWO}.json").write_text("[]")
    return data


def _make(source: Path, tmp_path: Path, keep: str = ONE,
          **kw) -> tuple[Path, Path, sample.TemplateSummary]:
    out = tmp_path / "root" / "sample"
    summary = sample.make_template(source, out, keep, codex_refresh=_refresh(), **kw)
    return source, out, summary


def _tree(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes()
            for p in sorted(path.rglob("*")) if p.is_file()}


def test_make_keeps_one_book_and_nothing_of_the_others(source: Path, tmp_path: Path) -> None:
    before = _tree(source)
    out = tmp_path / "root" / "sample"
    summary = sample.make_template(source, out, ONE, codex_refresh=_refresh())
    assert _tree(source) == before, "the source library is never touched"

    conn = open_db(out / "lore.db")
    assert [r[0] for r in conn.execute("SELECT id FROM books")] == [ONE]
    assert {r[0] for r in conn.execute("SELECT book_id FROM chunks")} == {ONE}
    assert [w.name for w in worlds.list_worlds(conn)] == ["Sherlock Holmes"]
    assert summary.world == "Sherlock Holmes" and summary.book_id == ONE
    for table in ("reading_positions", "gemini_ledger", "quota_ledger", "stage_timings"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE state != 'completed'"
                        " OR book_id != ?", (ONE,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] > 0  # units kept
    job = jobs.get_job(conn, ONE)
    assert job.state == jobs.DONE
    assert conn.execute("SELECT provider FROM book_jobs").fetchone()[0] is None
    assert {r[0] for r in conn.execute("SELECT book_id FROM beats")} == {ONE}
    # the stored file points into the template, not the source library
    assert conn.execute("SELECT file_path FROM books").fetchone()[0] == str(
        out / "inbox" / f"{ONE}-stored.epub")
    kuzu = KuzuStore(out / "kuzu" / "graph", read_only=True)
    try:
        check_world_invariant(conn, kuzu)
        assert [r[0] for r in kuzu.execute("MATCH (b:Book) RETURN b.id")] == [ONE]
        assert {b for r in kuzu.execute("MATCH (e:Entity) RETURN e.books")
                for b in r[0]} == {ONE}
        names = {r[0] for r in kuzu.execute("MATCH (e:Entity) RETURN e.name")}
        assert "Zed" not in names and "Ilsa Morrow" not in names
        assert "Maren Vex" in names
    finally:
        kuzu.close()
    db = lancedb.connect(str(out / "lancedb"))
    assert {r["book_id"] for r in db.open_table("chunks").to_arrow().to_pylist()} == {ONE}
    conn.close()

    # per-book artifacts of the others are gone
    assert sorted(p.name for p in (out / "extractions").iterdir()) == [ONE]
    assert sorted(p.name for p in (out / "beats").iterdir()) == [ONE]
    assert sorted(p.name for p in (out / "inbox").iterdir()) == [f"{ONE}-stored.epub"]
    assert not (out / "disambiguation" / "pending" / f"{TWO}.json").exists()
    assert list(json.loads((out / "narrators" / "answers.json").read_text())) == [f"{ONE}:abc"]
    assert list(json.loads((out / "speakers" / "answers.json").read_text())) == [
        f"{ONE}:1:0:sha256=1"]
    # and nothing of them is left in any file's bytes, nor the source's path
    for path, data in _tree(out).items():
        # (TWO's id is a substring of ONE's twice over: look for its chunk ids)
        for gone in (f"{TWO}:1".encode(), THREE.encode(), b"Ilsa Morrow", b"silver key",
                     b"Zed", str(source).encode()):
            assert gone not in data, (path, gone)
    assert summary.chunks == 2 and summary.entities >= 2 and summary.beats >= 1
    assert summary.size_bytes > 0
    assert sorted(summary.removed) == sorted([f"title-{TWO}", f"title-{THREE}"])


def test_make_rebuilds_the_codex_the_removal_dropped_from_saved_answers(source: Path,
                                                                        tmp_path: Path) -> None:
    _s, out, summary = _make(source, tmp_path)
    conn = open_db(out / "lore.db")
    names = {r[0] for r in conn.execute("SELECT name FROM codex_summaries")}
    # Maren Vex was in both books: her entry was dropped by the removal and
    # comes back from the answer saved when ONE alone was processed.
    assert "Maren Vex" in names
    assert summary.codex_missing == []
    assert summary.codex_entries == len(names)
    statements = {r[0] for r in conn.execute("SELECT id FROM codex_entries")}
    db = lancedb.connect(str(out / "lancedb"))
    assert {r["id"] for r in db.open_table("codex").to_arrow().to_pylist()} == statements
    conn.close()


def test_make_reports_codex_entries_with_no_saved_answer(source: Path, tmp_path: Path) -> None:
    # TWO was processed second: no answer was ever saved for TWO's evidence alone.
    _s, _out, summary = _make(source, tmp_path, keep=TWO)
    assert "Maren Vex" in summary.codex_missing
    assert any("no saved answer" in line for line in summary.lines())


def test_make_refuses_an_existing_out_an_unknown_book_and_an_unfinished_one(source: Path,
                                                                          tmp_path: Path) -> None:
    taken = tmp_path / "taken"
    taken.mkdir()
    with pytest.raises(sample.SampleError, match="already exists"):
        sample.make_template(source, taken, ONE, codex_refresh=_refresh())
    with pytest.raises(sample.SampleError, match="no book"):
        sample.make_template(source, tmp_path / "a", "0000000000000000", codex_refresh=_refresh())
    assert not (tmp_path / "a").exists()
    conn = sqlite3.connect(source / "lore.db")
    conn.execute("UPDATE book_jobs SET state = 'failed' WHERE book_id = ?", (ONE,))
    conn.commit()
    conn.close()
    with pytest.raises(sample.SampleError, match="unfinished"):
        sample.make_template(source, tmp_path / "b", ONE, codex_refresh=_refresh())
    assert not (tmp_path / "b").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")], "no staging left"
    with pytest.raises(sample.SampleError, match="outside"):
        sample.make_template(source, source / "sample", ONE, codex_refresh=_refresh())


# --------------------------------------------------------------------------
# Seeding


@pytest.fixture(scope="module")
def _made(_built: Path, tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("made")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(build, "MIN_MENTION_CHUNKS", 1)
        mp.delenv(sample.ENV_SAMPLE, raising=False)
        sample.make_template(_built, tmp / "root" / "sample", ONE, codex_refresh=_refresh())
    return tmp / "root" / "sample"


@pytest.fixture
def root(_made: Path, tmp_path: Path) -> Path:
    """A data root holding a copy of the made template."""
    shutil.copytree(_made, tmp_path / "data" / "sample")
    return tmp_path / "data"


def test_seed_copies_the_template_into_a_new_library_once(root: Path) -> None:
    dest = root / "users" / "u1"
    assert sample.seed_new_library(root, dest) is True
    conn = open_db(dest / "lore.db")
    assert [r[0] for r in conn.execute("SELECT id FROM books")] == [ONE]
    assert conn.execute("SELECT file_path FROM books").fetchone()[0] == str(
        dest / "inbox" / f"{ONE}-stored.epub")
    conn.close()
    assert sample.seed_new_library(root, dest) is False  # exists: never again
    assert not [p for p in (root / "users").iterdir() if p.name.startswith(".")]
    kuzu = KuzuStore(dest / "kuzu" / "graph", read_only=True)
    try:
        assert [r[0] for r in kuzu.execute("MATCH (b:Book) RETURN b.id")] == [ONE]
    finally:
        kuzu.close()


def test_seed_does_nothing_when_off_or_without_a_template(root: Path, tmp_path: Path,
                                                          monkeypatch) -> None:
    assert sample.seed_new_library(tmp_path / "none", tmp_path / "none" / "users" / "u1") is False
    monkeypatch.setenv(sample.ENV_SAMPLE, "0")
    assert sample.seed_new_library(root, root / "users" / "u2") is False
    assert not (root / "users" / "u2").exists()


def test_two_seeds_racing_leave_one_whole_library(root: Path) -> None:
    dest = root / "users" / "u1"
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(
        sample.seed_new_library(root, dest))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    conn = open_db(dest / "lore.db")
    assert [r[0] for r in conn.execute("SELECT id FROM books")] == [ONE]
    conn.close()
    assert not [p for p in (root / "users").iterdir() if p.name.startswith(".")]


def test_a_failed_copy_leaves_nothing(root: Path, monkeypatch) -> None:

    def boom(src, dest):
        Path(dest).mkdir()
        (Path(dest) / "half").write_text("x")
        raise OSError("disk full")

    monkeypatch.setattr(sample, "_copy_library", boom)
    assert sample.seed_new_library(root, root / "users" / "u1") is False
    assert sorted(p.name for p in (root / "users").iterdir()) == []


def test_tenancy_seeds_only_a_new_directory_and_only_when_asked(root: Path) -> None:
    from lore.tenancy import config_for, user_data_dir
    from lore.config import LoreConfig

    assert not (user_data_dir(root, "u-plain") / "lore.db").exists()  # not asked
    existing = root / "users" / "u-old"
    existing.mkdir(parents=True)
    assert not (user_data_dir(root, "u-old", seed=True) / "lore.db").exists()
    fresh = user_data_dir(root, "u-new", seed=True)
    assert (fresh / "lore.db").is_file()
    base = LoreConfig.load().model_copy(update={"data_dir": root})
    assert config_for(base, "local", seed=True) is base  # local mode: never
    assert (config_for(base, "u-next", seed=True).data_dir / "lore.db").is_file()


# --------------------------------------------------------------------------
# An existing empty library


def _empty_library(root: Path, user: str = "u1") -> Path:
    library = root / "users" / user
    library.mkdir(parents=True)
    conn = open_db(library / "lore.db")
    conn.execute("INSERT INTO gemini_ledger (day, model, count) VALUES ('2026-11-12', 'm', 4)")
    conn.execute("INSERT INTO worlds (name) VALUES ('Mine')")
    conn.commit()
    conn.close()
    KuzuStore(library / "kuzu" / "graph").close()
    return library


def test_add_to_an_empty_library_keeps_the_readers_own_rows(root: Path) -> None:
    library = _empty_library(root)
    assert sample.add_to_empty_library(root, library) == ONE
    conn = open_db(library / "lore.db")
    assert [r[0] for r in conn.execute("SELECT id FROM books")] == [ONE]
    assert conn.execute("SELECT count FROM gemini_ledger").fetchall() == [(4,)]
    assert sorted(w.name for w in worlds.list_worlds(conn)) == ["Mine", "Sherlock Holmes"]
    assert conn.execute("SELECT file_path FROM books").fetchone()[0] == str(
        library / "inbox" / f"{ONE}-stored.epub")
    conn.close()
    kuzu = KuzuStore(library / "kuzu" / "graph", read_only=True)
    try:
        assert [r[0] for r in kuzu.execute("MATCH (b:Book) RETURN b.id")] == [ONE]
    finally:
        kuzu.close()
    assert not [p for p in (root / "users").iterdir()
                if p.name.startswith(".") and not p.name.endswith(".lock")]
    with pytest.raises(sample.LibraryNotEmpty):
        sample.add_to_empty_library(root, library)


def test_add_refuses_while_work_is_under_way_and_without_a_template(root: Path,
                                                                    monkeypatch) -> None:
    library = _empty_library(root)
    (library / "inbox").mkdir(exist_ok=True)
    (library / "inbox" / ".upload-1.part").write_bytes(b"x")
    with pytest.raises(sample.LibraryNotEmpty, match="busy"):
        sample.add_to_empty_library(root, library)
    monkeypatch.setenv(sample.ENV_SAMPLE, "off")
    with pytest.raises(sample.SampleError):
        sample.add_to_empty_library(root, library)


def test_cli_sample_make(source: Path, tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from lore.cli import app

    made = sample.make_template
    monkeypatch.setattr(sample, "make_template", lambda *a, **kw: made(
        *a, codex_refresh=_refresh(), **kw))
    out = tmp_path / "root" / "sample"
    result = CliRunner().invoke(app, ["sample", "make", "--from", str(source),
                                      "--keep", ONE, "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "book: title-abcdef0123456789" in result.output
    assert "world: Sherlock Holmes" in result.output
    assert "size on disk:" in result.output
    again = CliRunner().invoke(app, ["sample", "make", "--from", str(source),
                                     "--keep", ONE, "--out", str(out)])
    assert again.exit_code == 1 and "already exists" in again.output
