"""Story beats, built: the prompt, verification, and the `beats` stage.

Invented book, invented names; a fake Gemini client and a fake embedder, a
real (tmp) Kuzu, SQLite and Lance. No network.
"""
import gc
import json
from pathlib import Path

import pytest

from lore.beats import build
from lore.beats.build import (
    BeatItem,
    BeatsAnswer,
    ChapterEntity,
    chapter_entities,
    chapter_parts,
    verify_beats,
)
from lore.beats.store import beats_for_entities, count
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import EMBED_DIM, LanceStore
from lore.db.sqlite import open_db
from lore.ingest.models import Chunk
from lore.inference.gemini import GeminiOutputError, GeminiQuotaError
from lore.orchestrator import jobs
from lore.orchestrator.pipeline import StageContext, run_book
from lore.orchestrator.stages import BeatsStage

BOOK = "bkaaaa0000000001"

C1 = ("Mira Voss walked to the iron gate alone at dawn. She kept the Ledger "
      "under her coat and told no one.")
C2 = ("Tomas waited by the quay and handed the guard a folded note. The guild "
      "men bowed low when Mira passed.")
C3 = ("At the council Mira refused to sign the order, and Tomas followed her "
      "out without a word.")

Q1 = "walked to the iron gate alone at dawn"
Q2 = "handed the guard a folded note"
Q3 = "refused to sign the order"

MIRA = ChapterEntity(id="e1", name="Mira Voss", kind="Character", aliases=("Mira",))
TOMAS = ChapterEntity(id="e2", name="Tomas", kind="Character", aliases=())
LEDGER = ChapterEntity(id="e3", name="the Ledger", kind="Object", aliases=())
ENTS = [MIRA, TOMAS, LEDGER]


def _chunk(chapter: int, seq: int, text: str) -> Chunk:
    return Chunk(id=f"{BOOK}:{chapter}:{seq}", book_id=BOOK, chapter_seq=chapter,
                 scene_seq=0, seq=seq, text=text)


def _item(entities, quote, kind="action", text="Something happens."):
    return BeatItem(entities=list(entities), kind=kind, text=text, quote=quote)


# --- verification ------------------------------------------------------------


def test_verify_pins_each_beat_to_the_chunk_holding_its_quote() -> None:
    chunks = [_chunk(1, 0, C1), _chunk(1, 1, C2)]
    got = verify_beats(
        BeatsAnswer(beats=[_item(["Tomas"], Q2), _item(["Mira"], Q1)]),
        BOOK, 1, chunks, ENTS)
    by_quote = {b.quote: b for b in got}
    assert by_quote[Q1].chunk_id == f"{BOOK}:1:0"
    assert by_quote[Q2].chunk_id == f"{BOOK}:1:1"
    assert all(b.book_id == BOOK and b.chapter_seq == 1 for b in got)
    assert by_quote[Q1].entities == ("Mira Voss",)  # an alias maps to the name
    assert len({b.id for b in got}) == 2


def test_verify_drops_a_quote_not_in_the_chapter() -> None:
    chunks = [_chunk(1, 0, C1)]
    answer = BeatsAnswer(beats=[
        _item(["Mira"], "walked to the iron gate alone at midnight"),
        _item(["Mira"], "she wept"),  # too short to prove anything
    ])
    assert verify_beats(answer, BOOK, 1, chunks, ENTS) == []


def test_verify_quote_matching_is_the_claims_matcher() -> None:
    chunks = [_chunk(1, 0, "He said, “I will not go,” and sat down again.")]
    curly = _item(["Tomas"], "\"I will not go,\" and sat down again")
    assert len(verify_beats(BeatsAnswer(beats=[curly]), BOOK, 1, chunks, ENTS)) == 1


def test_verify_drops_unlisted_subjects_and_unlisted_extras() -> None:
    chunks = [_chunk(1, 0, C1), _chunk(1, 1, C2)]
    answer = BeatsAnswer(beats=[
        _item(["The Warden"], Q1),                  # subject not listed: dropped
        _item(["Tomas", "The Warden"], Q2),         # extra not listed: removed
        _item(["The Warden", "Tomas"], Q1 + " "),   # subject not listed: dropped
    ])
    got = verify_beats(answer, BOOK, 1, chunks, ENTS)
    assert len(got) == 1 and got[0].entities == ("Tomas",)


def test_verify_drops_an_unknown_kind_and_overlong_quotes() -> None:
    chunks = [_chunk(1, 0, C1 + " " + C2)]
    long_quote = " ".join(["word"] * 41)
    answer = BeatsAnswer(beats=[
        _item(["Mira"], Q1, kind="gossip"),
        _item(["Mira"], long_quote),
    ])
    assert verify_beats(answer, BOOK, 1, chunks, ENTS) == []


def test_verify_collapses_duplicates_by_subject_and_quote() -> None:
    chunks = [_chunk(1, 0, C1)]
    answer = BeatsAnswer(beats=[
        _item(["Mira"], Q1),
        _item(["Mira Voss"], Q1.upper()),           # same subject, same quote
        _item(["Tomas"], Q1),                       # another subject: kept
    ])
    got = verify_beats(answer, BOOK, 1, chunks, ENTS)
    assert [b.entities[0] for b in got] == ["Mira Voss", "Tomas"]


def test_verify_a_quote_split_over_two_chunks_is_dropped() -> None:
    chunks = [_chunk(1, 0, "Mira Voss walked to the iron"),
              _chunk(1, 1, "gate alone at dawn and left.")]
    assert verify_beats(BeatsAnswer(beats=[_item(["Mira"], Q1)]),
                        BOOK, 1, chunks, ENTS) == []


# --- parts ---------------------------------------------------------------------


def test_chapter_parts_split_at_chunk_boundaries() -> None:
    chunks = [_chunk(1, i, "x" * 40) for i in range(5)]
    assert [len(p) for p in chapter_parts(chunks, max_chars=100)] == [2, 2, 1]
    assert len(chapter_parts(chunks, max_chars=10_000)) == 1
    assert chapter_parts([], max_chars=100) == []
    # one oversized chunk is a part of its own, never cut
    big = [_chunk(1, 0, "y" * 500), _chunk(1, 1, "z" * 10)]
    assert [len(p) for p in chapter_parts(big, max_chars=100)] == [1, 1]
    # order is preserved
    flat = [c.id for p in chapter_parts(chunks, max_chars=100) for c in p]
    assert flat == [c.id for c in chunks]


# --- chapter entities -----------------------------------------------------------


def _graph(path: Path) -> KuzuStore:
    k = KuzuStore(path)
    for i, kind in enumerate(["Character", "Character", "Object", "Place",
                              "Concept", "Faction"]):
        names = ["Mira Voss", "Tomas", "the Ledger", "the Quay", "Honour",
                 "the Guild"]
        k.execute("CREATE (:Entity {id:$i, name:$n, kind:$k, aliases:$a,"
                  " books:[$b]})",
                  {"i": f"e{i + 1}", "n": names[i], "k": kind,
                   "a": ["Mira"] if i == 0 else [], "b": BOOK})
    for cid, chapter in [(f"{BOOK}:1:0", 1), (f"{BOOK}:1:1", 1), (f"{BOOK}:2:0", 2)]:
        k.execute("CREATE (:Chunk {id:$i, book_id:$b, chapter_seq:$c})",
                  {"i": cid, "b": BOOK, "c": chapter})
    mentions = {f"{BOOK}:1:0": ["e1", "e3", "e4", "e5"],
                f"{BOOK}:1:1": ["e1", "e2", "e4"],
                f"{BOOK}:2:0": ["e2", "e6"]}
    for cid, ids in mentions.items():
        for eid in ids:
            k.execute("MATCH (c:Chunk {id:$c}), (e:Entity {id:$e})"
                      " CREATE (c)-[:MENTIONS]->(e)", {"c": cid, "e": eid})
    return k


def test_chapter_entities_ranked_by_mentions_and_kind(tmp_path) -> None:
    k = _graph(tmp_path / "g")
    got = chapter_entities(k, BOOK, 1)
    # places and concepts are excluded; ranked by chunks mentioning them
    assert [e.name for e in got] == ["Mira Voss", "Tomas", "the Ledger"]
    assert got[0].name == "Mira Voss" and got[0].aliases == ("Mira",)
    assert {e.kind for e in got} <= {"Character", "Faction", "Object"}
    assert [e.name for e in chapter_entities(k, BOOK, 2)] == ["Tomas", "the Guild"]
    assert len(chapter_entities(k, BOOK, 1, limit=1)) == 1
    assert chapter_entities(k, BOOK, 9) == []
    assert chapter_entities(k, "other-book", 1) == []
    k.close()


# --- the stage ---------------------------------------------------------------------


class _Client:
    """Answers per chunk text; the Nth call (1-based) in `fail_on` fails."""

    def __init__(self, answers, fail_on=(), bad_on=()):
        self.answers = answers          # {quote-bearing marker: BeatsAnswer}
        self.fail_on = set(fail_on)
        self.bad_on = set(bad_on)
        self.calls = 0
        self.prompts: list[str] = []
        self.closed = False

    async def generate_json(self, prompt, schema, **kw):
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls in self.fail_on:
            raise GeminiQuotaError("HTTP 429")
        if self.calls in self.bad_on:
            raise GeminiOutputError("not json", "")
        for marker, answer in self.answers.items():
            if marker in prompt:
                return answer
        return BeatsAnswer(beats=[])

    async def aclose(self):
        self.closed = True


def _embed_factory(log):
    def factory(config):
        async def embed(texts):
            log.extend(texts)
            return [[0.5] * EMBED_DIM for _ in texts]

        async def aclose():
            pass
        return embed, aclose
    return factory


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    with conn:
        conn.execute("INSERT INTO books (id, title, file_path) VALUES (?,?,?)",
                     (BOOK, "The Ledger Road", "x"))
        for (ch, seq, text) in [(1, 0, C1), (1, 1, C2), (2, 0, C3)]:
            conn.execute(
                "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq,"
                " text) VALUES (?,?,?,0,?,?)", (f"{BOOK}:{ch}:{seq}", BOOK, ch,
                                                seq, text))
    k = _graph(config.kuzu_dir / "graph")
    k.close()
    del k
    gc.collect()
    yield config, conn, tmp_path
    conn.close()


def _stage(env, client, embeds=None, lance=None, **kw):
    config, conn, tmp_path = env
    lance = lance if lance is not None else LanceStore(tmp_path / "lance")
    return BeatsStage(
        client_factory=lambda cfg: client,
        embed_factory=_embed_factory(embeds if embeds is not None else []),
        kuzu_factory=lambda cfg: KuzuStore(config.kuzu_dir / "graph", read_only=True),
        lance_factory=lambda cfg: lance, **kw), lance


ANSWERS = {
    "Chapter 1": BeatsAnswer(beats=[_item(["Mira"], Q1, text="Mira goes to the gate."),
                                    _item(["Tomas"], Q2, kind="treatment")]),
    "Chapter 2": BeatsAnswer(beats=[_item(["Tomas", "the Guild"], Q3,
                                          kind="decision")]),
}


def test_stage_builds_caches_and_saves(env) -> None:
    config, conn, tmp_path = env
    client = _Client(ANSWERS)
    embeds: list[str] = []
    stage, lance = _stage(env, client, embeds)
    job = run_book(conn, config, BOOK, [stage])
    assert job.state == jobs.DONE
    assert client.calls == 2 and client.closed
    assert count(conn, BOOK) == 3
    assert lance.count("beats") == 3
    assert len(embeds) == 3
    cached = sorted(p.name for p in (config.data_dir / "beats" / BOOK).iterdir())
    assert cached == ["1-0.json", "2-0.json"]
    # the stage links names to entity ids (relink) so queries find them
    assert {b.chapter_seq for b in beats_for_entities(conn, ["e2"])} == {1, 2}
    assert len(beats_for_entities(conn, ["e1"])) == 1  # Mira: ch1 only
    assert len(beats_for_entities(conn, ["e6"])) == 1  # the Guild, as a second entity
    # the prompt carries the book title, position and the ranked entities
    assert "The Ledger Road" in client.prompts[0]
    assert "Mira Voss" in client.prompts[0] and "the Ledger" in client.prompts[0]
    assert "Honour" not in client.prompts[0] and "the Quay" not in client.prompts[0]


def test_stage_reports_items_as_chapter_parts(env) -> None:
    config, conn, _ = env
    stage, _lance = _stage(env, _Client(ANSWERS))
    assert stage.total(StageContext(conn, config, BOOK)) == 2
    job = run_book(conn, config, BOOK, [stage])
    assert job.state == jobs.DONE


def test_quota_stop_keeps_cache_and_resume_requests_only_the_rest(env) -> None:
    config, conn, tmp_path = env
    first = _Client(ANSWERS, fail_on={2})
    stage, lance = _stage(env, first)
    job = run_book(conn, config, BOOK, [stage])
    assert job.state == jobs.WAITING_QUOTA and job.stage == "beats"
    assert job.done_items == 1 and job.total_items == 2
    assert first.calls == 2 and first.closed
    assert [p.name for p in (config.data_dir / "beats" / BOOK).iterdir()] == ["1-0.json"]
    assert count(conn, BOOK) == 2  # chapter 1's beats are already saved

    second = _Client(ANSWERS)
    stage2, _ = _stage(env, second, lance=lance)
    job = run_book(conn, config, BOOK, [stage2], force=True)
    assert job.state == jobs.DONE
    assert second.calls == 1, "the cached chapter is not requested again"
    assert "Chapter 2" in second.prompts[0]
    assert count(conn, BOOK) == 3
    assert lance.count("beats") == 3


def test_a_fully_cached_run_builds_no_client(env) -> None:
    config, conn, _ = env
    stage, lance = _stage(env, _Client(ANSWERS))
    run_book(conn, config, BOOK, [stage])

    def boom(cfg):
        raise AssertionError("no client may be built")

    stage2 = BeatsStage(client_factory=boom,
                        embed_factory=_embed_factory([]),
                        kuzu_factory=lambda cfg: KuzuStore(config.kuzu_dir / "graph",
                                                           read_only=True),
                        lance_factory=lambda cfg: lance)
    assert run_book(conn, config, BOOK, [stage2]).state == jobs.DONE
    assert count(conn, BOOK) == 3


def test_a_changed_hash_reruns_only_its_own_file(env) -> None:
    config, conn, _ = env
    stage, lance = _stage(env, _Client(ANSWERS))
    run_book(conn, config, BOOK, [stage])
    path = config.data_dir / "beats" / BOOK / "2-0.json"
    data = json.loads(path.read_text())
    assert data["prompt_hash"]
    data["prompt_hash"] = "stale"
    path.write_text(json.dumps(data))
    client = _Client(ANSWERS)
    stage2, _ = _stage(env, client, lance=lance)
    assert run_book(conn, config, BOOK, [stage2]).state == jobs.DONE
    assert client.calls == 1 and "Chapter 2" in client.prompts[0]
    assert json.loads(path.read_text())["prompt_hash"] != "stale"


def test_the_prompt_hash_follows_the_prompt_text(env, monkeypatch) -> None:
    config, conn, _ = env
    stage, lance = _stage(env, _Client(ANSWERS))
    run_book(conn, config, BOOK, [stage])
    monkeypatch.setattr(build, "BEATS_PROMPT", build.BEATS_PROMPT + "\n- Be brief.")
    client = _Client(ANSWERS)
    stage2, _ = _stage(env, client, lance=lance)
    run_book(conn, config, BOOK, [stage2])
    assert client.calls == 2


def test_a_long_chapter_is_split_into_parts(env, monkeypatch) -> None:
    config, conn, _ = env
    monkeypatch.setattr(build, "MAX_PART_CHARS", len(C1) + 5)
    client = _Client(ANSWERS)
    stage, _lance = _stage(env, client)
    assert stage.total(StageContext(conn, config, BOOK)) == 3
    assert run_book(conn, config, BOOK, [stage]).state == jobs.DONE
    assert client.calls == 3
    assert sorted(p.name for p in (config.data_dir / "beats" / BOOK).iterdir()) == [
        "1-0.json", "1-1.json", "2-0.json"]
    # each part is shown only its own chunks
    assert "folded note" not in client.prompts[0]
    assert "folded note" in client.prompts[1]
    # a beat from part 1 is pinned to that part's chunk and saved under it
    assert conn.execute("SELECT part FROM beats WHERE chunk_id=?",
                        (f"{BOOK}:1:1",)).fetchone() == (1,)


def test_disabled_stage_is_a_no_op(env) -> None:
    config, conn, _ = env
    config = config.model_copy(update={
        "beats": config.beats.model_copy(update={"enabled": False})})
    client = _Client(ANSWERS)
    stage, _lance = _stage(env, client)
    assert run_book(conn, config, BOOK, [stage]).state == jobs.DONE
    assert client.calls == 0 and count(conn, BOOK) == 0
    assert not (config.data_dir / "beats").exists()


def test_the_beats_switch_defaults_on_and_reads_from_toml(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert LoreConfig.load().beats.enabled is True
    (tmp_path / "lore.toml").write_text("[lore.beats]\nenabled = false\n")
    assert LoreConfig.load().beats.enabled is False


def test_stage_stops_at_the_answer_reserve_before_any_request(env) -> None:
    config, conn, _ = env
    config = config.model_copy(update={
        "processing": config.processing.model_copy(update={"answer_reserve": 10**9})})
    client = _Client(ANSWERS)
    stage, _lance = _stage(env, client)
    job = run_book(conn, config, BOOK, [stage])
    assert job.state == jobs.WAITING_QUOTA
    assert client.calls == 0


def test_an_unusable_answer_skips_the_part_without_caching(env) -> None:
    config, conn, _ = env
    client = _Client(ANSWERS, bad_on={1})
    stage, _lance = _stage(env, client)
    job = run_book(conn, config, BOOK, [stage])
    assert job.state == jobs.DONE
    assert [p.name for p in (config.data_dir / "beats" / BOOK).iterdir()] == ["2-0.json"]
    assert "1-0" in "".join(stage.skipped)


def test_a_book_with_no_graph_has_nothing_to_do(env) -> None:
    config, conn, _ = env
    client = _Client(ANSWERS)
    stage = BeatsStage(client_factory=lambda cfg: client,
                       embed_factory=_embed_factory([]),
                       kuzu_factory=lambda cfg: None,
                       lance_factory=lambda cfg: None)
    assert run_book(conn, config, BOOK, [stage]).state == jobs.DONE
    assert client.calls == 0


def test_a_failed_embed_leaves_the_part_to_be_redone(env) -> None:
    config, conn, tmp_path = env
    lance = LanceStore(tmp_path / "lance")

    def bad_embed(config):
        async def embed(texts):
            raise RuntimeError("embedder down")

        async def aclose():
            pass
        return embed, aclose

    stage = BeatsStage(client_factory=lambda cfg: _Client(ANSWERS),
                       embed_factory=bad_embed,
                       kuzu_factory=lambda cfg: KuzuStore(config.kuzu_dir / "graph",
                                                          read_only=True),
                       lance_factory=lambda cfg: lance)
    assert run_book(conn, config, BOOK, [stage]).state == jobs.FAILED
    assert count(conn, BOOK) == 0 and lance.count("beats") == 0
    # chapter 1's answer was paid for and cached; the retry asks only chapter 2
    client = _Client(ANSWERS)
    stage2, _ = _stage(env, client, lance=lance)
    assert run_book(conn, config, BOOK, [stage2]).state == jobs.DONE
    assert client.calls == 1 and count(conn, BOOK) == 3 and lance.count("beats") == 3


# --- relink after a graph load ---------------------------------------------------


def test_every_graph_load_relinks_the_beats(env, monkeypatch) -> None:
    from lore import cli
    config, conn, tmp_path = env
    stage, _lance = _stage(env, _Client(ANSWERS))
    run_book(conn, config, BOOK, [stage])
    with conn:
        conn.execute("UPDATE beat_entities SET entity_id = NULL")
    assert beats_for_entities(conn, ["e2"]) == []
    monkeypatch.setattr(cli, "_load_book_sync", lambda *a, **k: "report")
    stages = cli.default_stages(config, skip_extract=True)
    load = next(s for s in stages if s.name == "load")
    assert load.reload.__self__ is load
    assert load._load_fn(config, conn, BOOK) == "report"
    assert {b.chapter_seq for b in beats_for_entities(conn, ["e2"])} == {1, 2}


# --- the prompt ------------------------------------------------------------------


def test_the_prompt_asks_for_the_spec_rules() -> None:
    p = build.BEATS_PROMPT
    for must in ("{book_title}", "{position}", "{entities}", "{text}",
                 "word for word", "up to 25"):
        assert must in p
    for kind in ("action", "decision", "feeling", "treatment", "change"):
        assert kind in p
