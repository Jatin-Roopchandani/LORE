"""The query agent: which of the spec's outcomes a question lands in, and
which model, with which options, did each job.

No test here reaches Ollama or Gemini. Providers are fakes that record
what they were asked.
"""
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.extract.embed import QUERY_PREFIX
from lore.query.agent import (
    GRAPH_EMPTY_NOTE,
    SELECTION_OPTIONS,
    SYNTHESIS_OPTIONS,
    QueryModels,
    answer_question,
    make_query_models,
)
from lore.query.models import Answer
from lore.query.select import TemplateChoice
from lore.query.synthesize import REFUSAL, Claim, Synthesis

TITLE = "A Study in Scarlet"
TEXT = ("Holmes spoke to Watson about the case while they waited in the "
        "sitting room at Baker Street.")
QUOTE = "spoke to Watson about the case"
SEED_ROW = {"chunk_id": "b1:1:0", "entity_ids": ["e1"], "_distance": 0.1,
            "book_id": "b1", "chapter_seq": 1, "text": TEXT}
EDGE = [["Holmes", "OPPOSES", "distrusts", "Moriarty", "ev", 0.9, "b1", 1, "b1:1:0"]]


class FakeLance:
    def __init__(self, rows, vectors=None):
        self._rows = rows
        # Stored chunk vectors for ranking; by default every seed row's chunk
        # has one, pointing the same way as FakeOllama's question vector.
        self._vectors = vectors if vectors is not None else {
            r["chunk_id"]: [0.5] * 4 for r in rows}
        self.vector_requests: list[list[str]] = []

    def search_chunks(self, vector, limit=5): return self._rows[:limit]

    def chunk_vectors(self, chunk_ids):
        self.vector_requests.append(list(chunk_ids))
        return {c: self._vectors[c] for c in chunk_ids if c in self._vectors}


class FakeKuzu:
    """Answers seed_entities' three lookups, and returns `edges` for a template."""

    def __init__(self, edge_rows):
        self._edges = edge_rows
        self.template_params: list[dict] = []

    def execute(self, cypher, params=None):
        if "ALIAS_OF" in cypher:
            return []
        if "count(r)" in cypher:
            return [["e1", 3]]
        if "RETURN e.id, e.name" in cypher:
            return [["e1", "Holmes", "Character", ["b1"]]]
        self.template_params.append(dict(params or {}))
        return self._edges


class ExplodingKuzu:
    def execute(self, cypher, params=None):
        raise AssertionError("baseline must not query the graph")


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, author, source, file_path, status)"
                 " VALUES ('b1', ?, NULL, 'upload', '/x', 'chunked')", (TITLE,))
    conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                 " VALUES ('b1:1:0', 'b1', 1, 0, 0, ?)", (TEXT,))
    conn.commit()
    return conn


GOOD = Synthesis(claims=[Claim(text="Holmes talked the case over with Watson.",
                               passage=1, quote=QUOTE)], can_answer=True)


class FakeOllama:
    """Stands in for OllamaProvider: records every call and its options."""

    def __init__(self, choice: TemplateChoice, synthesis: Synthesis = GOOD):
        self.choice, self.synthesis = choice, synthesis
        self.generated: list[tuple[str, type, dict | None]] = []
        self.embedded: list[tuple[list[str], str]] = []
        self.closed = False

    async def generate(self, prompt, schema, *, options=None):
        self.generated.append((prompt, schema, options))
        return self.choice if schema is TemplateChoice else self.synthesis

    async def embed(self, texts, model):
        self.embedded.append((list(texts), model))
        return [[0.1] * 4 for _ in texts]

    async def aclose(self):
        self.closed = True


class FakeGemini:
    """Stands in for GeminiClient. `models` is the cascade's answer per call,
    in order (the last one repeats), so a test can make selection and
    synthesis land on different models of one shared client."""

    def __init__(self, synthesis: Synthesis = GOOD, model="gemini-3.1-flash-lite",
                 choice: TemplateChoice | None = None, models: list[str] | None = None):
        self.synthesis, self.choice = synthesis, choice
        self._models = list(models) if models else [model]
        self.prompts: list[tuple[str, type]] = []
        self.last_model = None
        self.closes = 0

    @property
    def closed(self):
        return self.closes > 0

    async def generate_json(self, prompt, schema):
        self.prompts.append((prompt, schema))
        self.last_model = self._models[min(len(self.prompts), len(self._models)) - 1]
        return self.choice if schema is TemplateChoice else self.synthesis

    async def aclose(self):
        self.closes += 1


CONFIG = LoreConfig()
GEMINI_CONFIG = CONFIG.model_copy(update={
    "synthesis": CONFIG.synthesis.model_copy(update={"provider": "gemini"})
})
SELECT_GEMINI_CONFIG = CONFIG.model_copy(update={
    "selection": CONFIG.selection.model_copy(update={"provider": "gemini"})
})
BOTH_GEMINI_CONFIG = GEMINI_CONFIG.model_copy(update={
    "selection": CONFIG.selection.model_copy(update={"provider": "gemini"})
})
CHOICE = TemplateChoice(template="neighbours", entities=[1])


def _local(choice=TemplateChoice(template="neighbours", entities=[1]), synthesis=GOOD):
    ollama = FakeOllama(choice, synthesis)
    return ollama, make_query_models(CONFIG, ollama=ollama)


def _selection_calls(ollama):
    return [c for c in ollama.generated if c[1] is TemplateChoice]


def _synthesis_calls(ollama):
    return [c for c in ollama.generated if c[1] is Synthesis]


# --- the five outcomes -----------------------------------------------------

async def test_a_full_graph_answer_reports_mode_graph(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert answer.mode == "graph"
    assert answer.template == "neighbours"
    assert answer.text == "Holmes talked the case over with Watson. [1]"
    assert answer.note is None
    assert answer.model == CONFIG.ollama.model
    conn.close()


async def test_no_seeds_is_an_empty_answer_not_an_error(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    answer = await answer_question(conn, FakeKuzu([]), FakeLance([]), "q", models)
    assert answer.mode == "empty"
    assert answer.text == REFUSAL
    # Nothing was asked of any model, so no model answered.
    assert answer.model is None
    assert ollama.generated == []
    conn.close()


async def test_an_empty_traversal_falls_back_and_says_so(tmp_path: Path) -> None:
    # The important one: degrading to vector search silently, and
    # presenting it as a graph answer, would make the phase's whole
    # claim unfalsifiable.
    conn = _conn(tmp_path)
    ollama, models = _local()
    answer = await answer_question(conn, FakeKuzu([]), FakeLance([SEED_ROW]), "q", models)
    assert answer.mode == "vector-fallback"
    assert answer.note == GRAPH_EMPTY_NOTE
    assert "graph" in answer.note.lower()
    assert answer.template is None
    assert answer.text == "Holmes talked the case over with Watson. [1]"
    assert answer.model == CONFIG.ollama.model
    conn.close()


async def test_baseline_never_touches_the_graph(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    answer = await answer_question(conn, ExplodingKuzu(), FakeLance([SEED_ROW]), "q",
                                   models, baseline=True)
    assert answer.mode == "vector-baseline"
    assert answer.template is None
    assert answer.note is None
    assert answer.model == CONFIG.ollama.model
    assert _selection_calls(ollama) == [], "baseline makes no selection call"
    conn.close()


async def test_a_rejected_template_choice_falls_back(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local(choice=TemplateChoice(template="invented", entities=[1]))
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert answer.mode == "vector-fallback"
    assert answer.note == GRAPH_EMPTY_NOTE
    conn.close()


async def test_a_refusal_still_names_the_model_that_refused(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local(synthesis=Synthesis(claims=[], can_answer=False))
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert answer.text == REFUSAL
    assert answer.model == CONFIG.ollama.model
    conn.close()


# --- options, prefixes and titles ------------------------------------------

async def test_selection_and_synthesis_get_their_own_options(tmp_path: Path) -> None:
    assert SELECTION_OPTIONS == {"temperature": 0, "seed": 0}
    # Ollama's default context is 4096 and past it the prompt's START is
    # silently dropped. 12288 holds the largest measured prompt + output.
    assert SYNTHESIS_OPTIONS == {"temperature": 0, "seed": 0, "num_ctx": 12288}
    conn = _conn(tmp_path)
    ollama, models = _local()
    await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert [c[2] for c in _selection_calls(ollama)] == [SELECTION_OPTIONS]
    assert [c[2] for c in _synthesis_calls(ollama)] == [SYNTHESIS_OPTIONS]
    conn.close()


async def test_every_embedded_query_carries_the_query_prefix_once(tmp_path: Path) -> None:
    # make_ollama_pair's embedder applies DOCUMENT_PREFIX; a query embedded
    # that way retrieves worse without ever erroring.
    conn = _conn(tmp_path)
    ollama, models = _local()
    await answer_question(conn, FakeKuzu([]), FakeLance([SEED_ROW]), "who?", models)
    texts = [t for batch, _ in ollama.embedded for t in batch]
    # Embedded once and reused: seeding and the fallback share one vector.
    assert texts == [QUERY_PREFIX + "who?"]
    assert {model for _, model in ollama.embedded} == {CONFIG.extraction.embed_model_ollama}
    conn.close()


@pytest.mark.parametrize("edges,mode", [(EDGE, "graph"), ([], "vector-fallback")])
async def test_book_titles_reach_synthesis_on_every_path(tmp_path: Path, edges, mode) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    answer = await answer_question(conn, FakeKuzu(edges), FakeLance([SEED_ROW]), "q", models)
    assert answer.mode == mode
    (prompt, _, _), = _synthesis_calls(ollama)
    assert f"({TITLE}, chapter 1)" in prompt
    conn.close()


async def test_titles_reach_synthesis_on_the_baseline_path(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    await answer_question(conn, ExplodingKuzu(), FakeLance([SEED_ROW]), "q", models,
                          baseline=True)
    (prompt, _, _), = _synthesis_calls(ollama)
    assert f"({TITLE}, chapter 1)" in prompt
    conn.close()


# --- opt-in Gemini synthesis ----------------------------------------------

async def test_gemini_answers_while_selection_stays_local(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama = FakeOllama(TemplateChoice(template="neighbours", entities=[1]))
    gemini = FakeGemini(model="gemini-3.5-flash-lite")
    models = make_query_models(GEMINI_CONFIG, ollama=ollama, gemini=gemini)
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert answer.mode == "graph"
    assert answer.text == "Holmes talked the case over with Watson. [1]"
    # The model that actually answered, as the cascade reports it.
    assert answer.model == "gemini-3.5-flash-lite"
    assert answer.selection_model == CONFIG.ollama.model
    assert [c[2] for c in _selection_calls(ollama)] == [SELECTION_OPTIONS]
    assert _synthesis_calls(ollama) == []
    (prompt, schema), = gemini.prompts
    assert schema is Synthesis
    assert f"({TITLE}, chapter 1)" in prompt
    conn.close()


async def test_the_quote_check_applies_to_gemini_too(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama = FakeOllama(TemplateChoice(template="neighbours", entities=[1]))
    invented = Synthesis(claims=[Claim(text="Holmes hates Watson.", passage=1,
                                       quote="Holmes hates Watson very much")],
                         can_answer=True)
    models = make_query_models(GEMINI_CONFIG, ollama=ollama, gemini=FakeGemini(invented))
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert answer.text == REFUSAL
    conn.close()


@pytest.mark.parametrize("config,section", [
    (GEMINI_CONFIG, "[lore.synthesis]"),
    (SELECT_GEMINI_CONFIG, "[lore.selection]"),
    (BOTH_GEMINI_CONFIG, "[lore.selection]"),
])
async def test_gemini_without_a_key_or_client_is_refused_not_downgraded(config, section) -> None:
    with pytest.raises(ValueError) as exc:
        make_query_models(config, ollama=FakeOllama(CHOICE))
    assert "key" in str(exc.value)
    assert section in str(exc.value)


async def test_aclose_closes_every_client_it_holds() -> None:
    ollama = FakeOllama(TemplateChoice(template="neighbours", entities=[1]))
    gemini = FakeGemini()
    models = make_query_models(GEMINI_CONFIG, ollama=ollama, gemini=gemini)
    assert isinstance(models, QueryModels)
    await models.aclose()
    assert ollama.closed and gemini.closed

    local = FakeOllama(TemplateChoice(template="neighbours", entities=[1]))
    await make_query_models(CONFIG, ollama=local).aclose()
    assert local.closed


def test_answer_model_defaults_to_none() -> None:
    answer = Answer(text="a", passages=(), mode="empty", template=None, note=None)
    assert answer.model is None
    assert answer.selection_model is None


# --- Task 8b: selection on Gemini ---------------------------------------------

async def test_selection_on_gemini_uses_generate_json_and_synthesis_stays_local(
        tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama = FakeOllama(CHOICE)
    gemini = FakeGemini(choice=CHOICE, model="gemini-3.1-flash-lite")
    models = make_query_models(SELECT_GEMINI_CONFIG, ollama=ollama, gemini=gemini)
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert answer.mode == "graph" and answer.template == "neighbours"
    assert [schema for _, schema in gemini.prompts] == [TemplateChoice]
    assert _selection_calls(ollama) == [], "never also asked the local model"
    assert [c[2] for c in _synthesis_calls(ollama)] == [SYNTHESIS_OPTIONS]
    assert answer.selection_model == "gemini-3.1-flash-lite"
    assert answer.model == CONFIG.ollama.model
    conn.close()


async def test_both_steps_on_gemini_share_one_client_and_name_each_model(
        tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama = FakeOllama(CHOICE)
    # The cascade reaches a different model for each call. Selection's
    # model must be read when selection ran, not after synthesis moved it.
    gemini = FakeGemini(choice=CHOICE,
                        models=["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"])
    models = make_query_models(BOTH_GEMINI_CONFIG, ollama=ollama, gemini=gemini)
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert [schema for _, schema in gemini.prompts] == [TemplateChoice, Synthesis]
    assert ollama.generated == []
    assert answer.selection_model == "gemini-3.1-flash-lite"
    assert answer.model == "gemini-3.5-flash-lite"
    await models.aclose()
    assert gemini.closes == 1 and ollama.closed
    conn.close()


def test_both_steps_on_gemini_build_exactly_one_client(monkeypatch) -> None:
    # One client, so per-model throttling and the RPM budget are counted once.
    import lore.query.agent as agent

    built = []

    class Recording:
        def __init__(self, api_key, *, models):
            built.append(tuple(models))

    monkeypatch.setattr(agent, "GeminiClient", Recording)
    make_query_models(BOTH_GEMINI_CONFIG, "k", ollama=FakeOllama(CHOICE))
    assert built == [tuple(CONFIG.synthesis.models)]


async def test_selection_model_names_the_local_model_and_is_none_when_not_asked(
        tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    graph = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert graph.selection_model == CONFIG.ollama.model
    # A rejected choice still ran selection, so the model is still named.
    _, rejecting = _local(choice=TemplateChoice(template="invented", entities=[1]))
    fallback = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q",
                                     rejecting)
    assert fallback.mode == "vector-fallback"
    assert fallback.selection_model == CONFIG.ollama.model
    baseline = await answer_question(conn, ExplodingKuzu(), FakeLance([SEED_ROW]), "q",
                                     models, baseline=True)
    assert baseline.selection_model is None
    empty = await answer_question(conn, FakeKuzu([]), FakeLance([]), "q", models)
    assert empty.selection_model is None
    conn.close()


def test_the_local_model_gets_a_cpu_sized_timeout() -> None:
    # Measured on CPU (Task 7b, num_ctx 12288): real synthesis calls took
    # 23-170 s, and three of eight exceeded the provider's 120 s default.
    from lore.query.agent import OLLAMA_TIMEOUT_S

    assert OLLAMA_TIMEOUT_S >= 600


def test_make_query_models_builds_its_ollama_with_that_timeout(monkeypatch) -> None:
    import lore.query.agent as agent

    seen = {}

    class Recording:
        def __init__(self, config, *, timeout=None):
            seen["timeout"] = timeout

        async def aclose(self):
            return None

    monkeypatch.setattr(agent, "OllamaProvider", Recording)
    make_query_models(CONFIG)
    assert seen["timeout"] == agent.OLLAMA_TIMEOUT_S


# --- Task 9b: one question vector; evidence ranked by relevance ----------------

async def test_the_graph_path_embeds_the_question_exactly_once(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    lance = FakeLance([SEED_ROW])
    answer = await answer_question(conn, FakeKuzu(EDGE), lance, "who?", models)
    assert answer.mode == "graph"
    assert [batch for batch, _ in ollama.embedded] == [[QUERY_PREFIX + "who?"]]
    # ...and that vector ranked the template's chunks.
    assert lance.vector_requests == [["b1:1:0"]]
    conn.close()


async def test_templates_are_asked_for_the_wide_candidate_pool(tmp_path: Path) -> None:
    from lore.query.agent import CANDIDATE_LIMIT

    conn = _conn(tmp_path)
    ollama, models = _local()
    kuzu = FakeKuzu(EDGE)
    await answer_question(conn, kuzu, FakeLance([SEED_ROW]), "q", models)
    assert [p["limit"] for p in kuzu.template_params] == [CANDIDATE_LIMIT]
    conn.close()


async def test_graph_evidence_keeps_the_relevant_late_passage_over_early_ones(
        tmp_path: Path) -> None:
    # Task 9's failure end to end: three early chunks and one late one, a
    # budget that holds two, and the late one is what the question is about.
    conn = _conn(tmp_path)
    edges, vectors = [], {}
    for ch, direction in ((2, [-1.0, 0.0, 0.0, 0.0]), (3, [0.0, 1.0, 0.0, 0.0]),
                          (4, [0.0, 0.0, 1.0, 0.0]), (12, [0.5, 0.5, 0.5, 0.5])):
        chunk_id = f"b1:{ch}:0"
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, 'b1', ?, 0, 0, ?)",
                     (chunk_id, ch, f"MARK{ch} " + "x" * 5000))
        edges.append(["Holmes", "OPPOSES", "distrusts", "Moriarty", "ev", 0.9,
                      "b1", ch, chunk_id])
        vectors[chunk_id] = direction
    conn.commit()
    ollama, models = _local(synthesis=Synthesis(claims=[], can_answer=False))
    await answer_question(conn, FakeKuzu(edges), FakeLance([SEED_ROW], vectors),
                          "q", models)
    (prompt, _, _), = _synthesis_calls(ollama)
    assert "MARK12" in prompt
    assert "MARK2 " not in prompt
    conn.close()


async def test_a_question_below_the_relevance_floor_is_an_empty_answer(tmp_path: Path) -> None:
    # Vector search always returns a nearest chunk, so "no seeds" never
    # happens on a real library; the floor is what makes `empty` reachable.
    from lore.query.agent import RELEVANCE_FLOOR

    conn = _conn(tmp_path)
    ollama, models = _local()
    # squared L2 on unit vectors: cosine = 1 - d/2
    far = {**SEED_ROW, "_distance": 2 * (1 - (RELEVANCE_FLOOR - 0.01))}
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([far]), "zqxjkv", models)
    assert answer.mode == "empty"
    assert answer.text == REFUSAL
    assert answer.model is None and answer.selection_model is None
    assert ollama.generated == [], "no selection and no synthesis call"
    conn.close()


async def test_a_question_above_the_relevance_floor_is_not_empty(tmp_path: Path) -> None:
    from lore.query.agent import RELEVANCE_FLOOR

    conn = _conn(tmp_path)
    ollama, models = _local()
    near = {**SEED_ROW, "_distance": 2 * (1 - (RELEVANCE_FLOOR + 0.01))}
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([near]), "q", models)
    assert answer.mode == "graph"
    conn.close()


async def test_the_floor_does_not_apply_to_the_baseline(tmp_path: Path) -> None:
    from lore.query.agent import RELEVANCE_FLOOR

    conn = _conn(tmp_path)
    ollama, models = _local()
    far = {**SEED_ROW, "_distance": 2 * (1 - (RELEVANCE_FLOOR - 0.01))}
    answer = await answer_question(conn, ExplodingKuzu(), FakeLance([far]), "q", models,
                                   baseline=True)
    assert answer.mode == "vector-baseline"
    conn.close()
