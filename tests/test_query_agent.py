"""The query agent: which of the spec's outcomes a question lands in, and
which model, with which options, did each job.

No test here reaches Ollama or Gemini. Providers are fakes that record
what they were asked.
"""
import re
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.inference.base import ProviderOutputError
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
from lore.query.decompose import SubQuestions
from lore.query.models import Answer
from lore.query.select import TemplateChoice
from lore.query.synthesize import (
    CHECK_SKIPPED_NOTE,
    NOT_SUPPORTED,
    REFUSAL,
    SUPPORTED,
    Claim,
    Faithfulness,
    Judgement,
    Synthesis,
)

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
        if "e.aliases" in cypher or "b:Book" in cypher:  # Task 10d's reads
            return []
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


def all_supported(prompt: str) -> Faithfulness:
    """What an honest faithfulness check answers: one aligned `supported`
    per claim. Both fakes reply with this, so a test that is about something
    else is not silently answering a Task 12 check with the wrong schema."""
    count = int(re.search(r"numbered 1 to (\d+)", prompt).group(1))
    return Faithfulness(judgements=[
        Judgement(claim=n, verdict=SUPPORTED, reason="the passage states it")
        for n in range(1, count + 1)])


class FakeOllama:
    """Stands in for OllamaProvider: records every call and its options."""

    def __init__(self, choice: TemplateChoice, synthesis: Synthesis = GOOD):
        self.choice, self.synthesis = choice, synthesis
        self.generated: list[tuple[str, type, dict | None]] = []
        self.embedded: list[tuple[list[str], str]] = []
        self.closed = False

    async def generate(self, prompt, schema, *, options=None):
        self.generated.append((prompt, schema, options))
        if schema is Faithfulness:
            return all_supported(prompt)
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
                 choice: TemplateChoice | None = None, models: list[str] | None = None,
                 faithfulness: Faithfulness | None = None):
        self.synthesis, self.choice = synthesis, choice
        self.faithfulness = faithfulness
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
        if schema is Faithfulness:
            return self.faithfulness or all_supported(prompt)
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
    # Task 10d changed this. It was `(prompt, schema), = gemini.prompts` with
    # `schema is Synthesis`: decomposition now runs on the synthesis provider.
    # Task 12 changed this. It was `== [SubQuestions, Synthesis]` and failed
    # with "Left contains one more item: Faithfulness": the faithfulness
    # check is a third call on the same synthesis client.
    assert [schema for _, schema in gemini.prompts] == [SubQuestions, Synthesis,
                                                        Faithfulness]
    prompt = gemini.prompts[1][0]
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


# --- Task 12: the faithfulness check is wired like selection and synthesis ----

def _rejected(prompt: str) -> Faithfulness:
    count = int(re.search(r"numbered 1 to (\d+)", prompt).group(1))
    return Faithfulness(judgements=[
        Judgement(claim=n, verdict=NOT_SUPPORTED, reason="the passage does not say it")
        for n in range(1, count + 1)])


async def test_a_claim_the_check_rejects_never_reaches_the_answer(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama = FakeOllama(TemplateChoice(template="neighbours", entities=[1]))
    gemini = FakeGemini(faithfulness=None)
    models = make_query_models(GEMINI_CONFIG, ollama=ollama, gemini=gemini)
    kept = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert kept.text == "Holmes talked the case over with Watson. [1]"

    rejecting = FakeGemini()
    rejecting.generate_json = _rejecting(rejecting)
    models = make_query_models(GEMINI_CONFIG, ollama=FakeOllama(
        TemplateChoice(template="neighbours", entities=[1])), gemini=rejecting)
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert answer.text == REFUSAL
    conn.close()


def _rejecting(fake):
    inner = fake.generate_json

    async def generate_json(prompt, schema):
        result = await inner(prompt, schema)
        return _rejected(prompt) if schema is Faithfulness else result
    return generate_json


async def test_the_check_runs_on_the_synthesis_provider_with_its_options(
        tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    # Synthesis local: the check is a local call, with the synthesis options
    # (num_ctx included -- the check prompt carries the passages again).
    ollama, models = _local()
    await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    checks = [c for c in ollama.generated if c[1] is Faithfulness]
    assert [c[2] for c in checks] == [SYNTHESIS_OPTIONS]

    # Synthesis on Gemini: the check goes to Gemini, never also to Ollama.
    local = FakeOllama(TemplateChoice(template="neighbours", entities=[1]))
    gemini = FakeGemini()
    remote = make_query_models(GEMINI_CONFIG, ollama=local, gemini=gemini)
    await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", remote)
    assert [s for _, s in gemini.prompts].count(Faithfulness) == 1
    assert [c for c in local.generated if c[1] is Faithfulness] == []
    conn.close()


async def test_models_without_a_check_answer_exactly_as_before(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    from dataclasses import replace as _replace
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q",
                                   _replace(models, check=None))
    assert answer.text == "Holmes talked the case over with Watson. [1]"
    assert answer.note is None
    assert [c for c in ollama.generated if c[1] is Faithfulness] == []
    conn.close()


async def test_a_check_that_fails_keeps_the_claims_and_says_so(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()

    async def broken(prompt, schema):
        raise ProviderOutputError("not a Faithfulness object", "{")

    from dataclasses import replace as _replace
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q",
                                   _replace(models, check=broken))
    assert answer.text == "Holmes talked the case over with Watson. [1]"
    assert answer.note == CHECK_SKIPPED_NOTE
    conn.close()


def test_make_query_models_gives_the_check_the_synthesis_callable() -> None:
    models = make_query_models(CONFIG, ollama=FakeOllama(CHOICE))
    assert models.check is models.synthesize


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
    # Task 10d changed this. It was models=[3.1, 3.5] and
    # `[schema ...] == [TemplateChoice, Synthesis]`: decomposition is now the
    # first call on the shared client.
    gemini = FakeGemini(choice=CHOICE,
                        models=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                                "gemini-3.5-flash-lite"])
    models = make_query_models(BOTH_GEMINI_CONFIG, ollama=ollama, gemini=gemini)
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    # Task 12 changed this. It was `== [SubQuestions, TemplateChoice,
    # Synthesis]` and failed with "Left contains one more item:
    # Faithfulness": the check is a fourth call on the shared client.
    assert [schema for _, schema in gemini.prompts] == [SubQuestions, TemplateChoice,
                                                        Synthesis, Faithfulness]
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
        def __init__(self, api_key, *, models, recorder=None):
            built.append(tuple(models))

    monkeypatch.setattr(agent, "GeminiClient", Recording)
    make_query_models(BOTH_GEMINI_CONFIG, "k", ollama=FakeOllama(CHOICE))
    assert built == [tuple(CONFIG.synthesis.models)]


def test_make_query_models_threads_the_recorder_to_the_client(monkeypatch) -> None:
    """The CLI's own path must count too, or the shared ledger is a fiction."""
    import lore.query.agent as agent

    seen: list[str] = []
    built = []

    class Recording:
        def __init__(self, api_key, *, models, recorder=None):
            built.append(recorder)

        async def aclose(self):
            return None

    monkeypatch.setattr(agent, "GeminiClient", Recording)
    make_query_models(GEMINI_CONFIG, "k", ollama=FakeOllama(CHOICE), recorder=seen.append)
    assert built == [seen.append]


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
    # Task 10d changed this. It was `assert ollama.generated == []`: the floor
    # now reads the best score over the sub-questions, so decomposition runs
    # first. Still no selection and no synthesis call.
    assert _selection_calls(ollama) == [] and _synthesis_calls(ollama) == []
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


# --- Task 10d: vector union, decomposition, named seeds, budget ----------------

from lore.query.agent import (  # noqa: E402
    GEMINI_EVIDENCE_CHARS,
    VECTOR_K,
    evidence_budget,
)
from lore.query.templates import MAX_EVIDENCE_CHARS  # noqa: E402


def _chunk(conn, chunk_id, text, chapter):
    conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                 " VALUES (?, 'b1', ?, 0, ?, ?)", (chunk_id, chapter, chapter, text))
    conn.commit()


def _row(chunk_id, entity_ids, distance, chapter, text=""):
    return {"chunk_id": chunk_id, "entity_ids": entity_ids, "_distance": distance,
            "book_id": "b1", "chapter_seq": chapter, "text": text}


class VectorLance:
    """search_chunks answers by which vector asked; chunk vectors are stored."""

    def __init__(self, rows_by_vector, vectors):
        self._rows, self._vectors = rows_by_vector, vectors
        self.searches: list[tuple] = []

    def search_chunks(self, vector, limit=5):
        self.searches.append((tuple(vector), limit))
        return self._rows.get(tuple(vector), [])[:limit]

    def chunk_vectors(self, chunk_ids):
        return {c: self._vectors[c] for c in chunk_ids if c in self._vectors}


class EntityKuzu:
    """Edges per entity; every entity with edges is live and canonical."""

    def __init__(self, edges_by_entity, names=()):
        self._edges, self._names = edges_by_entity, list(names)
        self.template_params: list[dict] = []
        self.name_reads = 0

    def execute(self, cypher, params=None):
        params = params or {}
        if "e.aliases" in cypher:
            self.name_reads += 1
            return [list(r) for r in self._names]
        if "b:Book" in cypher:
            return [["A Study in Scarlet"]]
        if "ALIAS_OF" in cypher:
            return []
        if "count(r)" in cypher:
            return [[i, 1] for i in params["ids"] if self._edges.get(i)]
        if "RETURN e.id, e.name" in cypher:
            return [[i, i.upper(), "Character", ["b1"]] for i in params["ids"]]
        self.template_params.append(dict(params))
        return self._edges.get(params.get("entity_id"), [])


def _edge_row(chunk_id, chapter, subject):
    return [subject, "UNCATEGORIZED", "met", "X", "ev", 0.9, "b1", chapter, chunk_id]


def _scripted(vectors_by_text, sub_questions, choices, synthesis=None, *,
              evidence_chars=MAX_EVIDENCE_CHARS):
    """QueryModels whose every answer is scripted and every call recorded."""
    calls = {"embed": [], "decompose": [], "select": [], "synthesize": []}

    async def embed(texts):
        calls["embed"].append(list(texts))
        return [vectors_by_text[t] for t in texts]

    async def decompose(prompt, schema):
        calls["decompose"].append(prompt)
        return SubQuestions(sub_questions=sub_questions)

    async def select(prompt, schema):
        calls["select"].append(prompt)
        return choices[len(calls["select"]) - 1]

    async def synthesize(prompt, schema):
        calls["synthesize"].append(prompt)
        return synthesis or Synthesis(claims=[], can_answer=False)

    async def aclose():
        return None

    models = QueryModels(embed, select, synthesize, lambda: "writer", aclose,
                         lambda: "chooser", decompose=decompose,
                         evidence_chars=evidence_chars)
    return models, calls


A, B, Q = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.6, 0.6, 0.529]


async def test_the_union_adds_a_vector_only_chunk_to_the_evidence(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _chunk(conn, "b1:9:0", "VECTORONLY passage with no edges at all", 9)
    lance = VectorLance(
        {tuple(Q): [_row("b1:1:0", ["e1"], 0.2, 1), _row("b1:9:0", [], 0.3, 9)]},
        {"b1:1:0": Q, "b1:9:0": Q})
    kuzu = EntityKuzu({"e1": [_edge_row("b1:1:0", 1, "Holmes")]})
    models, calls = _scripted({QUERY_PREFIX + "who?": Q}, ["who?"],
                              [TemplateChoice(template="neighbours", entities=[1])])
    answer = await answer_question(conn, kuzu, lance, "who?", models)
    assert answer.mode == "graph"
    assert [p.chunk_id for p in answer.passages] == ["b1:1:0", "b1:9:0"]
    assert "VECTORONLY" in calls["synthesize"][0]
    # The union uses the baseline's k and the question's own vector.
    assert (tuple(Q), VECTOR_K) in lance.searches
    conn.close()


async def test_a_single_part_question_is_todays_pipeline_plus_the_union(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    lance = VectorLance({tuple(Q): [_row("b1:1:0", ["e1"], 0.2, 1)]}, {"b1:1:0": Q})
    kuzu = EntityKuzu({"e1": [_edge_row("b1:1:0", 1, "Holmes")]})
    models, calls = _scripted({QUERY_PREFIX + "who?": Q}, ["who?"],
                              [TemplateChoice(template="neighbours", entities=[1])])
    answer = await answer_question(conn, kuzu, lance, "who?", models)
    assert calls["embed"] == [[QUERY_PREFIX + "who?"]], "embedded once"
    assert len(calls["decompose"]) == 1 and len(calls["select"]) == 1
    assert answer.template == "neighbours"
    assert answer.sub_questions == ("who?",)
    assert "covers" not in calls["synthesize"][0].lower()
    assert kuzu.name_reads == 1
    conn.close()


async def test_malformed_decomposition_falls_back_to_the_question(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()  # FakeOllama answers SubQuestions with a Synthesis
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert answer.mode == "graph"
    assert answer.sub_questions == ("q",)
    assert len(_selection_calls(ollama)) == 1
    conn.close()


async def test_decomposition_runs_on_the_synthesis_provider_deterministically(
        tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    (_, _, options), = [c for c in ollama.generated if c[1] is SubQuestions]
    assert options == SELECTION_OPTIONS

    ollama = FakeOllama(CHOICE)
    gemini = FakeGemini()
    models = make_query_models(GEMINI_CONFIG, ollama=ollama, gemini=gemini)
    await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert [s for _, s in gemini.prompts][0] is SubQuestions
    assert not [c for c in ollama.generated if c[1] is SubQuestions]
    conn.close()


async def test_a_two_part_question_gets_both_parts_before_a_second_for_either(
        tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    big = "x" * 5000  # the ollama budget (12,000) holds two
    for chunk_id, ch in (("b1:2:0", 2), ("b1:3:0", 3), ("b1:4:0", 4)):
        _chunk(conn, chunk_id, f"MARK{ch} " + big, ch)
    question = "Who is A, and why B?"
    subs = ["Who is A?", "Why B?"]
    lance = VectorLance(
        {tuple(Q): [_row("b1:2:0", ["ea"], 0.4, 2)],
         tuple(A): [_row("b1:2:0", ["ea"], 0.1, 2), _row("b1:3:0", ["ea"], 0.2, 3)],
         tuple(B): [_row("b1:4:0", ["eb"], 0.5, 4)]},
        # b1:3:0 is A's second-best and still outscores B's best on B.
        {"b1:2:0": [0.95, 0.0, 0.0], "b1:3:0": [0.9, 0.0, 0.0], "b1:4:0": [0.0, 0.3, 0.0]})
    kuzu = EntityKuzu({"ea": [_edge_row("b1:2:0", 2, "A"), _edge_row("b1:3:0", 3, "A")],
                       "eb": [_edge_row("b1:4:0", 4, "B")]})
    models, calls = _scripted(
        {QUERY_PREFIX + question: Q, QUERY_PREFIX + subs[0]: A, QUERY_PREFIX + subs[1]: B},
        subs,
        [TemplateChoice(template="neighbours", entities=[1]),
         TemplateChoice(template="two_hop", entities=[1])])
    answer = await answer_question(conn, kuzu, lance, question, models)
    assert answer.mode == "graph"
    assert [p.chunk_id for p in answer.passages] == ["b1:2:0", "b1:4:0"]
    assert answer.template == "neighbours+two_hop"
    assert answer.sub_questions == tuple(subs)
    # One selection per part, each asked about its own part.
    assert len(calls["select"]) == 2
    assert "Question: Who is A?" in calls["select"][0]
    assert "Question: Why B?" in calls["select"][1]
    # Sub-questions are embedded together, once; the question once.
    assert sorted(t for batch in calls["embed"] for t in batch) == sorted(
        QUERY_PREFIX + t for t in [question, *subs])
    # Task 10e: one synthesis call per part, over that part's own passages
    # under their global numbers, with the original question for context.
    first, second = calls["synthesize"]
    assert "Question: Who is A?" in first and "Question: Why B?" in second
    assert question in first and question in second
    assert "[1] (A Study in Scarlet, chapter 2)\nMARK2" in first and "MARK4" not in first
    assert "[2] (A Study in Scarlet, chapter 4)\nMARK4" in second and "MARK2" not in second
    # Decomposition is told the graph's books, so "in each novel" can split.
    assert "A Study in Scarlet" in calls["decompose"][0]
    conn.close()


async def test_template_names_are_deduplicated_in_part_order(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    subs = ["one?", "two?", "three?"]
    rows = {tuple(v): [_row("b1:1:0", ["e1"], 0.2, 1)] for v in (Q, A, B, [0.0, 0.0, 1.0])}
    lance = VectorLance(rows, {"b1:1:0": Q})
    kuzu = EntityKuzu({"e1": [_edge_row("b1:1:0", 1, "Holmes")]})
    models, _ = _scripted(
        {QUERY_PREFIX + "q": Q, QUERY_PREFIX + "one?": A, QUERY_PREFIX + "two?": B,
         QUERY_PREFIX + "three?": [0.0, 0.0, 1.0]}, subs,
        [TemplateChoice(template="two_hop", entities=[1]),
         TemplateChoice(template="neighbours", entities=[1]),
         TemplateChoice(template="two_hop", entities=[1])])
    answer = await answer_question(conn, kuzu, lance, "q", models)
    assert answer.template == "two_hop+neighbours"
    conn.close()


async def test_named_entities_are_seeded_first_for_every_part(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    lance = VectorLance({tuple(Q): [_row("b1:1:0", ["e1"], 0.2, 1)]}, {"b1:1:0": Q})
    kuzu = EntityKuzu({"e1": [_edge_row("b1:1:0", 1, "Holmes")],
                       "ferrier": [_edge_row("b1:1:0", 1, "Ferrier")]},
                      names=[("ferrier", "John Ferrier", ["ferrier"])])
    models, calls = _scripted({QUERY_PREFIX + "Who was John Ferrier?": Q},
                              ["Who was John Ferrier?"],
                              [TemplateChoice(template="neighbours", entities=[1])])
    await answer_question(conn, kuzu, lance, "Who was John Ferrier?", models)
    assert "1) FERRIER" in calls["select"][0] and "2) E1" in calls["select"][0]
    assert kuzu.template_params[0]["entity_id"] == "ferrier"
    conn.close()


async def test_the_floor_reads_the_best_part_not_the_whole_question(tmp_path: Path) -> None:
    from lore.query.agent import RELEVANCE_FLOOR

    conn = _conn(tmp_path)
    far = 2 * (1 - (RELEVANCE_FLOOR - 0.05))
    near = 2 * (1 - (RELEVANCE_FLOOR + 0.05))
    lance = VectorLance({tuple(Q): [_row("b1:1:0", ["e1"], far, 1)],
                         tuple(A): [_row("b1:1:0", ["e1"], near, 1)],
                         tuple(B): [_row("b1:1:0", ["e1"], far, 1)]}, {"b1:1:0": Q})
    kuzu = EntityKuzu({"e1": [_edge_row("b1:1:0", 1, "Holmes")]})
    vectors = {QUERY_PREFIX + "q": Q, QUERY_PREFIX + "a?": A, QUERY_PREFIX + "b?": B}
    choice = TemplateChoice(template="neighbours", entities=[1])
    models, _ = _scripted(vectors, ["a?", "b?"], [choice, choice])
    assert (await answer_question(conn, kuzu, lance, "q", models)).mode == "graph"
    models, calls = _scripted(vectors, ["b?"], [choice])
    answer = await answer_question(conn, kuzu, lance, "q", models)
    assert answer.mode == "empty" and calls["select"] == [] and calls["synthesize"] == []
    conn.close()


async def test_an_empty_traversal_falls_back_to_every_parts_vector_passages(
        tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _chunk(conn, "b1:9:0", "MARKB passage", 9)
    lance = VectorLance({tuple(Q): [_row("b1:1:0", ["e1"], 0.2, 1)],
                         tuple(A): [_row("b1:1:0", ["e1"], 0.2, 1)],
                         tuple(B): [_row("b1:9:0", ["e1"], 0.3, 9)]},
                        {"b1:1:0": A, "b1:9:0": B})
    kuzu = EntityKuzu({"e1": [_edge_row("b1:1:0", 1, "Holmes")]})
    choice = TemplateChoice(template="invented", entities=[1])
    models, calls = _scripted({QUERY_PREFIX + "q": Q, QUERY_PREFIX + "a?": A,
                               QUERY_PREFIX + "b?": B}, ["a?", "b?"], [choice, choice])
    answer = await answer_question(conn, kuzu, lance, "q", models)
    assert answer.mode == "vector-fallback"
    assert answer.note == GRAPH_EMPTY_NOTE
    assert answer.template is None
    assert [p.chunk_id for p in answer.passages] == ["b1:1:0", "b1:9:0"]
    conn.close()


def test_the_evidence_budget_follows_the_synthesis_provider() -> None:
    assert evidence_budget("ollama") == MAX_EVIDENCE_CHARS == 12_000
    assert evidence_budget("gemini") == GEMINI_EVIDENCE_CHARS
    assert GEMINI_EVIDENCE_CHARS > MAX_EVIDENCE_CHARS
    assert make_query_models(CONFIG, ollama=FakeOllama(CHOICE)).evidence_chars == 12_000
    gemini = make_query_models(GEMINI_CONFIG, ollama=FakeOllama(CHOICE), gemini=FakeGemini())
    assert gemini.evidence_chars == GEMINI_EVIDENCE_CHARS
    # Selection's provider does not decide it: synthesis reads the evidence.
    select_only = make_query_models(SELECT_GEMINI_CONFIG, ollama=FakeOllama(CHOICE),
                                    gemini=FakeGemini())
    assert select_only.evidence_chars == 12_000


async def test_the_agent_assembles_within_the_models_budget(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    for ch in (2, 3):
        _chunk(conn, f"b1:{ch}:0", f"MARK{ch} " + "x" * 900, ch)
    lance = VectorLance({tuple(Q): [_row("b1:2:0", ["e1"], 0.1, 2),
                                    _row("b1:3:0", ["e1"], 0.2, 3)]},
                        {"b1:2:0": Q, "b1:3:0": [0.1, 0.1, 0.1]})
    kuzu = EntityKuzu({"e1": [_edge_row("b1:2:0", 2, "H"), _edge_row("b1:3:0", 3, "H")]})
    choice = [TemplateChoice(template="neighbours", entities=[1])]
    small, _ = _scripted({QUERY_PREFIX + "q": Q}, ["q"], choice, evidence_chars=1000)
    assert [p.chunk_id for p in (await answer_question(conn, kuzu, lance, "q", small)).passages
            ] == ["b1:2:0"]
    roomy, _ = _scripted({QUERY_PREFIX + "q": Q}, ["q"], choice, evidence_chars=5000)
    assert len((await answer_question(conn, kuzu, lance, "q", roomy)).passages) == 2
    conn.close()


def test_answer_gains_sub_questions_without_breaking_old_constructions() -> None:
    answer = Answer(text="a", passages=(), mode="empty", template=None, note=None)
    assert answer.sub_questions == ()


async def test_baseline_stays_undecomposed(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    answer = await answer_question(conn, ExplodingKuzu(), FakeLance([SEED_ROW]), "q", models,
                                   baseline=True)
    assert not [c for c in ollama.generated if c[1] is SubQuestions]
    assert answer.sub_questions == ()
    conn.close()


# --- Task 10e: per-part synthesis; book fairness only on the fallback path -----

async def test_a_two_part_answer_is_composed_in_part_order_and_names_a_missing_part(
        tmp_path: Path) -> None:
    from lore.query.synthesize import PARTIAL_NOTE

    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, author, source, file_path, status)"
                 " VALUES ('b1', 'Book', NULL, 'upload', '/x', 'chunked')")
    conn.commit()
    _chunk(conn, "b1:1:0", "Alpha rode the grey horse home at dawn.", 1)
    _chunk(conn, "b1:2:0", "Beta hid the letters beneath the floorboards.", 2)
    _chunk(conn, "b1:3:0", "Gamma never spoke of the matter again.", 3)
    lance = VectorLance({tuple(Q): [_row("b1:1:0", ["e1"], 0.2, 1)],
                         tuple(A): [_row("b1:1:0", ["e1"], 0.1, 1)],
                         tuple(B): [_row("b1:2:0", ["e1"], 0.1, 2)],
                         (0.0, 0.0, 1.0): [_row("b1:3:0", ["e1"], 0.1, 3)]},
                        {"b1:1:0": A, "b1:2:0": B, "b1:3:0": [0.0, 0.0, 1.0]})
    kuzu = EntityKuzu({"e1": [_edge_row("b1:1:0", 1, "H")]})
    subs = ["a?", "b?", "c?"]
    replies = [Synthesis(claims=[Claim(text="Alpha rode home.", passage=1,
                                       quote="rode the grey horse home")], can_answer=True),
               Synthesis(claims=[], can_answer=False),
               Synthesis(claims=[Claim(text="Gamma kept silent.", passage=3,
                                       quote="never spoke of the matter")], can_answer=True)]
    models, calls = _scripted({QUERY_PREFIX + "q": Q, QUERY_PREFIX + "a?": A,
                               QUERY_PREFIX + "b?": B, QUERY_PREFIX + "c?": [0.0, 0.0, 1.0]},
                              subs, [CHOICE, CHOICE, CHOICE])

    async def synthesize(prompt, schema):
        calls["synthesize"].append(prompt)
        return replies[len(calls["synthesize"]) - 1]

    models = QueryModels(models.embed, models.select, synthesize, lambda: "writer",
                         models.aclose, lambda: "chooser", decompose=models.decompose)
    answer = await answer_question(conn, kuzu, lance, "q", models)
    assert len(calls["synthesize"]) == 3
    assert answer.text == "Alpha rode home. [1] Gamma kept silent. [3]"
    assert [p.n for p in answer.passages] == [1, 2, 3]
    assert PARTIAL_NOTE in answer.note and "not found: b?" in answer.note
    assert answer.model == "writer" and answer.mode == "graph"
    conn.close()


def _two_book_setup(tmp_path: Path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO books (id, title, author, source, file_path, status)"
                 " VALUES ('b2', 'Other', NULL, 'upload', '/y', 'chunked')")
    big = "x" * 5000  # the ollama budget (12,000) holds two
    for chunk_id, book, ch in (("b1:2:0", "b1", 2), ("b1:3:0", "b1", 3), ("b2:2:0", "b2", 2)):
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, ?, ?, 0, ?, ?)", (chunk_id, book, ch, ch, f"{chunk_id} {big}"))
    conn.commit()
    near, less = [0.95, 0.0, 0.0], [0.9, 0.0, 0.0]
    lance = VectorLance({tuple(Q): [_row("b1:2:0", ["e1"], 0.1, 2)]},
                        {"b1:2:0": near, "b1:3:0": less, "b2:2:0": [0.1, 0.0, 0.0]})
    rows = [[*_edge_row("b1:2:0", 2, "H")], [*_edge_row("b1:3:0", 3, "H")],
            ["H", "UNCATEGORIZED", "met", "X", "ev", 0.9, "b2", 2, "b2:2:0"]]
    return conn, lance, EntityKuzu({"e1": rows})


async def test_a_decomposed_part_ranks_purely_by_relevance(tmp_path: Path) -> None:
    conn, lance, kuzu = _two_book_setup(tmp_path)
    models, _ = _scripted({QUERY_PREFIX + "q": Q}, ["q"], [CHOICE])
    answer = await answer_question(conn, kuzu, lance, "q", models)
    assert [p.chunk_id for p in answer.passages] == ["b1:2:0", "b1:3:0"]
    conn.close()


async def test_a_single_part_across_books_answer_keeps_both_books(tmp_path: Path) -> None:
    conn, lance, kuzu = _two_book_setup(tmp_path)
    models, _ = _scripted({QUERY_PREFIX + "q": Q}, ["q"],
                          [TemplateChoice(template="across_books", entities=[1])])
    answer = await answer_question(conn, kuzu, lance, "q", models)
    assert [p.chunk_id for p in answer.passages] == ["b1:2:0", "b2:2:0"]
    conn.close()


async def test_a_failed_decomposition_keeps_both_books(tmp_path: Path) -> None:
    from lore.inference.gemini import GeminiOutputError

    conn, lance, kuzu = _two_book_setup(tmp_path)
    models, _ = _scripted({QUERY_PREFIX + "q": Q}, ["q"], [CHOICE])

    async def broken(prompt, schema):
        raise GeminiOutputError("not a SubQuestions object", "{")

    models = QueryModels(models.embed, models.select, models.synthesize, lambda: "w",
                         models.aclose, lambda: "c", decompose=broken)
    answer = await answer_question(conn, kuzu, lance, "q", models)
    assert [p.chunk_id for p in answer.passages] == ["b1:2:0", "b2:2:0"]
    none, _ = _scripted({QUERY_PREFIX + "q": Q}, ["q"], [CHOICE])
    no_decomposer = QueryModels(none.embed, none.select, none.synthesize, lambda: "w",
                                none.aclose, lambda: "c", decompose=None)
    answer = await answer_question(conn, kuzu, lance, "q", no_decomposer)
    assert [p.chunk_id for p in answer.passages] == ["b1:2:0", "b2:2:0"]
    conn.close()


# --- Task 3 (Phase 4): the structure the UI draws ---------------------------

async def test_answer_carries_the_claims_it_composed(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert answer.claims, "the composed claims must survive onto Answer"
    for claim in answer.claims:
        assert claim.quote
        assert claim.passage in {p.n for p in answer.passages}
    conn.close()


async def test_answer_carries_the_evidence_edges(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert answer.edges, "the subgraph the UI draws must survive onto Answer"
    assert all(edge.subject and edge.object for edge in answer.edges)
    conn.close()


async def test_answer_carries_the_seeds(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ollama, models = _local()
    answer = await answer_question(conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", models)
    assert answer.seeds
    assert all(seed.id and seed.name for seed in answer.seeds)
    conn.close()


def test_answer_defaults_keep_old_constructions_working() -> None:
    answer = Answer(text="x", passages=(), mode="empty", template=None, note=None)
    assert answer.claims == ()
    assert answer.edges == ()
    assert answer.seeds == ()


async def test_seeds_are_deduplicated_by_id_not_by_full_value(tmp_path: Path) -> None:
    """named_seeds always scores an id 0.0 (seed.py:280), while a
    vector-picked seed carries that part's own distance (seed.py:139-142,
    a per-part `best` dict). So one entity, named-seeded in one
    sub-question and only vector-found in another, produces two
    SeedEntity values that differ in `score` alone -- a dedup keyed on the
    whole value (id, name, kind, books, score) misses exactly that pair,
    and a UI drawing starting nodes would render the same entity twice
    with two different relevance numbers.
    """
    conn = _conn(tmp_path)
    question = "Who is at Baker Street, and why?"  # names nobody
    subs = ["Who is Holmes?", "why?"]
    lance = VectorLance(
        {tuple(Q): [_row("b1:1:0", ["e1"], 0.2, 1)],
         tuple(A): [_row("b1:1:0", ["e1"], 0.2, 1)],
         # e1 is only found here by vector search, at a real distance --
         # not named in this part, so it is not in `taken` and is scored.
         tuple(B): [_row("b1:1:0", ["e1"], 0.4, 1)]},
        {"b1:1:0": Q})
    # "Holmes" names e1; the whole question never says it, so only the
    # part that asks about Holmes seeds it by name.
    kuzu = EntityKuzu({"e1": [_edge_row("b1:1:0", 1, "Holmes")]},
                      names=[("e1", "Holmes", [])])
    models, _ = _scripted(
        {QUERY_PREFIX + question: Q, QUERY_PREFIX + subs[0]: A, QUERY_PREFIX + subs[1]: B},
        subs,
        [TemplateChoice(template="neighbours", entities=[1]),
         TemplateChoice(template="neighbours", entities=[1])])
    answer = await answer_question(conn, kuzu, lance, question, models)
    ids = [seed.id for seed in answer.seeds]
    assert len(ids) == len(set(ids)), f"e1 must appear once, not once per part: {ids}"
    assert ids == ["e1"]
    # First-found wins: e1 was named (score 0.0) in the first part, before
    # it was ever vector-found (score 0.4) in the second.
    assert answer.seeds[0].score == 0.0
    conn.close()
