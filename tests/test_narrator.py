"""Task 10c: first-person narration resolved to the book's narrator at load."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import EMBED_DIM, LanceStore
from lore.db.sqlite import open_db
from lore.graph.adjudicate import make_gemini_narrator
from lore.graph.loader import load_book
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book
from lore.inference.gemini import LITE_MODELS, GeminiClient
from lore.orchestrator.run import set_unit_state
from lore.resolve.narrator import (
    NarratorAnswer,
    NarratorCache,
    is_narrator_reference,
    opening_passages,
    render_prompt,
    rewrite_narrator,
    validated_narrator,
)

SECRET = "AIza-not-a-real-key-0000"


# --- what counts as a narrator reference ------------------------------------

@pytest.mark.parametrize("name", [
    "I", "me", "My", "myself", "We", "us", "our", "Ourselves",
    "narrator", "Narrator", "the narrator", "The Narrator", "  I ", "\tthe narrator\n",
])
def test_first_person_pronouns_and_narrator_references_match(name) -> None:
    assert is_narrator_reference(name)


@pytest.mark.parametrize("name", [
    "Ivan", "Irene Adler", "I. Watson", "Mr. I", "Isaac", "Iowa",
    "the narrator's friend", "Narrator Jones", "an unreliable narrator",
    "Meredith", "Usher", "Our Mutual Friend", "We the People", "", "  ",
    "he", "You", "they",
])
def test_a_longer_name_or_another_pronoun_is_never_a_narrator_reference(name) -> None:
    assert not is_narrator_reference(name)


# --- rewriting triplet rows -----------------------------------------------

def _row(subject, obj, names, kinds=None, **extra):
    row = {
        "chunk_id": "b:1:0", "subject": subject, "predicate": "MEMBER_OF",
        "object": obj, "evidence": "I was attached to the regiment.",
        "predicate_raw": "MEMBER_OF", "confidence": 0.9,
        "entity_names": list(names),
        "entity_kinds": list(kinds) if kinds is not None else ["Character"] * len(names),
    }
    row.update(extra)
    return row


def test_rewrite_replaces_whole_endpoints_and_their_mentions() -> None:
    rows = [
        _row("I", "Fifth Fusiliers", ["I", "Fifth Fusiliers"], ["Character", "Faction"]),
        _row("Stamford", "the narrator", ["Stamford", "the narrator"]),
        _row("Narrator", "Jezail Bullet", ["Narrator", "Jezail Bullet"],
             ["Character", "Object"]),
    ]
    rewritten, count = rewrite_narrator(rows, "John Watson")
    assert count == 3
    assert [(r["subject"], r["object"]) for r in rewritten] == [
        ("John Watson", "Fifth Fusiliers"),
        ("Stamford", "John Watson"),
        ("John Watson", "Jezail Bullet"),
    ]
    assert rewritten[0]["entity_names"] == ["John Watson", "Fifth Fusiliers"]
    assert rewritten[0]["entity_kinds"] == ["Character", "Faction"]
    assert rewritten[1]["entity_names"] == ["Stamford", "John Watson"]
    # Only the endpoint name changes: predicate and evidence are untouched.
    for before, after in zip(rows, rewritten):
        for field in ("predicate", "predicate_raw", "evidence", "chunk_id", "confidence"):
            assert after[field] == before[field]
    # The input rows are not mutated.
    assert rows[0]["subject"] == "I" and rows[0]["entity_names"][0] == "I"


def test_rewrite_adds_the_narrator_as_a_mention_when_the_endpoint_was_not_listed() -> None:
    # 34 of 327 real narrator endpoints are missing from entity_names; without
    # a mention the loader has no surface form to hang the edge on.
    rows = [_row("I", "Murray", ["Murray"])]
    rewritten, count = rewrite_narrator(rows, "John Watson")
    assert count == 1
    assert rewritten[0]["entity_names"] == ["Murray", "John Watson"]
    assert rewritten[0]["entity_kinds"] == ["Character", "Character"]


def test_rewrite_merges_duplicate_mentions_and_leaves_longer_names_alone() -> None:
    rows = [
        _row("I", "me", ["I", "me", "Irene Adler"]),
        _row("Irene Adler", "the narrator's friend", ["Irene Adler", "the narrator's friend"]),
    ]
    rewritten, count = rewrite_narrator(rows, "John Watson")
    assert count == 2
    assert rewritten[0]["entity_names"] == ["John Watson", "Irene Adler"]
    assert rewritten[1] == rows[1]


def test_a_mention_rewritten_to_the_narrator_is_a_character() -> None:
    rows = [_row("we", "Baker Street", ["we", "Baker Street"], ["Faction", "Place"])]
    rewritten, _count = rewrite_narrator(rows, "John Watson")
    assert rewritten[0]["entity_kinds"] == ["Character", "Place"]


# --- choosing and validating the answer -----------------------------------

def _chunk(book, chapter, seq, text):
    return Chunk(id=f"{book}:{chapter}:{seq}", book_id=book, chapter_seq=chapter,
                 scene_seq=0, seq=seq, text=text)


def test_opening_passages_skip_boilerplate_and_stop_at_the_budget() -> None:
    chunks = [
        _chunk("b", 1, 0, "The Project Gutenberg eBook of Something. " * 3),
        _chunk("b", 2, 1, "y" * 60),
        _chunk("b", 2, 0, "x" * 60),
        _chunk("b", 3, 0, "z" * 60),
    ]
    passages = opening_passages(chunks, budget=130)
    assert [p.id for p in passages] == ["b:2:0", "b:2:1"]
    # A single over-budget chunk is still offered rather than nothing.
    assert [p.id for p in opening_passages(chunks, budget=10)] == ["b:2:0"]


def test_a_name_that_is_not_in_the_supplied_text_is_unknown() -> None:
    text = "In the year 1878 I took my degree. “You are thin, Watson,” he said."
    assert validated_narrator(NarratorAnswer(first_person=True, narrator_name="Watson"),
                              text) == "Watson"
    assert validated_narrator(
        NarratorAnswer(first_person=True, narrator_name="John H. Watson"), text) is None
    assert validated_narrator(
        NarratorAnswer(first_person=False, narrator_name="Watson"), text) is None
    assert validated_narrator(
        NarratorAnswer(first_person=True, narrator_name=""), text) is None
    # A narrator reference is not a name, even when it appears in the text.
    assert validated_narrator(
        NarratorAnswer(first_person=True, narrator_name="I"), text) is None


# --- the Gemini detector and its cache --------------------------------------

def _gemini(handler) -> GeminiClient:
    async def no_sleep(_s: float) -> None:
        return None

    return GeminiClient(SECRET, models=list(LITE_MODELS),
                        transport=httpx.MockTransport(handler), sleep=no_sleep)


def _answering(by_marker: dict[str, dict], seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        prompt = body["contents"][0]["parts"][0]["text"]
        reply = next(value for marker, value in by_marker.items() if marker in prompt)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]})
    return handler


def _detector(tmp_path, handler):
    client = _gemini(handler)
    cache = NarratorCache(tmp_path / "narrators")
    detect, aclose = make_gemini_narrator(LoreConfig.load(), client=client, cache=cache)
    return client, cache, detect, aclose


SIS = [_chunk("sis", 5, 0, "In the year 1878 I took my degree. “Well, Watson?” said Stamford.")]
SOF = [_chunk("sof", 4, 0, "“Miss Morstan,” I said, “you are welcome.” I am Mary's friend.")]
THIRD = [_chunk("p", 1, 0, "It is a truth universally acknowledged, said Elizabeth.")]


async def test_the_detector_asks_once_and_a_reload_spends_zero_requests(tmp_path) -> None:
    seen: list = []
    replies = {"Stamford": {"first_person": True, "narrator_name": "Watson"}}
    client, cache, detect, aclose = _detector(tmp_path, _answering(replies, seen))
    try:
        assert await detect("sis", SIS) == "Watson"
        assert await detect("sis", SIS) == "Watson"
    finally:
        await aclose()
    assert client.requests_made == 1
    assert seen[0]["generationConfig"]["temperature"] == 0
    assert seen[0]["generationConfig"]["responseMimeType"] == "application/json"
    assert render_prompt(opening_passages(SIS)) in seen[0]["contents"][0]["parts"][0]["text"]
    stored = json.loads(cache.path.read_text())
    (key, entry), = stored.items()
    assert key.startswith("sis:")
    assert entry == {"first_person": True, "narrator_name": "Watson",
                     "model": LITE_MODELS[0]}
    assert SECRET not in cache.path.read_text()

    def must_not_call(request):  # pragma: no cover
        raise AssertionError("a cached narrator question was asked again")

    client2, _cache2, detect2, aclose2 = _detector(tmp_path, must_not_call)
    try:
        assert await detect2("sis", SIS) == "Watson"
    finally:
        await aclose2()
    assert client2.requests_made == 0


async def test_changed_text_changes_the_key(tmp_path) -> None:
    seen: list = []
    replies = {"Stamford": {"first_person": True, "narrator_name": "Watson"}}
    client, cache, detect, aclose = _detector(tmp_path, _answering(replies, seen))
    changed = [_chunk("sis", 5, 0, SIS[0].text + " Stamford laughed.")]
    try:
        await detect("sis", SIS)
        await detect("sis", changed)
    finally:
        await aclose()
    assert client.requests_made == 2
    assert len(json.loads(cache.path.read_text())) == 2
    assert NarratorCache.key("sis", render_prompt(SIS)) != NarratorCache.key(
        "sis", render_prompt(changed))
    assert NarratorCache.key("sis", render_prompt(SIS)) != NarratorCache.key(
        "sof", render_prompt(SIS))


async def test_two_books_get_their_own_narrators_and_unknowns_stay_unknown(tmp_path) -> None:
    seen: list = []
    replies = {
        "Stamford": {"first_person": True, "narrator_name": "Watson"},
        "Morstan": {"first_person": True, "narrator_name": "Mary"},
        "Elizabeth": {"first_person": False, "narrator_name": ""},
    }
    client, _cache, detect, aclose = _detector(tmp_path, _answering(replies, seen))
    try:
        assert await detect("sis", SIS) == "Watson"
        assert await detect("sof", SOF) == "Mary"
        assert await detect("p", THIRD) is None
    finally:
        await aclose()
    assert client.requests_made == 3


async def test_a_hallucinated_narrator_is_unknown(tmp_path) -> None:
    seen: list = []
    replies = {"Stamford": {"first_person": True, "narrator_name": "Dr. John Watson"}}
    _client, _cache, detect, aclose = _detector(tmp_path, _answering(replies, seen))
    try:
        assert await detect("sis", SIS) is None
    finally:
        await aclose()


def test_the_gemini_detector_refuses_without_a_key(tmp_path, monkeypatch) -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("lore.graph.adjudicate.default_key_file",
                        lambda: tmp_path / "absent.env")
    with pytest.raises(ValueError, match="no API key"):
        make_gemini_narrator(LoreConfig.load(), cache=NarratorCache(tmp_path))


# --- the loader ---------------------------------------------------------------

BOOK = "abcdef0123456789"
OTHER_BOOK = "9876543210fedcba"


async def _embed(texts):
    return [[0.0] * EMBED_DIM for _ in texts]


async def _never(canonical, mention) -> bool:
    return False


def _write_book(config, conn, book_id, rows):
    chunk_ids = sorted({row["chunk_id"] for row in rows})
    chunks = [
        Chunk(id=cid, book_id=book_id, chapter_seq=1, scene_seq=0, seq=i,
              text=f"passage {i}")
        for i, cid in enumerate(chunk_ids)
    ]
    ingest_book(conn, book_id=book_id, title="T", author="A", source="upload",
                file_path=f"/x/{book_id}.epub", chunks=chunks)
    unit = f"{book_id}-u0000-aaaa"
    d = config.extractions_dir / book_id / unit
    d.mkdir(parents=True, exist_ok=True)
    columns = ("chunk_id", "subject", "predicate", "object", "evidence",
               "entity_names", "entity_kinds")
    table = {name: [row[name] for row in rows] for name in columns}
    table.update({
        "book_id": [book_id] * len(rows), "confidence": [0.9] * len(rows),
        "extractor_model": ["m"] * len(rows), "run_id": [unit] * len(rows),
    })
    pq.write_table(pa.table(table), d / "triplets.parquet")
    pq.write_table(pa.table({
        "chunk_id": chunk_ids, "vector": [[0.0] * EMBED_DIM for _ in chunk_ids],
        "embed_model": ["e"] * len(chunk_ids),
    }), d / "embeddings.parquet")
    set_unit_state(conn, book_id, unit, "completed")


def _t(chunk, subject, predicate, obj, names, kinds=None):
    return {"chunk_id": chunk, "subject": subject, "predicate": predicate,
            "object": obj, "evidence": f"{subject} {predicate} {obj}",
            "entity_names": list(names),
            "entity_kinds": list(kinds) if kinds else ["Character"] * len(names)}


def _narrated_rows(book):
    return [
        _t(f"{book}:1:0", "John Watson", "KNOWS", "Stamford", ["John Watson", "Stamford"]),
        _t(f"{book}:1:1", "I", "MEMBER_OF", "Fifth Fusiliers", ["I", "Fifth Fusiliers"],
           ["Character", "Faction"]),
        _t(f"{book}:1:2", "Narrator", "POSSESSES", "Jezail Bullet",
           ["Narrator", "Jezail Bullet"], ["Character", "Object"]),
        _t(f"{book}:1:3", "Murray", "SAVES", "the narrator", ["Murray"]),
        _t(f"{book}:1:4", "Irene Adler", "KNOWS", "Stamford", ["Irene Adler", "Stamford"]),
    ]


def _stores(tmp_path):
    config = LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    kuzu = KuzuStore(config.kuzu_dir / "graph")
    lance = LanceStore(config.lancedb_dir)
    return config, conn, kuzu, lance


def _fixed_narrators(answers: dict[str, str | None], calls: list):
    async def detect(book_id, chunks):
        calls.append((book_id, [chunk.id for chunk in chunks]))
        return answers.get(book_id)
    return detect


def _edges(kuzu):
    return sorted(
        tuple(r) for r in kuzu.execute(
            "MATCH (a:Entity)-[r:REL]->(b:Entity) RETURN a.name, r.predicate_raw, b.name,"
            " r.evidence"
        )
    )


def _names(kuzu):
    return sorted(r[0] for r in kuzu.execute("MATCH (e:Entity) RETURN e.name"))


async def test_narrated_edges_survive_on_the_narrator_entity(tmp_path) -> None:
    config, conn, kuzu, lance = _stores(tmp_path)
    _write_book(config, conn, BOOK, _narrated_rows(BOOK))
    calls: list = []
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never,
                             narrator=_fixed_narrators({BOOK: "John Watson"}, calls))
    assert calls and calls[0][0] == BOOK
    assert report.narrator == "John Watson"
    assert report.narrator_rewrites == 3
    assert report.dropped_edges == 0
    edges = _edges(kuzu)
    assert ("John Watson", "MEMBER_OF", "Fifth Fusiliers",
            "I MEMBER_OF Fifth Fusiliers") in edges
    assert ("John Watson", "POSSESSES", "Jezail Bullet",
            "Narrator POSSESSES Jezail Bullet") in edges
    assert ("Murray", "SAVES", "John Watson", "Murray SAVES the narrator") in edges
    assert ("Irene Adler", "KNOWS", "Stamford", "Irene Adler KNOWS Stamford") in edges
    assert "Narrator" not in _names(kuzu)
    assert "I" not in _names(kuzu)
    watson_edges = [e for e in edges if "John Watson" in (e[0], e[2])]
    assert len(watson_edges) == 4
    conn.close(); kuzu.close()


async def test_a_book_without_a_narrator_loads_exactly_as_before(tmp_path) -> None:
    config, conn, kuzu, lance = _stores(tmp_path)
    _write_book(config, conn, BOOK, _narrated_rows(BOOK))
    baseline = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    baseline_edges, baseline_names = _edges(kuzu), _names(kuzu)
    # Today's behaviour, pinned: "I" and "the narrator" edges are dropped and
    # the capitalised "Narrator" survives as its own node.
    assert baseline.dropped_edges == 2
    assert "Narrator" in baseline_names
    assert (baseline.narrator, baseline.narrator_rewrites) == ("", 0)

    calls: list = []
    unknown = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never,
                              narrator=_fixed_narrators({}, calls))
    assert calls
    assert (unknown.narrator, unknown.narrator_rewrites) == ("", 0)
    assert unknown.narrator_checked and not baseline.narrator_checked
    assert (unknown.rel_edges, unknown.dropped_edges, unknown.entities) == (
        baseline.rel_edges, baseline.dropped_edges, baseline.entities)
    assert _edges(kuzu) == baseline_edges and _names(kuzu) == baseline_names
    conn.close(); kuzu.close()


async def test_two_books_each_get_their_own_narrator(tmp_path) -> None:
    config, conn, kuzu, lance = _stores(tmp_path)
    _write_book(config, conn, BOOK, [
        _t(f"{BOOK}:1:0", "I", "VISITS", "Rome", ["I", "Rome"], ["Character", "Place"]),
    ])
    _write_book(config, conn, OTHER_BOOK, [
        _t(f"{OTHER_BOOK}:1:0", "I", "VISITS", "Paris", ["I", "Paris"],
           ["Character", "Place"]),
    ])
    detect = _fixed_narrators({BOOK: "Anna Vale", OTHER_BOOK: "Tomas Reed"}, [])
    first = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never, narrator=detect)
    second = await load_book(config, conn, kuzu, lance, OTHER_BOOK, _embed, _never,
                             narrator=detect)
    assert (first.narrator, second.narrator) == ("Anna Vale", "Tomas Reed")
    assert [(e[0], e[2]) for e in _edges(kuzu)] == [
        ("Anna Vale", "Rome"), ("Tomas Reed", "Paris")]
    conn.close(); kuzu.close()


async def test_a_reload_with_the_narrator_is_idempotent(tmp_path) -> None:
    config, conn, kuzu, lance = _stores(tmp_path)
    _write_book(config, conn, BOOK, _narrated_rows(BOOK))
    detect = _fixed_narrators({BOOK: "John Watson"}, [])
    first = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never, narrator=detect)
    edges = _edges(kuzu)
    second = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never, narrator=detect)
    assert (second.rel_edges, second.entities, second.narrator_rewrites) == (
        first.rel_edges, first.entities, first.narrator_rewrites)
    assert _edges(kuzu) == edges
    conn.close(); kuzu.close()


# --- the CLI ------------------------------------------------------------------

def test_load_book_sync_wires_the_gemini_narrator(tmp_path, monkeypatch) -> None:
    import lore.cli as cli
    from lore.graph.loader import LoadReport

    config = LoreConfig.load(tmp_path / "absent.toml").model_copy(
        update={"data_dir": tmp_path / "data"})
    config = config.model_copy(update={
        "adjudication": config.adjudication.model_copy(update={"provider": "gemini"})})
    seen: dict = {}

    def fake_pair(cfg, api_key=None, **kwargs):
        async def aclose():
            seen["pair_closed"] = True
        return "EMBED", "ADJ", aclose

    def fake_narrator(cfg, api_key=None, **kwargs):
        seen["narrator_key"] = api_key

        async def aclose():
            seen["narrator_closed"] = True
        return "DETECT", aclose

    async def fake_load(*args, **kwargs):
        seen["narrator"] = kwargs.get("narrator")
        return LoadReport(book_id="b1")

    monkeypatch.setattr(cli, "make_gemini_pair", fake_pair)
    monkeypatch.setattr(cli, "make_gemini_narrator", fake_narrator)
    monkeypatch.setattr(cli, "load_book", fake_load)
    monkeypatch.setattr(cli, "KuzuStore", lambda path: type("K", (), {"close": lambda s: None})())
    monkeypatch.setattr(cli, "LanceStore", lambda path: None)
    cli._load_book_sync(config, None, "b1", api_key="k")
    assert seen == {"narrator_key": "k", "narrator": "DETECT",
                    "narrator_closed": True, "pair_closed": True}


def test_load_book_sync_skips_the_narrator_on_ollama(tmp_path, monkeypatch) -> None:
    import lore.cli as cli
    from lore.graph.loader import LoadReport

    config = LoreConfig.load(tmp_path / "absent.toml").model_copy(
        update={"data_dir": tmp_path / "data"})
    seen: dict = {}

    async def aclose():
        return None

    def no_narrator(*args, **kwargs):  # pragma: no cover
        raise AssertionError("narrator detection is Gemini-only")

    async def fake_load(*args, **kwargs):
        seen["narrator"] = kwargs.get("narrator", "absent")
        return LoadReport(book_id="b1")

    monkeypatch.setattr(cli, "make_ollama_pair", lambda cfg: (None, None, aclose))
    monkeypatch.setattr(cli, "make_gemini_narrator", no_narrator)
    monkeypatch.setattr(cli, "load_book", fake_load)
    monkeypatch.setattr(cli, "KuzuStore", lambda path: type("K", (), {"close": lambda s: None})())
    monkeypatch.setattr(cli, "LanceStore", lambda path: None)
    cli._load_book_sync(config, None, "b1")
    assert seen == {"narrator": None}


@pytest.mark.parametrize("fields, provider, expected, absent", [
    ({"narrator": "John Watson", "narrator_rewrites": 147, "narrator_checked": True},
     "gemini", "147 endpoint(s) rewritten to the narrator, John Watson", "skipped"),
    ({"narrator_checked": True}, "gemini", "no named first-person narrator", "endpoint(s) rewritten"),
    ({}, "ollama", "narrator resolution skipped", "endpoint(s) rewritten"),
])
def test_process_reports_the_narrator(tmp_path: Path, monkeypatch, fields, provider,
                                      expected, absent) -> None:
    from unittest.mock import patch

    from typer.testing import CliRunner

    from lore.cli import app
    from lore.graph.loader import LoadReport

    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "lore.toml").write_text(
        f'[lore]\nextraction_provider = "ollama"\n[lore.adjudication]\nprovider = "{provider}"\n')
    report = LoadReport(book_id=BOOK, entities=1, rel_edges=1, chunks=1, **fields)
    with (
        patch("lore.cli._graph_build_key", return_value=None),
        patch("lore.cli._load_book_sync", return_value=report),
    ):
        result = CliRunner().invoke(app, ["process", BOOK, "--skip-extract"])
    assert result.exit_code == 0, result.output
    assert expected in result.output
    assert absent not in result.output
