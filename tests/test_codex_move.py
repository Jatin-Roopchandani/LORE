"""The codex through remove and move: entries of the entities a book touches
are dropped and rebuilt, and a move out and back asks the model nothing the
second time (the cache is keyed by evidence).

Reuses the two-book library of test_book_remove (invented names).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lore import worlds
from lore.codex import build, store
from lore.codex.build import CodexAnswer, StatementItem
from lore.db.lance_store import EMBED_DIM
from lore.graph.loader import check_world_invariant, load_book
from lore.graph.remove import remove_book_from_graph
from lore.orchestrator import jobs
from lore.orchestrator.move import prepare_move
from lore.orchestrator.pipeline import run_book
from lore.orchestrator.stages import (
    BeatsStage,
    CodexStage,
    LoadStage,
    ReassignStage,
    RemoveStage,
)
from tests.test_book_remove import ONE, TWO, FakeGemini, _build, _Graph, _named

Q_ONE = "refused to let anyone pass through it"
Q_TWO = "gave the silver key to Zed without saying a single word"
Q_WHIT = "wrote down every word she said in his small black notebook"


@pytest.fixture(autouse=True)
def _low_threshold(monkeypatch):
    monkeypatch.setattr(build, "MIN_MENTION_CHUNKS", 1)


class CodexClient:
    """Answers every entity with the statements whose quotes its prompt holds,
    and records each request."""

    def __init__(self) -> None:
        self.names: list[str] = []

    async def generate_json(self, prompt, schema, **kw):
        self.names.append(prompt.split("Subject: ")[1].split(" (")[0])
        items = [StatementItem(aspect="nature", text="Something is shown.", quote=q)
                 for q in (Q_ONE, Q_TWO, Q_WHIT) if q in prompt]
        return CodexAnswer(summary="An entity.", statements=items)

    async def aclose(self) -> None:
        pass


def _stages(config, kuzu, lance, gemini, client, world_id=None):
    def load(cfg, conn, book_id):
        return asyncio.run(load_book(cfg, conn, kuzu, lance, book_id,
                                     gemini.embed, gemini.adjudicate))

    async def aclose() -> None:
        pass

    def embed_factory(cfg):
        return gemini.embed, aclose

    stages = [
        LoadStage(load),
        BeatsStage(client_factory=gemini.beats_client, embed_factory=embed_factory,
                   kuzu_factory=lambda cfg: _Graph(kuzu), lance_factory=lambda cfg: lance),
        CodexStage(client_factory=lambda cfg: client, embed_factory=embed_factory,
                   kuzu_factory=lambda cfg: _Graph(kuzu),
                   lance_factory=lambda cfg: lance, all_worlds=world_id is not None),
    ]
    if world_id is not None:
        stages = [
            RemoveStage(lambda cfg, conn, book_id: remove_book_from_graph(
                conn, kuzu, lance, book_id)),
            ReassignStage(world_id),
        ] + stages
    return stages


def _library(tmp_path: Path):
    config, conn, kuzu, lance = _build(tmp_path)
    gemini, client = FakeGemini(), CodexClient()
    for book in (ONE, TWO):
        job = run_book(conn, config, book, _stages(config, kuzu, lance, gemini, client))
        assert job.state == jobs.DONE, job.stopped_reason
    return config, conn, kuzu, lance, gemini, client


def _move(config, conn, kuzu, lance, gemini, client, book, world_id):
    assert prepare_move(conn, book, world_id)
    return run_book(conn, config, book,
                    _stages(config, kuzu, lance, gemini, client, world_id), claimed=True)


def _codex(conn, lance) -> dict:
    return {
        "summaries": sorted(conn.execute(
            "SELECT entity_id, world_id, name, kind, summary, prompt_hash,"
            " evidence_hash FROM codex_summaries")),
        "entries": sorted(conn.execute(
            "SELECT id, entity_id, world_id, aspect, position, statement, quote,"
            " chunk_id FROM codex_entries")),
        "lance": sorted(r["id"] for r in lance._db.open_table("codex")
                        .to_arrow().to_pylist()),
    }


def _chunk_books(snapshot) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for _id, _eid, world, *_rest, chunk_id in snapshot["entries"]:
        out.setdefault(world, set()).add(chunk_id.split(":")[0])
    return out


def test_processing_builds_each_worlds_codex_from_its_own_books(tmp_path: Path) -> None:
    config, conn, kuzu, lance, _g, client = _library(tmp_path)
    world_a = worlds.world_of(conn, TWO).id
    maren = _named(kuzu, "Maren Vex")[0][0]
    entry = store.entries_for(conn, [maren])[0]
    assert entry.world_id == world_a
    assert {s.chunk_id.split(":")[0] for s in entry.statements} == {ONE, TWO}
    assert len(_codex(conn, lance)["lance"]) == len(_codex(conn, lance)["entries"])
    kuzu.close()


def test_remove_drops_the_entries_of_entities_the_book_touches(tmp_path: Path) -> None:
    config, conn, kuzu, lance, _g, _c = _library(tmp_path)
    bo = _named(kuzu, "Bo")
    zed = _named(kuzu, "Zed")[0][0]
    maren = _named(kuzu, "Maren Vex")[0][0]
    assert store.with_entries(conn, [maren, zed])
    report = remove_book_from_graph(conn, kuzu, lance, TWO)
    assert report.codex >= 2
    assert store.with_entries(conn, [maren, zed]) == []
    after = _codex(conn, lance)
    assert len(after["lance"]) == len(after["entries"])
    # an entity of the other book alone keeps its entry
    assert bo and store.with_entries(conn, [bo[0][0]]) == [bo[0][0]]
    assert remove_book_from_graph(conn, kuzu, lance, TWO).codex == 0  # repeatable
    kuzu.close()


def test_a_move_out_and_back_rebuilds_then_asks_nothing(tmp_path: Path) -> None:
    config, conn, kuzu, lance, gemini, client = _library(tmp_path)
    original = _codex(conn, lance)
    world_a = worlds.world_of(conn, TWO).id
    world_b = worlds.create_world(conn, "World B")
    asked = len(client.names)

    job = _move(config, conn, kuzu, lance, gemini, client, TWO, world_b)
    assert job.state == jobs.DONE
    check_world_invariant(conn, kuzu)
    moved = _codex(conn, lance)
    assert len(client.names) > asked  # new evidence in both worlds
    books_by_world = _chunk_books(moved)
    assert books_by_world[world_a] == {ONE}
    assert books_by_world[world_b] == {TWO}
    assert len(moved["lance"]) == len(moved["entries"])
    assert {w for _e, w, *_r in moved["summaries"]} == {world_a, world_b}

    asked = len(client.names)
    job = _move(config, conn, kuzu, lance, gemini, client, TWO, world_a)
    assert job.state == jobs.DONE
    check_world_invariant(conn, kuzu)
    assert len(client.names) == asked, "every entry came from the cache"
    assert _codex(conn, lance) == original
    assert gemini.requests == []
    kuzu.close()


def test_move_stages_end_with_the_codex_of_every_world() -> None:
    from lore.cli import default_stages, move_stages
    from lore.config import LoreConfig

    config = LoreConfig.load()
    moves = move_stages(config, 2)
    assert [s.name for s in moves] == ["remove", "reassign", "load", "beats", "codex"]
    assert moves[-1]._all_worlds is True
    plain = default_stages(config, skip_extract=True)
    assert [s.name for s in plain][-2:] == ["beats", "codex"]
    assert plain[-1]._all_worlds is False
