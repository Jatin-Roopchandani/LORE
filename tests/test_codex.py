"""The codex: store, evidence, verification, the `codex` stage, relink, and a
move that drops and rebuilds entries.

Invented books and names; a fake Gemini client and a fake embedder, a real
(tmp) Kuzu, SQLite and Lance. No network.
"""
import gc
import json
import re
import sqlite3
from pathlib import Path

import pytest

from lore import worlds
from lore.beats.store import Beat, beat_id, relink as relink_beats, save_beats
from lore.codex import build, store
from lore.codex.build import CodexAnswer, CodexEntity, StatementItem
from lore.codex.store import CodexEntry, Statement
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import EMBED_DIM, LanceStore
from lore.db.sqlite import open_db
from lore.ingest.models import Chunk
from lore.inference.gemini import GeminiOutputError, GeminiQuotaError
from lore.orchestrator import jobs
from lore.orchestrator.pipeline import STAGES, StageContext, run_book
from lore.orchestrator.stages import CodexStage

BOOK = "bkaaaa0000000001"
BOOK2 = "bkbbbb0000000002"

C0 = ("Mira Voss walked to the iron gate alone at dawn. She kept the Ledger "
      "under her coat and told no one.")
C1 = ("Tomas waited by the quay and handed the guard a folded note. The guild "
      "men bowed low when Mira passed.")
C2 = ("At the council Mira refused to sign the order, and Tomas followed her "
      "out without a word.")
C3 = ("Years later Mira Voss opened the Ledger again and burned every page of "
      "it in the open square.")

Q0 = "kept the Ledger under her coat and told no one"
Q2 = "Mira refused to sign the order"
Q3 = "burned every page of it in the open square"


def _chunk(book, chapter, seq, text):
    return Chunk(id=f"{book}:{chapter}:{seq}", book_id=book, chapter_seq=chapter,
                 scene_seq=0, seq=seq, text=text)


def _item(aspect, quote, text="Something is established."):
    return StatementItem(aspect=aspect, text=text, quote=quote)


MIRA = CodexEntity(id="e1", name="Mira Voss", kind="Character", aliases=("Mira",))


def _evidence(chunks, labels=None):
    passages = tuple(build.Passage(n=i, chunk_id=c.id, book_id=c.book_id,
                                   chapter_seq=c.chapter_seq, text=c.text)
                     for i, c in enumerate(chunks, start=1))
    return build.Evidence(beats=(), passages=passages,
                          labels=tuple(labels or [f"p{p.n}" for p in passages]))


# --- verification -------------------------------------------------------------


def test_verify_pins_orders_and_drops() -> None:
    ev = _evidence([_chunk(BOOK, 1, 0, C0), _chunk(BOOK, 2, 0, C2),
                    _chunk(BOOK, 3, 0, C3)])
    raw = CodexAnswer(summary="  A woman who\nkeeps a book.  ", statements=[
        _item("change", Q3, "Burns it."),
        _item("role", Q2, "Refuses."),
        _item("nature", Q0, "Secretive."),
        _item("nature", Q0.upper(), "Same quote, same aspect."),   # repeat
        _item("gossip", Q0),                                       # unknown aspect
        _item("nature", "kept the Ledger under her cloak and told no one"),  # not in text
        _item("nature", "told no one"),                            # too short
        _item("nature", Q0, text="  "),                            # empty text
    ])
    entry = build.verify_entry(raw, MIRA, 7, ev, "hash")
    assert [s.aspect for s in entry.statements] == ["nature", "role", "change"]
    assert [s.chunk_id for s in entry.statements] == [
        f"{BOOK}:1:0", f"{BOOK}:2:0", f"{BOOK}:3:0"]
    assert entry.summary == "A woman who keeps a book."
    assert (entry.entity_id, entry.world_id, entry.evidence_hash) == ("e1", 7, "hash")
    assert entry.prompt_hash == build.prompt_hash()


def test_verify_orders_by_story_within_an_aspect_and_caps() -> None:
    ev = _evidence([_chunk(BOOK, 1, 0, C0), _chunk(BOOK, 3, 0, C3)])
    raw = CodexAnswer(summary="", statements=[
        _item("history", Q3, "Late."), _item("history", Q0, "Early.")])
    entry = build.verify_entry(raw, MIRA, 1, ev, "h")
    assert [s.text for s in entry.statements] == ["Early.", "Late."]
    assert entry.summary == "Early."  # no summary given: the first statement


def test_verify_returns_none_when_nothing_survives() -> None:
    ev = _evidence([_chunk(BOOK, 1, 0, C0)])
    raw = CodexAnswer(summary="x", statements=[_item("nature", "not in the text at all here")])
    assert build.verify_entry(raw, MIRA, 1, ev, "h") is None
    assert build.verify_entry(CodexAnswer(summary="x", statements=[]),
                              MIRA, 1, ev, "h") is None


def test_verify_drops_overlong_quotes_and_a_split_quote(monkeypatch) -> None:
    ev = _evidence([_chunk(BOOK, 1, 0, "Mira Voss walked to the iron"),
                    _chunk(BOOK, 1, 1, "gate alone at dawn and left.")])
    raw = CodexAnswer(summary="s", statements=[
        _item("nature", "Mira Voss walked to the iron gate alone at dawn")])
    assert build.verify_entry(raw, MIRA, 1, ev, "h") is None
    monkeypatch.setattr(build, "MAX_QUOTE_WORDS", 3)
    ev = _evidence([_chunk(BOOK, 1, 0, C0)])
    assert build.verify_entry(CodexAnswer(summary="s", statements=[
        _item("nature", Q0)]), MIRA, 1, ev, "h") is None


# --- the prompt and the cache keys ----------------------------------------------


def test_prompt_renders_entity_moments_and_numbered_passages() -> None:
    ev = _evidence([_chunk(BOOK, 1, 0, C0), _chunk(BOOK, 2, 0, C2)], ["Book, chapter 1", "Book, chapter 2"])
    beat = Beat(id="b", book_id=BOOK, chapter_seq=1, chunk_id=f"{BOOK}:1:0",
                entities=("Mira Voss",), kind="action", text="Mira goes to the gate.",
                quote=Q0)
    ev = build.Evidence(beats=(beat,), passages=ev.passages, labels=ev.labels)
    prompt = build.render_prompt(MIRA, ev)
    assert "Mira Voss (Character); also called: Mira" in prompt
    assert "- Mira goes to the gate." in prompt
    assert f"[2] Book, chapter 2\n{C2}" in prompt


def test_evidence_hash_follows_what_the_prompt_shows() -> None:
    ev = _evidence([_chunk(BOOK, 1, 0, C0)])
    base = build.evidence_hash(MIRA, ev)
    assert base == build.evidence_hash(MIRA, ev)
    assert base != build.evidence_hash(MIRA, _evidence([_chunk(BOOK, 1, 0, C0 + " x")]))
    other = CodexEntity(id="e1", name="Mira Voss", kind="Character", aliases=())
    assert base != build.evidence_hash(other, ev)


def test_cache_is_keyed_by_prompt_and_evidence(tmp_path, monkeypatch) -> None:
    path = build.cache_path(tmp_path, "e1", "abc")
    assert path != build.cache_path(tmp_path, "e1", "abd")
    answer = CodexAnswer(summary="s", statements=[_item("nature", Q0)])
    assert build.read_cached(path, "abc") is None
    build.write_cached(path, "abc", answer)
    assert build.read_cached(path, "abc") == answer
    assert build.read_cached(path, "other") is None
    monkeypatch.setattr(build, "CODEX_PROMPT", build.CODEX_PROMPT + "\n- Be brief.")
    assert build.read_cached(path, "abc") is None
    path.write_text("not json")
    assert build.read_cached(path, "abc") is None


def test_prompt_names_no_book_character_or_genre() -> None:
    banned = (r"\b(holmes|watson|sherlock|small|hope|drebber|stangerson|sholto|ferrier|"
              r"tonga|morstan|lestrade|baker street|mormon|agra|crime|crimes|murder|"
              r"villain|villains|detective|treasure|darcy|bennet|wizard|dragon|magic|"
              r"kingdom|elf|elves|quest|sword)\b")
    assert not re.findall(banned, build.CODEX_PROMPT.lower())


# --- the store -------------------------------------------------------------------


def _conn(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "lore.db")


def _entry(entity_id="e1", world=1, name="Mira Voss", texts=("One.", "Two.")) -> CodexEntry:
    return CodexEntry(
        entity_id=entity_id, world_id=world, name=name, kind="Character",
        summary="A woman.", prompt_hash="p", evidence_hash="v",
        statements=tuple(Statement(aspect="nature" if i == 0 else "role", text=t,
                                   quote=f"quote number {i} here ok",
                                   chunk_id=f"{BOOK}:1:{i}")
                         for i, t in enumerate(texts)))


def test_store_round_trip_and_replace(tmp_path) -> None:
    conn = _conn(tmp_path)
    ids = store.save_entry(conn, _entry())
    assert ids == [store.statement_id("e1", 0, "One."), store.statement_id("e1", 1, "Two.")]
    got = store.entries_for(conn, ["e1", "nope", "e1"])
    assert len(got) == 1 and got[0].summary == "A woman."
    assert [(s.aspect, s.text, s.position, s.id) for s in got[0].statements] == [
        ("nature", "One.", 0, ids[0]), ("role", "Two.", 1, ids[1])]
    assert store.summary_for(conn, "e1").prompt_hash == "p"
    assert store.statement_ids(conn, ["e1"]) == sorted(ids)
    # saving again replaces, never duplicates
    store.save_entry(conn, _entry(texts=("Only.",)))
    assert [s.text for s in store.entries_for(conn, ["e1"])[0].statements] == ["Only."]
    assert conn.execute("SELECT COUNT(*) FROM codex_entries").fetchone()[0] == 1


def test_store_world_queries_and_delete(tmp_path) -> None:
    conn = _conn(tmp_path)
    store.save_entry(conn, _entry("e1", 1, "Mira"))
    store.save_entry(conn, _entry("e2", 1, "Ash"))
    store.save_entry(conn, _entry("e3", 2, "Zed"))
    assert [s.name for s in store.summaries_for_world(conn, 1)] == ["Ash", "Mira"]
    assert store.count(conn, 1) == 2 and store.count(conn, 3) == 0
    assert store.world_ids_with_codex(conn) == [1, 2]
    assert store.with_entries(conn, ["e1", "e3", "zz"]) == ["e1", "e3"]
    gone = store.delete_entities(conn, ["e1", "e2"])
    assert len(gone) == 4
    assert store.entries_for(conn, ["e1", "e2"]) == []
    assert [s.name for s in store.entries_for(conn, ["e3"])] == ["Zed"]
    assert store.delete_entities(conn, ["e1"]) == []  # safe to repeat


def test_lance_codex_table(tmp_path) -> None:
    lance = LanceStore(tmp_path / "lance")
    assert "codex" in lance.table_names()
    v = [0.5] * EMBED_DIM
    lance.upsert_codex([{"id": "a", "vector": v}, {"id": "b", "vector": v}])
    lance.upsert_codex([{"id": "a", "vector": v}])
    assert lance.count("codex") == 2
    assert {r["id"] for r in lance.search_codex(v, ["a", "zz"], 5)} == {"a"}
    assert lance.search_codex(v, [], 5) == []
    lance.delete_codex(["a"])
    assert lance.count("codex") == 1
    lance.delete_codex([])


class _Graph:
    def __init__(self, k):
        self._k = k

    def execute(self, cypher, params=None):
        return self._k.execute(cypher, params)

    def close(self):
        pass


def test_relink_follows_merges_and_drops_gone_entities(tmp_path) -> None:
    conn = _conn(tmp_path)
    k = KuzuStore(tmp_path / "kuzu")
    for i, n in [("e1", "Mira"), ("e2", "Mira Voss"), ("e3", "Tomas"), ("e4", "Ash")]:
        k.execute("CREATE (:Entity {id:$i, name:$n, kind:'Character', aliases:[], books:['b']})",
                  {"i": i, "n": n})
    k.execute("MATCH (a:Entity {id:'e1'}), (c:Entity {id:'e2'}) CREATE (a)-[:ALIAS_OF]->(c)")
    k.execute("MATCH (a:Entity {id:'e4'}), (c:Entity {id:'e3'}) CREATE (a)-[:ALIAS_OF]->(c)")
    store.save_entry(conn, _entry("e1", 1, "Mira"))      # a stub: moves to e2
    store.save_entry(conn, _entry("e3", 1, "Tomas"))
    store.save_entry(conn, _entry("e4", 1, "Ash"))       # stub of e3, which has one: dropped
    store.save_entry(conn, _entry("e9", 1, "Gone"))      # no such entity: dropped
    store.save_entry(conn, _entry("e2", 1, "Keep"))      # untouched ... but e1 collides
    dropped: list[str] = []

    class Lance:
        def delete_codex(self, ids):
            dropped.extend(ids)

    changed = store.relink(conn, k, Lance())
    # e1 -> e2 collides with e2's own entry, so e1 is dropped as well
    assert store.with_entries(conn, ["e1", "e2", "e3", "e4", "e9"]) == ["e2", "e3"]
    assert changed == 3 and len(dropped) == 6
    # a plain move: the stub's entry goes to the entity it became
    store.delete_entities(conn, ["e2"])
    store.save_entry(conn, _entry("e1", 1, "Mira"))
    assert store.relink(conn, k) == 1
    assert store.with_entries(conn, ["e1", "e2"]) == ["e2"]
    assert store.relink(conn, k) == 0
    k.close()


# --- selection and evidence --------------------------------------------------------


def _graph(path: Path) -> KuzuStore:
    k = KuzuStore(path)
    kinds = {"e1": ("Mira Voss", "Character"), "e2": ("Tomas", "Character"),
             "e3": ("the Ledger", "Object"), "e4": ("the Quay", "Place"),
             "e5": ("Honour", "Concept"), "e6": ("the Guild", "Faction"),
             "e7": ("Brannoch", "Book")}
    for eid, (name, kind) in kinds.items():
        k.execute("CREATE (:Entity {id:$i, name:$n, kind:$k, aliases:$a, books:[$b]})",
                  {"i": eid, "n": name, "k": kind,
                   "a": ["Mira"] if eid == "e1" else [], "b": BOOK})
    mentions = {f"{BOOK}:1:0": ["e1", "e3", "e4", "e5", "e7"],
                f"{BOOK}:1:1": ["e1", "e2", "e4", "e6"],
                f"{BOOK}:2:0": ["e1", "e2", "e6", "e3"],
                f"{BOOK}:3:0": ["e1", "e3", "e4"]}
    for cid in mentions:
        k.execute("CREATE (:Chunk {id:$i, book_id:$b, chapter_seq:$c})",
                  {"i": cid, "b": BOOK, "c": int(cid.split(":")[1])})
    for cid, ids in mentions.items():
        for eid in ids:
            k.execute("MATCH (c:Chunk {id:$c}), (e:Entity {id:$e}) CREATE (c)-[:MENTIONS]->(e)",
                      {"c": cid, "e": eid})
    return k


def test_significant_entities_by_kind_threshold_and_cap(tmp_path) -> None:
    k = _graph(tmp_path / "g")
    got = build.significant_entities(k, [BOOK], 10, min_chunks=2)
    # Book is not a codex kind; ranked by chunks mentioning, then name
    assert [(e.name, e.mentions) for e in got] == [
        ("Mira Voss", 4), ("the Ledger", 3), ("the Quay", 3), ("Tomas", 2),
        ("the Guild", 2)]
    assert got[0].aliases == ("Mira",)
    assert [e.name for e in build.significant_entities(k, [BOOK], 2, min_chunks=2)] == [
        "Mira Voss", "the Ledger"]
    assert [e.name for e in build.significant_entities(k, [BOOK], 10, min_chunks=4)] == [
        "Mira Voss"]
    assert build.significant_entities(k, ["other"], 10, min_chunks=1) == []
    assert build.significant_entities(k, [], 10) == []
    assert build.significant_entities(k, [BOOK], 0) == []
    assert build.MIN_MENTION_CHUNKS >= 2 and build.CODEX_MAX_PER_BOOK == 150
    k.close()


# --- the stage --------------------------------------------------------------------


class _Client:
    """Answers by a marker found in the prompt; the Nth call in `fail_on` hits
    the quota, in `bad_on` returns unusable output."""

    def __init__(self, answers, fail_on=(), bad_on=()):
        self.answers = answers
        self.fail_on, self.bad_on = set(fail_on), set(bad_on)
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
            if f"Subject: {marker}" in prompt:
                return answer
        return CodexAnswer(summary="", statements=[])

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


ANSWERS = {
    "Mira Voss": CodexAnswer(summary="A keeper of the Ledger.", statements=[
        _item("nature", Q0, "She keeps the Ledger."),
        _item("role", Q2, "She refuses to sign."),
        _item("change", Q3, "She burns the Ledger.")]),
    "the Ledger": CodexAnswer(summary="A book.", statements=[
        _item("history", Q3, "It is burned.")]),
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(build, "MIN_MENTION_CHUNKS", 3)
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    world = worlds.create_world(conn, "Ash World")
    with conn:
        conn.execute("INSERT INTO books (id, title, file_path, world_id) VALUES (?,?,?,?)",
                     (BOOK, "The Ledger Road", "x", world))
        for ch, seq, text in [(1, 0, C0), (1, 1, C1), (2, 0, C2), (3, 0, C3)]:
            conn.execute(
                "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                " VALUES (?,?,?,0,?,?)", (f"{BOOK}:{ch}:{seq}", BOOK, ch, seq, text))
    k = _graph(config.kuzu_dir / "graph")
    k.close()
    del k
    gc.collect()
    yield config, conn, tmp_path, world
    conn.close()


def _stage(env, client, embeds=None, lance=None, **kw):
    config, conn, tmp_path, _w = env
    lance = lance if lance is not None else LanceStore(tmp_path / "lance")
    return CodexStage(
        client_factory=lambda cfg: client,
        embed_factory=_embed_factory(embeds if embeds is not None else []),
        kuzu_factory=lambda cfg: KuzuStore(config.kuzu_dir / "graph", read_only=True),
        lance_factory=lambda cfg: lance, **kw), lance


def test_the_stage_follows_beats_in_the_pipeline() -> None:
    assert STAGES.index("codex") == STAGES.index("beats") + 1 < STAGES.index("ready")
    assert CodexStage.name == "codex"


def test_stage_builds_caches_and_saves(env) -> None:
    config, conn, tmp_path, world = env
    client = _Client(ANSWERS)
    embeds: list[str] = []
    stage, lance = _stage(env, client, embeds)
    # Mira (4 chunks), the Ledger (3), the Quay (3): Tomas and the Guild fall below 3
    assert stage.total(StageContext(conn, config, BOOK)) == 3
    job = run_book(conn, config, BOOK, [stage])
    assert job.state == jobs.DONE
    assert client.calls == 3 and client.closed
    mira = store.entries_for(conn, ["e1"])[0]
    assert (mira.world_id, mira.name, mira.summary) == (world, "Mira Voss", "A keeper of the Ledger.")
    assert [s.aspect for s in mira.statements] == ["nature", "role", "change"]
    assert [s.chunk_id for s in mira.statements] == [f"{BOOK}:1:0", f"{BOOK}:2:0", f"{BOOK}:3:0"]
    assert store.entries_for(conn, ["e3"])[0].statements[0].quote == Q3
    assert store.with_entries(conn, ["e4"]) == []     # the Quay: an empty answer
    assert lance.count("codex") == 4 and len(embeds) == 4
    assert "Mira Voss (nature): She keeps the Ledger." in embeds
    assert len(list(config.codex_dir.iterdir())) == 3
    # the prompt carries the entity, its aliases and passages in story order
    p = client.prompts[0]
    assert "Subject: Mira Voss (Character); also called: Mira" in p
    assert p.index(C0) < p.index(C2) < p.index(C3)


def test_a_finished_stage_asks_nothing_and_builds_nothing(env) -> None:
    config, conn, tmp_path, _w = env
    stage, lance = _stage(env, _Client(ANSWERS))
    run_book(conn, config, BOOK, [stage])

    def boom(cfg):
        raise AssertionError("nothing may be built")

    stage2 = CodexStage(client_factory=boom, embed_factory=boom,
                        kuzu_factory=lambda cfg: KuzuStore(config.kuzu_dir / "graph", read_only=True),
                        lance_factory=boom)
    assert run_book(conn, config, BOOK, [stage2]).state == jobs.DONE


def test_quota_stop_keeps_cache_and_resume_requests_only_the_rest(env) -> None:
    config, conn, tmp_path, _w = env
    first = _Client(ANSWERS, fail_on={2})
    stage, lance = _stage(env, first)
    job = run_book(conn, config, BOOK, [stage])
    assert job.state == jobs.WAITING_QUOTA and job.stage == "codex"
    assert job.done_items == 1 and job.total_items == 3
    assert first.calls == 2 and first.closed
    assert len(list(config.codex_dir.iterdir())) == 1
    assert store.with_entries(conn, ["e1", "e3"]) == ["e1"]

    second = _Client(ANSWERS)
    stage2, _ = _stage(env, second, lance=lance)
    job = run_book(conn, config, BOOK, [stage2], force=True)
    assert job.state == jobs.DONE
    assert second.calls == 2, "the entity already answered is not requested again"
    assert not any("Subject: Mira Voss" in p for p in second.prompts)
    assert store.with_entries(conn, ["e1", "e3"]) == ["e1", "e3"]
    assert lance.count("codex") == 4


def test_cached_answer_is_saved_without_a_request(env) -> None:
    """A run killed after a request, before the save, redoes only the save."""
    config, conn, tmp_path, _w = env
    stage, lance = _stage(env, _Client(ANSWERS))
    run_book(conn, config, BOOK, [stage])
    store.delete_entities(conn, ["e1"])
    lance.delete_codex(sorted(r["id"] for r in lance._db.open_table("codex").to_arrow().to_pylist()))
    client = _Client(ANSWERS)
    stage2, _ = _stage(env, client, lance=lance)
    assert run_book(conn, config, BOOK, [stage2]).state == jobs.DONE
    assert client.calls == 0
    assert store.with_entries(conn, ["e1"]) == ["e1"]
    assert lance.count("codex") == 3


def test_changed_prompt_or_evidence_rebuilds_only_what_changed(env, monkeypatch) -> None:
    config, conn, tmp_path, _w = env
    stage, lance = _stage(env, _Client(ANSWERS))
    run_book(conn, config, BOOK, [stage])
    # new text in one chunk the Ledger and Mira share changes both; the Quay too
    with conn:
        conn.execute("UPDATE chunks SET text = text || ' It rained.' WHERE id = ?",
                     (f"{BOOK}:3:0",))
    client = _Client(ANSWERS)
    stage2, _ = _stage(env, client, lance=lance)
    run_book(conn, config, BOOK, [stage2])
    assert client.calls == 3
    monkeypatch.setattr(build, "CODEX_PROMPT", build.CODEX_PROMPT + "\n- Be brief.")
    client = _Client(ANSWERS)
    stage3, _ = _stage(env, client, lance=lance)
    run_book(conn, config, BOOK, [stage3])
    assert client.calls == 3


def test_a_rebuild_replaces_old_statements_everywhere(env) -> None:
    config, conn, tmp_path, _w = env
    stage, lance = _stage(env, _Client(ANSWERS))
    run_book(conn, config, BOOK, [stage])
    with conn:
        conn.execute("UPDATE chunks SET text = text || ' It rained.' WHERE id = ?",
                     (f"{BOOK}:3:0",))
    fewer = {"Mira Voss": CodexAnswer(summary="A keeper.", statements=[_item("nature", Q0, "Kept.")])}
    stage2, _ = _stage(env, _Client(fewer), lance=lance)
    run_book(conn, config, BOOK, [stage2])
    assert [s.text for s in store.entries_for(conn, ["e1"])[0].statements] == ["Kept."]
    assert store.with_entries(conn, ["e3"]) == []  # now an empty answer: entry dropped
    assert lance.count("codex") == 1


def test_disabled_stage_is_a_no_op(env) -> None:
    config, conn, tmp_path, _w = env
    config = config.model_copy(update={
        "codex": config.codex.model_copy(update={"enabled": False})})
    client = _Client(ANSWERS)
    stage, _lance = _stage(env, client)
    assert run_book(conn, config, BOOK, [stage]).state == jobs.DONE
    assert client.calls == 0 and store.count(conn, 1) == 0
    assert not config.codex_dir.exists()


def test_the_codex_switch_defaults_on_and_reads_from_toml(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert LoreConfig.load().codex.enabled is True
    (tmp_path / "lore.toml").write_text("[lore.codex]\nenabled = false\n")
    assert LoreConfig.load().codex.enabled is False


def test_stage_stops_at_the_answer_reserve_before_any_request(env) -> None:
    config, conn, _t, _w = env
    config = config.model_copy(update={
        "processing": config.processing.model_copy(update={"answer_reserve": 10**9})})
    client = _Client(ANSWERS)
    stage, _lance = _stage(env, client)
    job = run_book(conn, config, BOOK, [stage])
    assert job.state == jobs.WAITING_QUOTA and client.calls == 0


def test_an_unusable_answer_skips_the_entity_without_caching(env) -> None:
    config, conn, _t, _w = env
    client = _Client(ANSWERS, bad_on={1})
    stage, _lance = _stage(env, client)
    assert run_book(conn, config, BOOK, [stage]).state == jobs.DONE
    assert len(list(config.codex_dir.glob("*.json"))) == 2  # no answer cached for it (only a count of tries)
    assert "Mira Voss" in "".join(stage.skipped)


class _Stubborn(_Client):
    """Never gives usable output for the subjects in `bad_for`."""

    def __init__(self, answers, bad_for=()):
        super().__init__(answers)
        self.bad_for = tuple(bad_for)

    async def generate_json(self, prompt, schema, **kw):
        if any(f"Subject: {m}" in prompt for m in self.bad_for):
            self.calls += 1
            self.prompts.append(prompt)
            raise GeminiOutputError("not json", "")
        return await super().generate_json(prompt, schema, **kw)


def _asked(client, name):
    return sum(f"Subject: {name}" in p for p in client.prompts)


def test_an_entity_that_keeps_failing_is_cached_as_failed_after_two_attempts(env, monkeypatch) -> None:
    config, conn, _t, _w = env
    first = _Stubborn(ANSWERS, bad_for=["Mira Voss"])
    stage, _ = _stage(env, first)
    run_book(conn, config, BOOK, [stage], force=True)
    assert _asked(first, "Mira Voss") == 1 and "Mira Voss" in "".join(stage.skipped)
    second = _Stubborn(ANSWERS, bad_for=["Mira Voss"])
    stage2, _ = _stage(env, second)
    run_book(conn, config, BOOK, [stage2], force=True)
    assert _asked(second, "Mira Voss") == 1  # the second and last try
    marked = [json.loads(f.read_text()) for f in config.codex_dir.glob("*.json")
              if json.loads(f.read_text()).get("failed")]
    assert len(marked) == 1 and marked[0]["answer"] == {"summary": "", "statements": []}
    # From now on a Continue does not ask again, and the entity is counted done.
    third = _Stubborn(ANSWERS, bad_for=["Mira Voss"])
    stage3, _ = _stage(env, third)
    job = run_book(conn, config, BOOK, [stage3], force=True)
    assert third.calls == 0 and job.done_items == job.total_items
    assert store.with_entries(conn, ["e1"]) == []
    # A new prompt invalidates the marker.
    monkeypatch.setattr(build, "CODEX_PROMPT", build.CODEX_PROMPT + "\n- Be brief.")
    fourth = _Stubborn(ANSWERS, bad_for=["Mira Voss"])
    stage4, _ = _stage(env, fourth)
    run_book(conn, config, BOOK, [stage4], force=True)
    assert _asked(fourth, "Mira Voss") == 1


def test_a_success_after_a_failed_attempt_is_cached_normally(env) -> None:
    config, conn, _t, _w = env
    run_book(conn, config, BOOK, [_stage(env, _Client(ANSWERS, bad_on={1}))[0]], force=True)
    again = _Client(ANSWERS)
    run_book(conn, config, BOOK, [_stage(env, again)[0]], force=True)
    assert _asked(again, "Mira Voss") == 1
    assert store.with_entries(conn, ["e1"]) == ["e1"]
    assert not any(json.loads(f.read_text()).get("failed") for f in config.codex_dir.glob("*.json"))


def test_a_book_with_no_graph_has_nothing_to_do(env) -> None:
    config, conn, _t, _w = env
    client = _Client(ANSWERS)
    stage = CodexStage(client_factory=lambda cfg: client,
                       embed_factory=_embed_factory([]),
                       kuzu_factory=lambda cfg: None, lance_factory=lambda cfg: None)
    assert run_book(conn, config, BOOK, [stage]).state == jobs.DONE
    assert client.calls == 0


def test_evidence_stays_inside_the_world(env) -> None:
    """Another world's chunks mentioning the same entity are never shown."""
    config, conn, tmp_path, world = env
    other = worlds.create_world(conn, "Other World")
    with conn:
        conn.execute("INSERT INTO books (id, title, file_path, world_id) VALUES (?,?,?,?)",
                     (BOOK2, "Elsewhere", "x", other))
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?,?,1,0,0,?)", (f"{BOOK2}:1:0", BOOK2, "A stranger kept a secret."))
    k = KuzuStore(config.kuzu_dir / "graph")
    k.execute("CREATE (:Chunk {id:$i, book_id:$b, chapter_seq:1})", {"i": f"{BOOK2}:1:0", "b": BOOK2})
    k.execute("MATCH (c:Chunk {id:$c}), (e:Entity {id:'e1'}) CREATE (c)-[:MENTIONS]->(e)",
              {"c": f"{BOOK2}:1:0"})
    k.close()
    del k
    gc.collect()
    client = _Client(ANSWERS)
    stage, _ = _stage(env, client)
    run_book(conn, config, BOOK, [stage])
    assert all("A stranger kept a secret" not in p for p in client.prompts)


def test_evidence_pack_uses_beats_first_and_caps_them(env, monkeypatch) -> None:
    config, conn, tmp_path, _w = env
    beats = [Beat(id=beat_id(BOOK, ch, ("Mira Voss",), f"q{i}"), book_id=BOOK,
                  chapter_seq=ch, chunk_id=cid, entities=("Mira Voss",), kind="action",
                  text=f"moment {i}", quote=f"q{i}")
             for i, (ch, cid) in enumerate([(1, f"{BOOK}:1:0"), (1, f"{BOOK}:1:1"),
                                            (2, f"{BOOK}:2:0"), (3, f"{BOOK}:3:0")])]
    for b in beats:
        save_beats(conn, BOOK, b.chapter_seq, 0, [b]) if b.chunk_id != f"{BOOK}:1:1" else None
    save_beats(conn, BOOK, 1, 0, [beats[0], beats[1]])
    k = KuzuStore(config.kuzu_dir / "graph", read_only=True)
    relink_beats(conn, k)
    ev = build.evidence_pack(conn, k, MIRA, [BOOK], None)
    assert [b.text for b in ev.beats] == ["moment 0", "moment 1", "moment 2", "moment 3"]
    assert [p.chunk_id for p in ev.passages] == [
        f"{BOOK}:1:0", f"{BOOK}:1:1", f"{BOOK}:2:0", f"{BOOK}:3:0"]
    assert [p.n for p in ev.passages] == [1, 2, 3, 4]
    monkeypatch.setattr(build, "MAX_BEATS", 2)
    monkeypatch.setattr(build, "MAX_PASSAGES", 3)
    ev = build.evidence_pack(conn, k, MIRA, [BOOK], None)
    assert [b.text for b in ev.beats] == ["moment 0", "moment 3"]  # earliest and latest
    assert len(ev.passages) == 3 and ev.passages[0].chunk_id == f"{BOOK}:1:0"
    # nothing mentions an entity of no chunk: no passages
    ghost = CodexEntity(id="nobody", name="Nobody", kind="Character")
    assert build.evidence_pack(conn, k, ghost, [BOOK], None).passages == ()
    k.close()
