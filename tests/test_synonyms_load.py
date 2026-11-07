"""Synonym questions through the disambiguation store, the load and the
disambiguate stage (lore/resolve/synonyms.py). No network: answers are written
to the cache by hand, or by a fake client."""
from pathlib import Path

import pytest

from lore.graph.loader import load_book
from lore.orchestrator import jobs
from lore.orchestrator.pipeline import run_book
from lore.orchestrator.stages import DisambiguateStage
from lore.resolve.disambiguation import DisambiguationStore
from lore.resolve.synonyms import Side, SynonymQuestion
from tests.test_loader import _add_book, _chunk, _env, _never, _row, _rows

BOOK = "abcdef0123456789"


def _q(a: str, b: str, book: str = BOOK) -> SynonymQuestion:
    return SynonymQuestion(book, Side(f"id:{a}", a, "Faction", (f"{a} rode.",)),
                           Side(f"id:{b}", b, "Faction", (f"{b} watched.",)))


# --------------------------------------------------------------------------
# the store

def test_synonym_questions_are_recorded_per_book_and_answers_cached(tmp_path: Path) -> None:
    store = DisambiguationStore(tmp_path / "d")
    store.record_synonym(_q("Myrddraal", "Fade"))
    store.record_synonym(_q("Rand", "Tam"))
    store.flush(BOOK)
    again = DisambiguationStore(tmp_path / "d")
    pending = again.pending_synonyms()
    assert {frozenset({q.a.name, q.b.name}) for q in pending} == {
        frozenset({"Myrddraal", "Fade"}), frozenset({"Rand", "Tam"})}
    assert again.pending() == []          # the bare-name questions are apart
    again.save_synonym_answers({_q("Myrddraal", "Fade").key: "same"})
    assert DisambiguationStore(tmp_path / "d").synonym_verdict(_q("Fade", "Myrddraal")) == "same"
    assert again.synonym_verdict(_q("Rand", "Tam")) is None
    assert [q.b.name for q in again.unanswered_synonyms(pending)] == ["Tam"]
    # a re-load that asks nothing replaces the book's questions
    empty = DisambiguationStore(tmp_path / "d")
    empty.flush(BOOK)
    assert DisambiguationStore(tmp_path / "d").pending_synonyms() == []


def test_a_corrupt_synonym_file_reads_as_nothing(tmp_path: Path) -> None:
    store = DisambiguationStore(tmp_path / "d")
    store.synonym_pending_dir.mkdir(parents=True)
    (store.synonym_pending_dir / "x.json").write_text("{not json")
    store.synonym_answers_path.write_text("[1, 2")
    assert store.pending_synonyms() == []
    assert store.synonym_answers() == {}
    # an unknown verdict in the cache never merges
    store.save_synonym_answers({_q("A", "B").key: "maybe"})
    assert store.synonym_verdict(_q("A", "B")) is None


# --------------------------------------------------------------------------
# the load

def _book(config, conn):
    chunks = [_chunk(BOOK, 1, 0, "The Myrddraal rode after Rand."),
              _chunk(BOOK, 1, 1, "A Fade watched the farm."),
              _chunk(BOOK, 1, 2, "Ba'alzamon came into Rand's dream."),
              _chunk(BOOK, 1, 3, "The Dark One stirred in Shayol Ghul."),
              _chunk(BOOK, 2, 4, "Fade: See Myrddraal. Ba'alzamon (bah-AHL-zah-mon): "
                                 "Believed to be the Trolloc name for the Dark One.")]
    c = [ch.id for ch in chunks]
    rows = [_row(c[0], BOOK, "Myrddraal", "HUNTS", "Rand", ["Myrddraal", "Rand"],
                 ["Faction", "Character"], "The Myrddraal rode after Rand."),
            _row(c[1], BOOK, "Fade", "WATCHES", "Rand", ["Fade", "Rand"],
                 ["Faction", "Character"], "A Fade watched the farm."),
            _row(c[2], BOOK, "Ba'alzamon", "THREATENS", "Rand", ["Ba'alzamon", "Rand"],
                 ["Character", "Character"], "Ba'alzamon came into Rand's dream."),
            _row(c[3], BOOK, "The Dark One", "RULES", "Shayol Ghul",
                 ["The Dark One", "Shayol Ghul"], ["Character", "Place"],
                 "The Dark One stirred in Shayol Ghul.")]
    _add_book(config, conn, BOOK, chunks, rows)


async def _embed(texts):
    from lore.db.lance_store import EMBED_DIM
    return [[1.0] + [0.0] * (EMBED_DIM - 1) for _ in texts]


async def _load(config, conn, kuzu, lance, store):
    return await load_book(config, conn, kuzu, lance, BOOK, _embed, _never,
                           disambiguation=store)


def _entities(kuzu) -> dict[str, str]:
    return {name: eid for eid, name in _rows(kuzu, "MATCH (e:Entity) RETURN e.id, e.name")}


@pytest.mark.asyncio
async def test_a_load_records_synonym_questions_and_merges_nothing_unanswered(
        tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    _book(config, conn)
    store = DisambiguationStore(config.disambiguation_dir)
    await _load(config, conn, kuzu, lance, store)
    pending = DisambiguationStore(config.disambiguation_dir).pending_synonyms()
    pairs = {frozenset({q.a.name, q.b.name}) for q in pending}
    assert frozenset({"Myrddraal", "Fade"}) in pairs
    assert frozenset({"Ba'alzamon", "The Dark One"}) in pairs
    names = _entities(kuzu)
    assert {"Myrddraal", "Fade", "Ba'alzamon", "The Dark One"} <= set(names)


@pytest.mark.asyncio
async def test_answers_merge_same_and_link_claimed(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    _book(config, conn)
    store = DisambiguationStore(config.disambiguation_dir)
    await _load(config, conn, kuzu, lance, store)
    verdicts = {frozenset({"Myrddraal", "Fade"}): "same",
                frozenset({"Ba'alzamon", "The Dark One"}): "claimed"}
    answers = {q.key: verdicts.get(frozenset({q.a.name, q.b.name}), "different")
               for q in store.pending_synonyms()}
    store.save_synonym_answers(answers)
    await _load(config, conn, kuzu, lance, DisambiguationStore(config.disambiguation_dir))

    names = _entities(kuzu)
    # one entity; the other name survives only as an alias node pointing at it
    canonicals = {name for (name,) in _rows(
        kuzu, "MATCH (e:Entity) WHERE NOT (e)-[:ALIAS_OF]->(:Entity) RETURN e.name")}
    merged = [n for n in ("Myrddraal", "Fade") if n in canonicals]
    assert len(merged) == 1, canonicals
    (canonical,) = merged
    other = ({"Myrddraal", "Fade"} - {canonical}).pop()
    if other in names:
        assert _rows(kuzu, f"MATCH (a:Entity {{name: '{other}'}})-[:ALIAS_OF]->(b:Entity)"
                           " RETURN b.name") == [(canonical,)]
    # both creatures' edges hang on the one node
    hunted = _rows(kuzu, "MATCH (a:Entity)-[r:REL]->(b:Entity {name: 'Rand'})"
                         " RETURN a.name, r.predicate_raw ORDER BY r.predicate_raw")
    assert {(canonical, "HUNTS"), (canonical, "WATCHES")} <= set(hunted)
    aliases = _rows(kuzu, f"MATCH (e:Entity {{id: '{names[canonical]}'}}) RETURN e.aliases")
    assert {"myrddraal", "fade"} <= set(aliases[0][0])
    # a claim is a link with its sentence, never a merge
    assert "Ba'alzamon" in names and "The Dark One" in names
    link = _rows(kuzu, "MATCH (a:Entity)-[r:REL {predicate: 'IDENTIFIED_AS'}]->(b:Entity)"
                       " RETURN a.name, b.name, r.evidence, r.chunk_id")
    assert len(link) == 1
    assert {link[0][0], link[0][1]} == {"Ba'alzamon", "The Dark One"}
    assert "Trolloc name for the Dark One" in link[0][2]
    assert link[0][3] == f"{BOOK}:2:4", "the linking sentence's own chunk"


@pytest.mark.asyncio
async def test_with_the_resolver_switch_off_a_load_asks_and_merges_nothing(
        tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    _book(config, conn)
    store = DisambiguationStore(config.disambiguation_dir)
    await _load(config, conn, kuzu, lance, store)
    store.save_synonym_answers({q.key: "same" for q in store.pending_synonyms()})
    off = config.model_copy(update={
        "resolver": config.resolver.model_copy(update={"synonyms": False})})
    await _load(off, conn, kuzu, lance, DisambiguationStore(config.disambiguation_dir))
    assert DisambiguationStore(config.disambiguation_dir).pending_synonyms() == []
    assert {"Myrddraal", "Fade"} <= set(_entities(kuzu))


@pytest.mark.asyncio
async def test_without_a_store_a_load_writes_no_questions(tmp_path: Path) -> None:
    config, conn, kuzu, lance = _env(tmp_path)
    _book(config, conn)
    await _load(config, conn, kuzu, lance, None)
    assert not config.disambiguation_dir.exists()


def test_a_claim_read_in_back_matter_hangs_on_a_story_chunk(tmp_path: Path) -> None:
    from lore.graph.loader import IDENTIFIED_AS, _apply_synonyms
    from lore.resolve.normalize import entity_id
    from lore.resolve.resolver import CanonicalEntity, ResolutionResult

    b, d = entity_id("ba alzamon", "Character"), entity_id("dark one", "Character")
    resolution = ResolutionResult(
        entities=[CanonicalEntity(b, "Ba'alzamon", "Character", ("ba alzamon",), (BOOK,)),
                  CanonicalEntity(d, "The Dark One", "Character", ("dark one",), (BOOK,))],
        mention_map={("Ba'alzamon", "Character", BOOK): b,
                     ("The Dark One", "Character", BOOK): d})
    triplets = [
        _row(f"{BOOK}:1:{i}", BOOK, "Ba'alzamon", "SERVES", "The Dark One",
             ["Ba'alzamon", "The Dark One"], ["Character", "Character"], f"dream {i}")
        for i in (3, 1)]
    glossary = (f"{BOOK}:9:1", "Ba'alzamon (bah-AHL-zah-mon): Believed to be the Trolloc "
                               "name for the Dark One.")
    store = DisambiguationStore(tmp_path / "d")
    _apply_synonyms(BOOK, triplets, resolution, [glossary], {}, store, {f"{BOOK}:1:1",
                                                                       f"{BOOK}:1:3"})
    (question,) = store._synonyms[BOOK].values()
    store.save_synonym_answers({question.key: "claimed"})
    merged, rows = _apply_synonyms(BOOK, triplets, resolution, [glossary], {}, store,
                                   {f"{BOOK}:1:1", f"{BOOK}:1:3"})
    assert merged is resolution
    assert [(r["subject"], r["predicate"], r["object"], r["chunk_id"]) for r in rows] == [
        ("Ba'alzamon", IDENTIFIED_AS, "The Dark One", f"{BOOK}:1:1")]
    assert "Trolloc name for the Dark One" in rows[0]["evidence"]


# --------------------------------------------------------------------------
# the disambiguate stage

class _Client:
    def __init__(self):
        self.prompts = []
        self.closed = False

    async def generate(self, prompt: str) -> str:
        import re
        self.prompts.append(prompt)
        lines = re.findall(r"^(\d+)\. (.+?) \(\w+\) / (.+?) \(\w+\)$", prompt, re.MULTILINE)
        return "\n".join(f"{n}. {a} / {b} -> same" for n, a, b in lines)

    async def aclose(self):
        self.closed = True


@pytest.fixture
def stage_env(tmp_path: Path, monkeypatch):
    from lore.config import LoreConfig
    from lore.db.sqlite import open_db
    from tests.fixtures import seed_book
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    seed_book(BOOK)
    config = LoreConfig.load()
    config = config.model_copy(update={
        "disambiguation": config.disambiguation.model_copy(update={"enabled": True})})
    conn = open_db(config.sqlite_path)
    yield config, conn
    conn.close()


def test_the_disambiguate_stage_asks_synonym_questions_and_reloads(stage_env) -> None:
    config, conn = stage_env
    store = DisambiguationStore(config.disambiguation_dir)
    for i in range(3):
        store.record_synonym(_q(f"A{i}", f"B{i}"))
    store.flush(BOOK)
    client, reloads = _Client(), []
    job = run_book(conn, config, BOOK, [DisambiguateStage(lambda cfg: client,
                                                          reload=reloads.append)])
    assert job.state == jobs.DONE, job.stopped_reason
    assert len(client.prompts) == 1 and client.closed
    store = DisambiguationStore(config.disambiguation_dir)
    assert store.unanswered_synonyms(store.pending_synonyms()) == []
    assert len(reloads) == 1
    # answered: a second run asks nothing
    again = _Client()
    run_book(conn, config, BOOK, [DisambiguateStage(lambda cfg: again)], force=True)
    assert again.prompts == []
