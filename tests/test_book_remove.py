"""Taking a book out of the graph, and moving a book between worlds.

Real SQLite, Kuzu and LanceDB in a tmp dir, with synthetic books (invented
names). Gemini is a fake that records every request: a remove, a move and a
move back must record none, because everything is answered from the saved
extraction, the adjudication caches and the beats cache.

World "World A" holds books ONE and TWO. Both name Maren Vex (one entity once
both are loaded) and Dr. John Watson; book TWO also writes plain "Watson" next
to him (an alias node there) and meets Zed, who is in no other book.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lore import worlds
from lore.beats import build as beats_build
from lore.db.lance_store import EMBED_DIM
from lore.graph.loader import WorldInvariantError, check_world_invariant, load_book
from lore.graph.remove import remove_book_from_graph
from lore.ingest.registry import chunks_for_book
from lore.orchestrator import jobs
from lore.orchestrator.move import prepare_move
from lore.orchestrator.pipeline import JobRefused, run_book
from lore.orchestrator.stages import (
    BeatsStage,
    LoadStage,
    ReassignStage,
    RemoveStage,
)
from tests.test_loader import (
    BOOK,
    BOOK_B,
    _add_book,
    _chunk,
    _env,
    _KuzuFailingOn,
    _row,
)

ONE, TWO = BOOK, BOOK_B
CHAR = ["Character", "Character"]


class FakeGemini:
    """Every request any step would send to Gemini lands in `requests`.
    Embeddings are local (Ollama), so they are not requests."""

    def __init__(self) -> None:
        self.requests: list[tuple] = []

    async def embed(self, texts):
        return [[0.0] * EMBED_DIM for _ in texts]

    async def adjudicate(self, canonical, mention) -> bool:
        self.requests.append(("adjudicate", canonical, mention))
        return False

    def beats_client(self, config):
        gemini = self

        class _Client:
            async def generate_json(self, prompt, schema, **kwargs):
                gemini.requests.append(("beats", prompt[:40]))
                raise AssertionError("a beats request was sent")

            async def aclose(self) -> None:
                pass

        return _Client()


class _Graph:
    """The test's open Kuzu, as a stage factory returns it: close is a no-op
    (Kuzu allows one writable handle, and the test keeps it)."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def execute(self, cypher, params=None):
        return self._inner.execute(cypher, params)

    def close(self) -> None:
        pass


def _build(tmp_path: Path):
    """Both books in World A, loaded in order ONE, TWO, with their beats."""
    config, conn, kuzu, lance = _env(tmp_path)
    c1 = [_chunk(ONE, 1, 0, "Maren Vex stood at the old gate and refused to let anyone pass through it."),
          _chunk(ONE, 1, 1, "Dr. John Watson wrote down every word she said in his small black notebook.")]
    _add_book(config, conn, ONE, c1, [
        _row(c1[0].id, ONE, "Maren Vex", "OPPOSES", "Bo", ["Maren Vex", "Bo"], CHAR),
        _row(c1[1].id, ONE, "Dr. John Watson", "LOVES", "Bo",
             ["Dr. John Watson", "Bo"], CHAR),
    ], world="World A")
    c2 = [_chunk(TWO, 1, 0, "Maren Vex gave the silver key to Zed without saying a single word."),
          _chunk(TWO, 1, 1, "Watson followed them down the long cold corridor to the cellar door.")]
    _add_book(config, conn, TWO, c2, [
        _row(c2[0].id, TWO, "Maren Vex", "LOVES", "Zed", ["Maren Vex", "Zed"], CHAR),
        _row(c2[1].id, TWO, "Dr. John Watson", "FOLLOWS", "Zed",
             ["Dr. John Watson", "Zed"], CHAR),
        _row(c2[1].id, TWO, "Watson", "TRUSTS", "Maren Vex",
             ["Watson", "Maren Vex"], CHAR),
    ], world="World A")
    _cache_beats(config, conn, ONE, "Maren Vex", "refused to let anyone pass through it")
    _cache_beats(config, conn, TWO, "Maren Vex", "gave the silver key to Zed")
    return config, conn, kuzu, lance


def _cache_beats(config, conn, book_id, subject, quote) -> None:
    """The saved Gemini answer for the book's one chapter part."""
    chunks = chunks_for_book(conn, book_id)
    beats_build.write_cached(
        beats_build.cache_path(config.beats_dir, book_id, 1, 0), chunks,
        beats_build.BeatsAnswer(beats=[beats_build.BeatItem(
            entities=[subject], kind="decision", text=f"{subject} decides.",
            quote=quote)]))


def _stages(config, kuzu, lance, gemini, world_id=None, *, load=None, remove=None):
    """The move stages (or, without a world, just load and beats) on the
    test's open stores and the fake Gemini."""

    def _load(cfg, conn, book_id):
        return asyncio.run(load_book(cfg, conn, kuzu, lance, book_id,
                                     gemini.embed, gemini.adjudicate))

    async def _aclose() -> None:
        pass

    stages = [
        LoadStage(load or _load),
        BeatsStage(client_factory=gemini.beats_client,
                   embed_factory=lambda cfg: (gemini.embed, _aclose),
                   kuzu_factory=lambda cfg: _Graph(kuzu),
                   lance_factory=lambda cfg: lance),
    ]
    if world_id is not None:
        stages = [
            RemoveStage(remove or (lambda cfg, conn, book_id: remove_book_from_graph(
                conn, kuzu, lance, book_id))),
            ReassignStage(world_id),
        ] + stages
    return stages


def _process(config, conn, kuzu, lance, gemini, book_id):
    job = run_book(conn, config, book_id, _stages(config, kuzu, lance, gemini))
    assert job.state == jobs.DONE, job.stopped_reason


def _move(config, conn, kuzu, lance, gemini, book_id, world_id, **kw):
    assert prepare_move(conn, book_id, world_id)
    return run_book(conn, config, book_id,
                    _stages(config, kuzu, lance, gemini, world_id, **kw))


def _library(tmp_path: Path):
    config, conn, kuzu, lance = _build(tmp_path)
    gemini = FakeGemini()
    for book in (ONE, TWO):
        _process(config, conn, kuzu, lance, gemini, book)
    return config, conn, kuzu, lance, gemini


def _rows(kuzu, cypher, params=None):
    return [tuple(r) for r in kuzu.execute(cypher, params)]


def _snapshot(conn, kuzu, lance, config, books=None) -> dict:
    """Everything a move must restore. With `books`, only what belongs to
    those books alone: a world's view of the combined graph."""
    import lancedb

    keep_books = set(books) if books else None

    def of_books(book_ids) -> bool:
        return keep_books is None or set(book_ids) <= keep_books

    def of_chunk(chunk_id: str) -> bool:
        return keep_books is None or chunk_id.split(":")[0] in keep_books

    entities = sorted(
        (r[0], r[1], r[2], tuple(r[3] or ()), tuple(r[4] or ()))
        for r in _rows(kuzu, "MATCH (e:Entity) RETURN e.id, e.name, e.kind,"
                             " e.aliases, e.books")
        if of_books(r[4] or ()))
    ids = {e[0] for e in entities}
    db = lancedb.connect(str(config.lancedb_dir))
    beats = sorted(row for row in conn.execute(
        "SELECT id, book_id, chapter_seq, chunk_id, kind, text, quote FROM beats")
        if of_books([row[1]]))
    beat_ids = {row[0] for row in beats}
    return {
        "entities": entities,
        "rel": sorted(r for r in _rows(
            kuzu, "MATCH (a:Entity)-[r:REL]->(b:Entity) RETURN a.id, r.predicate,"
                  " b.id, r.chunk_id, r.book_id, r.chapter_seq, r.evidence")
            if of_books([r[4]])),
        "mentions": sorted(r for r in _rows(
            kuzu, "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) RETURN c.id, e.id")
            if of_chunk(r[0])),
        "alias_of": sorted(r for r in _rows(
            kuzu, "MATCH (a:Entity)-[:ALIAS_OF]->(b:Entity) RETURN a.id, b.id")
            if r[0] in ids and r[1] in ids),
        "books": sorted(r for r in _rows(kuzu, "MATCH (b:Book) RETURN b.id, b.title")
                        if of_books([r[0]])),
        "chunk_nodes": sorted(r for r in _rows(
            kuzu, "MATCH (c:Chunk) RETURN c.id, c.book_id") if of_books([r[1]])),
        "lance_chunks": sorted(
            (r["chunk_id"], tuple(r["entity_ids"]), round(float(r["vector"][0]), 6))
            for r in db.open_table("chunks").to_arrow().to_pylist()
            if of_chunk(r["chunk_id"])),
        "lance_entities": sorted(
            (r["entity_id"], round(float(r["vector"][0]), 6))
            for r in db.open_table("entities").to_arrow().to_pylist()
            if r["entity_id"] in ids),
        "lance_beats": sorted(r["id"] for r in db.open_table("beats")
                              .to_arrow().to_pylist() if r["id"] in beat_ids),
        "beats": beats,
        "beat_entities": sorted(r for r in conn.execute(
            "SELECT beat_id, position, name, entity_id FROM beat_entities")
            if r[0] in beat_ids),
        "sqlite_chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
    }


def _named(kuzu, name):
    return sorted(
        (r[0], tuple(r[1])) for r in _rows(
            kuzu, "MATCH (e:Entity) WHERE e.name = $n RETURN e.id, e.books",
            {"n": name}))


# --------------------------------------------------------------------------
# remove_book_from_graph
# --------------------------------------------------------------------------


def test_remove_equals_a_graph_built_without_the_book(tmp_path: Path) -> None:
    config, conn, kuzu, lance, _gemini = _library(tmp_path / "both")
    report = remove_book_from_graph(conn, kuzu, lance, TWO)

    solo_config, solo_conn, solo_kuzu, solo_lance, _ = _library_one(tmp_path / "solo")
    assert (_snapshot(conn, kuzu, lance, config, books=[ONE])
            == _snapshot(solo_conn, solo_kuzu, solo_lance, solo_config, books=[ONE]))
    assert report.chunks == 2 and report.beats == 1 and report.vector_chunks == 2
    assert report.entities_deleted >= 2  # Zed, and the Watson alias node
    assert report.shared_with == (ONE,)
    # The kept shared entities no longer list the book, and the alias form
    # only book TWO's alias node carried is gone from the canonical entity.
    assert all(TWO not in books for _id, books in _named(kuzu, "Maren Vex"))
    assert _rows(kuzu, "MATCH (e:Entity) WHERE list_contains(e.books, $b)"
                       " RETURN e.id", {"b": TWO}) == []
    kuzu.close()
    solo_kuzu.close()


def _library_one(tmp_path: Path):
    """A graph built from book ONE alone (the reference for a removal)."""
    config, conn, kuzu, lance = _build(tmp_path)
    gemini = FakeGemini()
    _process(config, conn, kuzu, lance, gemini, ONE)
    return config, conn, kuzu, lance, gemini


def test_remove_keeps_the_registry_and_every_cache(tmp_path: Path) -> None:
    config, conn, kuzu, lance, _gemini = _library(tmp_path)
    chunks = conn.execute("SELECT COUNT(*) FROM chunks WHERE book_id = ?",
                          (TWO,)).fetchone()[0]
    remove_book_from_graph(conn, kuzu, lance, TWO)
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE book_id = ?",
                        (TWO,)).fetchone()[0] == chunks == 2
    assert (config.beats_dir / TWO / "1-0.json").exists()
    assert (config.extractions_dir / TWO).is_dir()
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE state = 'completed'"
                        " AND book_id = ?", (TWO,)).fetchone()[0] >= 1
    assert conn.execute("SELECT status FROM books WHERE id = ?",
                        (TWO,)).fetchone()[0] == "chunked"
    kuzu.close()


def test_remove_is_idempotent(tmp_path: Path) -> None:
    config, conn, kuzu, lance, _gemini = _library(tmp_path)
    remove_book_from_graph(conn, kuzu, lance, TWO)
    once = _snapshot(conn, kuzu, lance, config)
    again = remove_book_from_graph(conn, kuzu, lance, TWO)
    assert _snapshot(conn, kuzu, lance, config) == once
    assert (again.chunks, again.rel_edges, again.entities_deleted,
            again.entities_released, again.beats, again.vector_chunks) == (0,) * 6
    # A book that was never loaded is a no-op too.
    never = _chunk("ffffffffffffffff", 1, 0)
    _add_book(config, conn, "ffffffffffffffff", [never], [], world="World A")
    once = _snapshot(conn, kuzu, lance, config)
    remove_book_from_graph(conn, kuzu, lance, "ffffffffffffffff")
    assert _snapshot(conn, kuzu, lance, config) == once
    kuzu.close()


def test_a_remove_stopped_after_the_vectors_is_finished_by_the_next_run(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance, _gemini = _library(tmp_path / "stopped")
    with pytest.raises(RuntimeError, match="injected"):
        remove_book_from_graph(conn, _KuzuFailingOn(kuzu, "DETACH DELETE c"),
                               lance, TWO)
    # The graph rolled back whole; the vectors and nothing else are gone.
    assert _named(kuzu, "Maren Vex")[0][1] == tuple(sorted((ONE, TWO)))
    remove_book_from_graph(conn, kuzu, lance, TWO)

    clean_config, clean_conn, clean_kuzu, clean_lance, _ = _library(tmp_path / "clean")
    remove_book_from_graph(clean_conn, clean_kuzu, clean_lance, TWO)
    assert (_snapshot(conn, kuzu, lance, config)
            == _snapshot(clean_conn, clean_kuzu, clean_lance, clean_config))
    kuzu.close()
    clean_kuzu.close()


def test_removing_a_book_drops_its_alias_nodes_and_forms(tmp_path: Path) -> None:
    config, conn, kuzu, lance, _gemini = _library(tmp_path)
    watson = _named(kuzu, "Dr. John Watson")
    assert len(watson) == 1
    assert "watson" in _rows(kuzu, "MATCH (e:Entity {id: $i}) RETURN e.aliases",
                             {"i": watson[0][0]})[0][0]
    assert _rows(kuzu, "MATCH (a:Entity)-[:ALIAS_OF]->(:Entity) RETURN a.id")
    remove_book_from_graph(conn, kuzu, lance, TWO)
    assert _rows(kuzu, "MATCH (a:Entity)-[:ALIAS_OF]->(:Entity) RETURN a.id") == []
    assert "watson" not in _rows(
        kuzu, "MATCH (e:Entity {id: $i}) RETURN e.aliases",
        {"i": watson[0][0]})[0][0]
    kuzu.close()


# --------------------------------------------------------------------------
# moving a book: the round trip
# --------------------------------------------------------------------------


def test_a_move_out_and_back_restores_the_graph_with_no_requests(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance, gemini = _library(tmp_path / "lib")
    original = _snapshot(conn, kuzu, lance, config)
    world_a = worlds.world_of(conn, TWO).id
    world_b = worlds.create_world(conn, "World B")
    assert len(_named(kuzu, "Maren Vex")) == 1  # shared before the move

    # Move TWO to World B.
    job = _move(config, conn, kuzu, lance, gemini, TWO, world_b)
    assert job.state == jobs.DONE and job.stage == "ready"
    assert worlds.world_of(conn, TWO).id == world_b
    check_world_invariant(conn, kuzu)
    # The shared entities are now two each, one per world.
    for name in ("Maren Vex", "Dr. John Watson"):
        nodes = _named(kuzu, name)
        assert len(nodes) == 2
        assert sorted(books for _id, books in nodes) == sorted([(ONE,), (TWO,)])
    assert conn.execute("SELECT status FROM books WHERE id = ?",
                        (TWO,)).fetchone()[0] == "chunked"  # no CLI marked it
    # World A is exactly a graph built from book ONE alone, and TWO's beats
    # were rebuilt from the cache in World B.
    solo_config, solo_conn, solo_kuzu, solo_lance, _ = _library_one(tmp_path / "solo")
    assert (_snapshot(conn, kuzu, lance, config, books=[ONE])
            == _snapshot(solo_conn, solo_kuzu, solo_lance, solo_config, books=[ONE]))
    solo_kuzu.close()
    assert len(_snapshot(conn, kuzu, lance, config, books=[TWO])["beats"]) == 1
    assert len(_snapshot(conn, kuzu, lance, config, books=[TWO])["lance_beats"]) == 1

    # And back.
    job = _move(config, conn, kuzu, lance, gemini, TWO, world_a)
    assert job.state == jobs.DONE
    check_world_invariant(conn, kuzu)
    assert _snapshot(conn, kuzu, lance, config) == original
    assert gemini.requests == []  # everything came from the caches
    kuzu.close()


# --------------------------------------------------------------------------
# the move job: stages, resume, no-op
# --------------------------------------------------------------------------


def test_a_move_runs_its_stages_in_order_and_ends_ready(tmp_path: Path) -> None:
    config, conn, kuzu, lance, gemini = _library(tmp_path)
    world_b = worlds.create_world(conn, "World B")
    seen: list[str] = []

    class Spy:
        def __init__(self, stage) -> None:
            self._stage, self.name = stage, stage.name

        def total(self, ctx):
            return self._stage.total(ctx)

        def run(self, ctx, ctl):
            seen.append(self.name)
            self._stage.run(ctx, ctl)

    assert prepare_move(conn, TWO, world_b)
    job = run_book(conn, config, TWO, [Spy(s) for s in _stages(
        config, kuzu, lance, gemini, world_b)])
    assert seen == ["remove", "reassign", "load", "beats"]
    assert (job.state, job.stage) == (jobs.DONE, "ready")
    kuzu.close()


def test_a_move_to_the_same_world_is_a_no_op(tmp_path: Path) -> None:
    config, conn, kuzu, lance, gemini = _library(tmp_path)
    world_a = worlds.world_of(conn, TWO).id
    assert prepare_move(conn, TWO, world_a) is False  # finished job
    conn.execute("DELETE FROM book_jobs")
    conn.commit()
    assert prepare_move(conn, TWO, world_a) is False  # no job
    kuzu.close()


def test_an_interrupted_move_resumes_at_its_stage(tmp_path: Path) -> None:
    config, conn, kuzu, lance, gemini = _library(tmp_path)
    original = _snapshot(conn, kuzu, lance, config)
    world_a = worlds.world_of(conn, TWO).id
    world_b = worlds.create_world(conn, "World B")
    removed: list[str] = []

    def counting_remove(cfg, c, book_id):
        removed.append(book_id)
        return remove_book_from_graph(c, kuzu, lance, book_id)

    def dying_load(cfg, c, book_id):
        raise RuntimeError("power cut")

    job = _move(config, conn, kuzu, lance, gemini, TWO, world_b,
                load=dying_load, remove=counting_remove)
    assert job.state == jobs.FAILED and job.stage == "load"
    assert worlds.world_of(conn, TWO).id == world_b  # reassign had finished
    # Same command again: past `reassign`, so the book is "already there"
    # yet the unfinished job resumes, and remove does not run again.
    job = _move(config, conn, kuzu, lance, gemini, TWO, world_b,
                remove=counting_remove)
    assert job.state == jobs.DONE
    assert removed == [TWO]
    check_world_invariant(conn, kuzu)
    assert len(_named(kuzu, "Maren Vex")) == 2

    job = _move(config, conn, kuzu, lance, gemini, TWO, world_a)
    assert job.state == jobs.DONE
    assert _snapshot(conn, kuzu, lance, config) == original
    assert gemini.requests == []
    kuzu.close()


def test_a_move_stopped_before_reassign_resumes_there(tmp_path: Path) -> None:
    config, conn, kuzu, lance, gemini = _library(tmp_path)
    world_b = worlds.create_world(conn, "World B")
    removed: list[str] = []

    class DyingReassign(ReassignStage):
        def run(self, ctx, ctl):
            raise RuntimeError("killed")

    def counting_remove(cfg, c, book_id):
        removed.append(book_id)
        return remove_book_from_graph(c, kuzu, lance, book_id)

    assert prepare_move(conn, TWO, world_b)
    stages = _stages(config, kuzu, lance, gemini, world_b, remove=counting_remove)
    stages[1] = DyingReassign(world_b)
    job = run_book(conn, config, TWO, stages)
    assert job.state == jobs.FAILED and job.stage == "reassign"
    assert worlds.world_of(conn, TWO).id != world_b

    job = _move(config, conn, kuzu, lance, gemini, TWO, world_b,
                remove=counting_remove)
    assert job.state == jobs.DONE and removed == [TWO]
    assert worlds.world_of(conn, TWO).id == world_b
    check_world_invariant(conn, kuzu)
    kuzu.close()


def test_a_stale_job_never_makes_a_move_skip_its_remove(tmp_path: Path) -> None:
    config, conn, kuzu, lance, gemini = _library(tmp_path)
    world_b = worlds.create_world(conn, "World B")
    # An ordinary process job that failed at load: not a move's progress.
    jobs.start_job(conn, TWO, "load", 1)
    jobs.fail(conn, TWO, "boom")
    assert prepare_move(conn, TWO, world_b) is True
    assert jobs.get_job(conn, TWO).stage == "remove"
    job = run_book(conn, config, TWO, _stages(config, kuzu, lance, gemini, world_b))
    assert job.state == jobs.DONE
    check_world_invariant(conn, kuzu)  # remove did run: no entity spans worlds
    assert len(_named(kuzu, "Maren Vex")) == 2
    kuzu.close()


def test_a_move_is_refused_while_another_runner_holds_the_book(
    tmp_path: Path,
) -> None:
    config, conn, kuzu, lance, gemini = _library(tmp_path)
    world_b = worlds.create_world(conn, "World B")
    jobs.start_job(conn, TWO, "load", 1)  # running, fresh heartbeat
    with pytest.raises(JobRefused):
        prepare_move(conn, TWO, world_b)
    kuzu.close()


def test_a_book_left_in_another_world_without_removal_fails_the_invariant(
    tmp_path: Path,
) -> None:
    """Why remove must come first: reassigning alone leaves one entity
    listing books of two worlds."""
    config, conn, kuzu, lance, gemini = _library(tmp_path)
    world_b = worlds.create_world(conn, "World B")
    worlds.assign(conn, TWO, world_b)
    with pytest.raises(WorldInvariantError):
        check_world_invariant(conn, kuzu)
    kuzu.close()
