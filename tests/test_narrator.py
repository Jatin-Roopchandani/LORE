"""Task 10c: first-person endpoints resolved to their speaker, per passage."""
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
from lore.graph.adjudicate import make_gemini_first_person
from lore.graph.loader import load_book
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book
from lore.inference.gemini import LITE_MODELS, GeminiClient
from lore.orchestrator.run import set_unit_state
from lore.resolve.narrator import (
    NarratorAnswer,
    NarratorCache,
    SpeakerAnswers,
    SpeakerCache,
    SpeakerItem,
    SpeakerResolution,
    first_person_sentences,
    is_narrator_reference,
    make_first_person_resolver,
    opening_passages,
    render_prompt,
    render_speaker_prompt,
    rewrite_first_person,
    speaker_context,
    validated_narrator,
    validated_speakers,
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

def _row(subject, obj, names, kinds=None, chunk="b:1:0",
         evidence="I was attached to the regiment."):
    return {
        "chunk_id": chunk, "subject": subject, "predicate": "MEMBER_OF",
        "object": obj, "evidence": evidence,
        "predicate_raw": "MEMBER_OF", "confidence": 0.9,
        "entity_names": list(names),
        "entity_kinds": list(kinds) if kinds is not None else ["Character"] * len(names),
    }


def _always(name):
    return lambda row: name


def test_rewrite_replaces_whole_endpoints_and_their_mentions() -> None:
    rows = [
        _row("I", "Fifth Fusiliers", ["I", "Fifth Fusiliers"], ["Character", "Faction"]),
        _row("Stamford", "the narrator", ["Stamford", "the narrator"]),
        _row("Narrator", "Jezail Bullet", ["Narrator", "Jezail Bullet"],
             ["Character", "Object"]),
    ]
    rewritten, count, unresolved = rewrite_first_person(rows, _always("John Watson"))
    assert (count, unresolved) == (3, 0)
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


def test_rewrite_adds_the_speaker_as_a_mention_when_the_endpoint_was_not_listed() -> None:
    rows = [_row("I", "Murray", ["Murray"])]
    rewritten, count, _unresolved = rewrite_first_person(rows, _always("John Watson"))
    assert count == 1
    assert rewritten[0]["entity_names"] == ["Murray", "John Watson"]
    assert rewritten[0]["entity_kinds"] == ["Character", "Character"]


def test_rewrite_merges_duplicate_mentions_and_leaves_longer_names_alone() -> None:
    rows = [
        _row("I", "me", ["I", "me", "Irene Adler"]),
        _row("Irene Adler", "the narrator's friend", ["Irene Adler", "the narrator's friend"]),
    ]
    rewritten, count, _unresolved = rewrite_first_person(rows, _always("John Watson"))
    assert count == 2
    assert rewritten[0]["entity_names"] == ["John Watson", "Irene Adler"]
    assert rewritten[1] == rows[1]


def test_a_mention_rewritten_to_the_speaker_is_a_character() -> None:
    rows = [_row("we", "Baker Street", ["we", "Baker Street"], ["Faction", "Place"])]
    rewritten, _count, _unresolved = rewrite_first_person(rows, _always("John Watson"))
    assert rewritten[0]["entity_kinds"] == ["Character", "Place"]


def test_each_row_gets_its_own_speaker_and_unresolved_rows_are_left_alone() -> None:
    rows = [
        _row("I", "Drebber", ["I", "Drebber"], evidence="I killed him."),
        _row("I", "Holmes", ["I", "Holmes"], evidence="I said to Holmes."),
        _row("the narrator", "Lucy", ["the narrator", "Lucy"], evidence="I loved her."),
    ]
    speakers = {"I killed him.": "Jefferson Hope", "I said to Holmes.": "Watson"}
    rewritten, count, unresolved = rewrite_first_person(
        rows, lambda row: speakers.get(row["evidence"]))
    assert (count, unresolved) == (2, 1)
    assert [r["subject"] for r in rewritten] == ["Jefferson Hope", "Watson", "the narrator"]
    assert rewritten[2] == rows[2]


# --- the book-level hint ------------------------------------------------------

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
    assert [p.id for p in opening_passages(chunks, budget=10)] == ["b:2:0"]


def test_a_narrator_name_that_is_not_in_the_supplied_text_is_unknown() -> None:
    text = "In the year 1878 I took my degree. “You are thin, Watson,” he said."
    assert validated_narrator(NarratorAnswer(first_person=True, narrator_name="Watson"),
                              text) == "Watson"
    assert validated_narrator(
        NarratorAnswer(first_person=True, narrator_name="John H. Watson"), text) is None
    assert validated_narrator(
        NarratorAnswer(first_person=False, narrator_name="Watson"), text) is None
    assert validated_narrator(
        NarratorAnswer(first_person=True, narrator_name=""), text) is None
    assert validated_narrator(
        NarratorAnswer(first_person=True, narrator_name="I"), text) is None


# --- per-passage speaker questions --------------------------------------------

def test_first_person_sentences_are_grouped_per_chunk_unique_and_sorted() -> None:
    rows = [
        _row("I", "B", ["I", "B"], chunk="k:1:1", evidence="Zed I went."),
        _row("A", "me", ["A", "me"], chunk="k:1:1", evidence="Alpha saw me."),
        _row("I", "C", ["I", "C"], chunk="k:1:1", evidence="Zed I went."),
        _row("Holmes", "Watson", ["Holmes", "Watson"], chunk="k:1:2", evidence="x"),
        _row("we", "D", ["we", "D"], chunk="k:2:0", evidence="We ran."),
    ]
    assert first_person_sentences(rows) == {
        "k:1:1": ["Alpha saw me.", "Zed I went."],
        "k:2:0": ["We ran."],
    }


def test_speaker_context_is_chapter_opening_previous_and_the_passage() -> None:
    chunks = [_chunk("k", 1, i, f"text {i}") for i in range(5)] + [
        _chunk("k", 2, 0, "other chapter")]
    labels = lambda cid: [(label, c.id) for label, c in speaker_context(chunks, cid)]
    assert labels("k:1:3") == [("Chapter opening", "k:1:0"), ("Previous passage", "k:1:2"),
                               ("Passage", "k:1:3")]
    assert labels("k:1:1") == [("Previous passage", "k:1:0"), ("Passage", "k:1:1")]
    assert labels("k:1:0") == [("Passage", "k:1:0")]
    assert labels("k:2:0") == [("Passage", "k:2:0")]
    assert labels("k:9:9") == []


def test_the_speaker_prompt_carries_hint_context_and_numbered_sentences() -> None:
    chunks = [_chunk("k", 1, 0, "OPENING"), _chunk("k", 1, 1, "PREVIOUS"),
              _chunk("k", 1, 2, "PASSAGE")]
    prompt = render_speaker_prompt(speaker_context(chunks, "k:1:2"),
                                   ["I went.", "We ran."], "Watson")
    assert prompt.index("OPENING") < prompt.index("PREVIOUS") < prompt.index("PASSAGE")
    assert "narrated in the first person by Watson" in prompt
    assert "quoted speech" in prompt.lower()
    assert "1. I went.\n2. We ran." in prompt
    assert "narrated in the first person by" not in render_speaker_prompt(
        speaker_context(chunks, "k:1:2"), ["I went."], None)


def _items(*pairs):
    return SpeakerAnswers(speakers=[
        SpeakerItem(sentence=i, speaker_name=name, confidence=conf)
        for i, (name, conf) in enumerate(pairs, 1)])


def test_only_certain_validated_names_are_accepted() -> None:
    text = "“Well, Hope,” said Holmes. The Doctor nodded."
    answers = _items(("Hope", "certain"), ("Holmes", "unsure"), ("unknown", "certain"),
                     ("Jefferson Hope", "certain"), ("I", "certain"),
                     ("Doctor", "certain"), ("Watson", "certain"))
    assert validated_speakers(answers, 7, text, hint=None) == [
        "Hope", None, None, None, None, None, None]
    # The validated book hint counts as supplied text.
    assert validated_speakers(answers, 7, text, hint="Watson")[6] == "Watson"


def test_a_misaligned_answer_list_is_refused() -> None:
    text = "Hope and Holmes."
    assert validated_speakers(_items(("Hope", "certain")), 2, text, None) is None
    rotated = SpeakerAnswers(speakers=[
        SpeakerItem(sentence=2, speaker_name="Hope", confidence="certain"),
        SpeakerItem(sentence=1, speaker_name="Holmes", confidence="certain")])
    assert validated_speakers(rotated, 2, text, None) is None


# --- the resolver and its caches ------------------------------------------------

BOOKCHUNKS = [
    _chunk("sis", 5, 0, "In the year 1878 I took my degree. “Well, Watson?” said Stamford."),
    _chunk("sis", 17, 0, "“My name is Jefferson Hope,” said the prisoner."),
    _chunk("sis", 17, 1, "“I took the ring from her finger,” he went on."),
]
ROWS = [
    _row("I", "degree", ["I"], chunk="sis:5:0", evidence="I took my degree."),
    _row("I", "ring", ["I"], chunk="sis:17:1", evidence="I took the ring from her finger"),
    _row("Stamford", "the narrator", ["Stamford"], chunk="sis:5:0",
         evidence="said Stamford to me"),
]


class _FakeAsk:
    def __init__(self, hint, by_chunk):
        self.hint = hint
        self.by_chunk = by_chunk
        self.narrator_prompts: list[str] = []
        self.speaker_prompts: list[str] = []

    async def narrator(self, prompt):
        self.narrator_prompts.append(prompt)
        return self.hint, "model-a"

    async def speakers(self, prompt):
        self.speaker_prompts.append(prompt)
        marker = next(m for m in self.by_chunk if m in prompt.split("[Passage]")[1])
        return self.by_chunk[marker], "model-a"


def _resolver(tmp_path, fake):
    return make_first_person_resolver(
        fake.narrator, fake.speakers,
        NarratorCache(tmp_path / "narrators"), SpeakerCache(tmp_path / "speakers"))


async def test_the_resolver_asks_per_chunk_and_a_reload_spends_nothing(tmp_path) -> None:
    fake = _FakeAsk(NarratorAnswer(first_person=True, narrator_name="Watson"), {
        "1878": _items(("Watson", "certain"), ("Watson", "certain")),
        "the ring": _items(("Jefferson Hope", "certain")),
    })
    resolution = await _resolver(tmp_path, fake)("sis", BOOKCHUNKS, ROWS)
    assert isinstance(resolution, SpeakerResolution)
    assert resolution.narrator_hint == "Watson"
    assert resolution.speakers == {
        ("sis:5:0", "I took my degree."): "Watson",
        ("sis:5:0", "said Stamford to me"): "Watson",
        ("sis:17:1", "I took the ring from her finger"): "Jefferson Hope",
    }
    assert (resolution.requests, resolution.mismatched_chunks) == (2, 0)
    assert len(fake.narrator_prompts) == 1 and len(fake.speaker_prompts) == 2
    # The ring passage was shown its chapter's previous chunk.
    assert "My name is Jefferson Hope" in fake.speaker_prompts[1]
    stored = json.loads((tmp_path / "speakers" / "answers.json").read_text())
    assert sorted(key.split(":sha256=")[0] for key in stored) == ["sis:17:1", "sis:5:0"]

    again = _FakeAsk(None, {})
    reloaded = await _resolver(tmp_path, again)("sis", BOOKCHUNKS, ROWS)
    assert reloaded.speakers == resolution.speakers
    assert reloaded.requests == 0
    assert again.narrator_prompts == [] and again.speaker_prompts == []


async def test_changed_text_changes_the_speaker_key(tmp_path) -> None:
    fake = _FakeAsk(NarratorAnswer(first_person=False, narrator_name=""), {
        "1878": _items(("unknown", "unsure"), ("unknown", "unsure")),
        "the ring": _items(("Jefferson Hope", "certain")),
    })
    await _resolver(tmp_path, fake)("sis", BOOKCHUNKS, ROWS)
    changed = BOOKCHUNKS[:2] + [_chunk("sis", 17, 1, BOOKCHUNKS[2].text + " the ring!")]
    await _resolver(tmp_path, fake)("sis", changed, ROWS)
    assert len(fake.speaker_prompts) == 3
    assert SpeakerCache.key("sis:17:1", "a") != SpeakerCache.key("sis:17:1", "b")


async def test_a_misaligned_answer_leaves_the_chunk_unresolved_and_uncached(tmp_path) -> None:
    fake = _FakeAsk(NarratorAnswer(first_person=True, narrator_name="Watson"), {
        "1878": _items(("Watson", "certain")),  # two sentences asked, one answered
        "the ring": _items(("Jefferson Hope", "certain")),
    })
    resolution = await _resolver(tmp_path, fake)("sis", BOOKCHUNKS, ROWS)
    assert resolution.mismatched_chunks == 1
    assert set(resolution.speakers) == {("sis:17:1", "I took the ring from her finger")}
    stored = json.loads((tmp_path / "speakers" / "answers.json").read_text())
    assert len(stored) == 1


async def test_no_first_person_rows_means_no_requests(tmp_path) -> None:
    fake = _FakeAsk(None, {})
    rows = [_row("Holmes", "Watson", ["Holmes", "Watson"], chunk="sis:5:0")]
    resolution = await _resolver(tmp_path, fake)("sis", BOOKCHUNKS, rows)
    assert resolution.speakers == {} and resolution.requests == 0
    assert fake.narrator_prompts == [] and fake.speaker_prompts == []


# --- the Gemini factory ------------------------------------------------------------

def _gemini(handler) -> GeminiClient:
    async def no_sleep(_s: float) -> None:
        return None

    return GeminiClient(SECRET, models=list(LITE_MODELS),
                        transport=httpx.MockTransport(handler), sleep=no_sleep)


async def test_the_gemini_factory_sends_json_requests_and_caches(tmp_path) -> None:
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        prompt = body["contents"][0]["parts"][0]["text"]
        if "Sentences:" in prompt:
            reply = {"speakers": [
                {"sentence": 1, "speaker_name": "Jefferson Hope", "confidence": "certain"}]}
        else:
            reply = {"first_person": True, "narrator_name": "Watson"}
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]})

    config = LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})
    resolve, aclose = make_gemini_first_person(config, client=_gemini(handler))
    try:
        resolution = await resolve("sis", BOOKCHUNKS, ROWS[1:2])
    finally:
        await aclose()
    assert resolution.speakers == {
        ("sis:17:1", "I took the ring from her finger"): "Jefferson Hope"}
    assert len(seen) == 2
    assert all(body["generationConfig"] == {"temperature": 0,
                                            "responseMimeType": "application/json"}
               for body in seen)
    assert (config.narrators_dir / "answers.json").is_file()
    assert (config.speakers_dir / "answers.json").is_file()
    for path in (config.narrators_dir / "answers.json", config.speakers_dir / "answers.json"):
        assert SECRET not in path.read_text()


async def test_a_bare_speaker_list_is_accepted_as_the_answer_object(tmp_path) -> None:
    # Measured: Flash Lite sometimes answers the list itself rather than
    # {"speakers": [...]}. The content is the same; refusing it would waste
    # the request.
    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["contents"][0]["parts"][0]["text"]
        if "Sentences:" in prompt:
            reply = [{"sentence": 1, "speaker_name": "Jefferson Hope",
                      "confidence": "certain"}]
        else:
            reply = {"first_person": True, "narrator_name": "Watson"}
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]})

    config = LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})
    resolve, aclose = make_gemini_first_person(config, client=_gemini(handler))
    try:
        resolution = await resolve("sis", BOOKCHUNKS, ROWS[1:2])
    finally:
        await aclose()
    assert resolution.speakers == {
        ("sis:17:1", "I took the ring from her finger"): "Jefferson Hope"}


def test_the_gemini_factory_refuses_without_a_key(tmp_path, monkeypatch) -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("lore.graph.adjudicate.default_key_file",
                        lambda: tmp_path / "absent.env")
    config = LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})
    with pytest.raises(ValueError, match="no API key"):
        make_gemini_first_person(config)


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
        _t(f"{book}:1:5", "Narrator", "KILLS", "Drebber", ["Narrator", "Drebber"]),
    ]


def _stores(tmp_path):
    config = LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    kuzu = KuzuStore(config.kuzu_dir / "graph")
    lance = LanceStore(config.lancedb_dir)
    return config, conn, kuzu, lance


def _fixed_speakers(by_evidence: dict[str, str], calls: list, hint="John Watson"):
    async def resolve(book_id, chunks, rows):
        calls.append((book_id, sorted({row["chunk_id"] for row in rows})))
        return SpeakerResolution(
            narrator_hint=hint or "",
            speakers={(row["chunk_id"], row["evidence"]): by_evidence[row["evidence"]]
                      for row in rows if row["evidence"] in by_evidence},
            requests=3, mismatched_chunks=0)
    return resolve


def _edges(kuzu):
    return sorted(
        tuple(r) for r in kuzu.execute(
            "MATCH (a:Entity)-[r:REL]->(b:Entity) RETURN a.name, r.predicate_raw, b.name,"
            " r.evidence"
        )
    )


def _names(kuzu):
    return sorted(r[0] for r in kuzu.execute("MATCH (e:Entity) RETURN e.name"))


async def test_first_person_edges_attach_to_each_passage_speaker(tmp_path) -> None:
    config, conn, kuzu, lance = _stores(tmp_path)
    _write_book(config, conn, BOOK, _narrated_rows(BOOK))
    calls: list = []
    speakers = {
        "I MEMBER_OF Fifth Fusiliers": "John Watson",
        "Narrator POSSESSES Jezail Bullet": "John Watson",
        "Murray SAVES the narrator": "John Watson",
        "Narrator KILLS Drebber": "Jefferson Hope",
    }
    report = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never,
                             first_person=_fixed_speakers(speakers, calls))
    assert calls and calls[0][0] == BOOK
    assert report.first_person_checked
    assert report.narrator == "John Watson"
    assert (report.first_person_rewrites, report.first_person_unresolved) == (4, 0)
    assert report.speaker_requests == 3
    assert report.dropped_edges == 0
    edges = _edges(kuzu)
    assert ("John Watson", "MEMBER_OF", "Fifth Fusiliers",
            "I MEMBER_OF Fifth Fusiliers") in edges
    assert ("John Watson", "POSSESSES", "Jezail Bullet",
            "Narrator POSSESSES Jezail Bullet") in edges
    assert ("Murray", "SAVES", "John Watson", "Murray SAVES the narrator") in edges
    assert ("Jefferson Hope", "KILLS", "Drebber", "Narrator KILLS Drebber") in edges
    assert not any(e[0] == "John Watson" and e[1] == "KILLS" for e in edges)
    assert "Narrator" not in _names(kuzu) and "I" not in _names(kuzu)
    conn.close(); kuzu.close()


async def test_unresolved_first_person_edges_drop_exactly_as_before(tmp_path) -> None:
    config, conn, kuzu, lance = _stores(tmp_path)
    _write_book(config, conn, BOOK, _narrated_rows(BOOK))
    baseline = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never)
    baseline_edges, baseline_names = _edges(kuzu), _names(kuzu)
    # Today's behaviour, pinned: "I" and "the narrator" edges are dropped and
    # the capitalised "Narrator" survives as its own node.
    assert baseline.dropped_edges == 2
    assert "Narrator" in baseline_names
    assert not baseline.first_person_checked
    assert (baseline.narrator, baseline.first_person_rewrites) == ("", 0)

    calls: list = []
    nothing = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never,
                              first_person=_fixed_speakers({}, calls, hint=None))
    assert calls and nothing.first_person_checked
    assert (nothing.first_person_rewrites, nothing.first_person_unresolved) == (0, 4)
    assert (nothing.rel_edges, nothing.dropped_edges, nothing.entities) == (
        baseline.rel_edges, baseline.dropped_edges, baseline.entities)
    assert _edges(kuzu) == baseline_edges and _names(kuzu) == baseline_names
    conn.close(); kuzu.close()


async def test_two_books_each_get_their_own_speakers(tmp_path) -> None:
    config, conn, kuzu, lance = _stores(tmp_path)
    _write_book(config, conn, BOOK, [
        _t(f"{BOOK}:1:0", "I", "VISITS", "Rome", ["I", "Rome"], ["Character", "Place"]),
    ])
    _write_book(config, conn, OTHER_BOOK, [
        _t(f"{OTHER_BOOK}:1:0", "I", "VISITS", "Paris", ["I", "Paris"],
           ["Character", "Place"]),
    ])

    async def resolve(book_id, chunks, rows):
        name = {BOOK: "Anna Vale", OTHER_BOOK: "Tomas Reed"}[book_id]
        return SpeakerResolution(narrator_hint=name, speakers={
            (row["chunk_id"], row["evidence"]): name for row in rows})

    first = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never,
                            first_person=resolve)
    second = await load_book(config, conn, kuzu, lance, OTHER_BOOK, _embed, _never,
                             first_person=resolve)
    assert (first.narrator, second.narrator) == ("Anna Vale", "Tomas Reed")
    assert [(e[0], e[2]) for e in _edges(kuzu)] == [
        ("Anna Vale", "Rome"), ("Tomas Reed", "Paris")]
    conn.close(); kuzu.close()


async def test_a_reload_with_speakers_is_idempotent(tmp_path) -> None:
    config, conn, kuzu, lance = _stores(tmp_path)
    _write_book(config, conn, BOOK, _narrated_rows(BOOK))
    resolve = _fixed_speakers({"Narrator KILLS Drebber": "Jefferson Hope"}, [])
    first = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never,
                            first_person=resolve)
    edges = _edges(kuzu)
    second = await load_book(config, conn, kuzu, lance, BOOK, _embed, _never,
                             first_person=resolve)
    assert (second.rel_edges, second.entities, second.first_person_rewrites) == (
        first.rel_edges, first.entities, first.first_person_rewrites)
    assert _edges(kuzu) == edges
    conn.close(); kuzu.close()


# --- the CLI ------------------------------------------------------------------

def _fake_stores(monkeypatch, cli):
    monkeypatch.setattr(cli, "KuzuStore", lambda path: type("K", (), {"close": lambda s: None})())
    monkeypatch.setattr(cli, "LanceStore", lambda path: None)


def test_load_book_sync_wires_the_gemini_speaker_resolver(tmp_path, monkeypatch) -> None:
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

    def fake_first_person(cfg, api_key=None, **kwargs):
        seen["key"] = api_key

        async def aclose():
            seen["resolver_closed"] = True
        return "RESOLVE", aclose

    async def fake_load(*args, **kwargs):
        seen["first_person"] = kwargs.get("first_person")
        return LoadReport(book_id="b1")

    monkeypatch.setattr(cli, "make_gemini_pair", fake_pair)
    monkeypatch.setattr(cli, "make_gemini_first_person", fake_first_person)
    monkeypatch.setattr(cli, "load_book", fake_load)
    _fake_stores(monkeypatch, cli)
    cli._load_book_sync(config, None, "b1", api_key="k")
    assert seen == {"key": "k", "first_person": "RESOLVE",
                    "resolver_closed": True, "pair_closed": True}


def test_load_book_sync_skips_speakers_on_ollama(tmp_path, monkeypatch) -> None:
    import lore.cli as cli
    from lore.graph.loader import LoadReport

    config = LoreConfig.load(tmp_path / "absent.toml").model_copy(
        update={"data_dir": tmp_path / "data"})
    seen: dict = {}

    async def aclose():
        return None

    def no_resolver(*args, **kwargs):  # pragma: no cover
        raise AssertionError("speaker resolution is Gemini-only")

    async def fake_load(*args, **kwargs):
        seen["first_person"] = kwargs.get("first_person", "absent")
        return LoadReport(book_id="b1")

    monkeypatch.setattr(cli, "make_ollama_pair", lambda cfg: (None, None, aclose))
    monkeypatch.setattr(cli, "make_gemini_first_person", no_resolver)
    monkeypatch.setattr(cli, "load_book", fake_load)
    _fake_stores(monkeypatch, cli)
    cli._load_book_sync(config, None, "b1")
    assert seen == {"first_person": None}


@pytest.mark.parametrize("fields, provider, expected, absent", [
    ({"first_person_checked": True, "narrator": "Watson", "first_person_rewrites": 120,
      "first_person_unresolved": 26, "speaker_requests": 66},
     "gemini", "120 first-person endpoint(s) rewritten to their speaker, 26 left"
     " unresolved", "skipped"),
    ({"first_person_checked": True}, "gemini", "0 first-person endpoint(s) rewritten",
     "skipped"),
    ({}, "ollama", "first-person speaker resolution skipped", "rewritten"),
])
def test_process_reports_first_person_resolution(tmp_path: Path, monkeypatch, fields,
                                                 provider, expected, absent) -> None:
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
