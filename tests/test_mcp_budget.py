"""lore_search: the budget guard, and the failures that are NOT the budget.

No test reaches Ollama or Gemini. `answer_question` is replaced by a fake
that makes one real call through the injected models, and the models are
fakes; the one test that goes through make_query_models patches the HTTP
transport.
"""
import dataclasses
import functools
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from lore.config import LoreConfig
from lore.db.sqlite import MIGRATIONS
from lore.inference.gemini import (
    GeminiAuthError, GeminiClient, GeminiKeyFileError, GeminiOutputError,
    GeminiQuotaError, GeminiTransportError, limits_for,
)
from lore.mcp.tools import entity, library, lore_search, subgraph
from lore.orchestrator.gemini_budget import calls_today, recorder_for, today
from lore.query.agent import QueryModels
from lore.query.models import Answer, Passage
from lore.query.synthesize import Synthesis
from tests.mcp_fixtures import build_store_triple, empty_config

FIRST, SECOND = "gemini-3.1-flash-lite", "gemini-3.5-flash-lite"
SECRET = "AIza-super-secret-key-value"
ANSWER = Answer(
    text="Holmes opposes Moriarty [1].",
    passages=(Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="x"),),
    mode="graph", template="neighbours", note=None, model=SECOND,
)


@pytest.fixture
def live_config(tmp_path) -> LoreConfig:
    config = build_store_triple(
        tmp_path,
        books=[("b1", "A Study in Scarlet")],
        entities=[("e1", "Sherlock Holmes", "Character", ["b1"], [])],
    )
    return config.model_copy(update={
        "synthesis": config.synthesis.model_copy(
            update={"provider": "gemini", "models": [FIRST, SECOND]}),
    })


@pytest.fixture
def sqlite_conn(live_config):
    conn = sqlite3.connect(live_config.sqlite_path)
    yield conn
    conn.close()


def _models(config, synthesize=None, closed=None) -> QueryModels:
    record = recorder_for(config.sqlite_path)

    async def synth(prompt, schema):
        record(SECOND)
        if synthesize is not None:
            raise synthesize
        return Synthesis(claims=[], can_answer=False)

    async def aclose():
        if closed is not None:
            closed.append(True)

    async def embed(texts):
        raise AssertionError("never embeds: answer_question is faked")

    return QueryModels(embed, synth, synth, lambda: SECOND, aclose, lambda: FIRST)


@pytest.fixture(autouse=True)
def fake_answer():
    async def answer(conn, kuzu, lance, question, models, *, baseline=False):
        await models.synthesize("prompt", Synthesis)
        return ANSWER

    with patch("lore.mcp.tools.answer_question", answer):
        yield


@pytest.fixture
def fake_models(live_config):
    return _models(live_config)


def _fill(config, *models):
    """Spend each model's whole daily allowance, on a migrated ledger."""
    conn = sqlite3.connect(config.sqlite_path)
    for model in models:
        conn.execute(
            "INSERT INTO gemini_ledger (day, model, count) VALUES (?, ?, ?)",
            (today(), model, limits_for(model).rpd))
    conn.commit()
    conn.close()


@pytest.fixture
def at_the_cap(live_config):
    _fill(live_config, FIRST, SECOND)


async def test_lore_search_returns_a_structured_answer(live_config, fake_models):
    result = await lore_search(live_config, "Who?", models=fake_models)
    assert result["answer"]
    assert "claims" in result and "edges" in result and "seeds" in result
    assert result["mode"] in {"graph", "vector-fallback", "empty"}
    json.dumps(result, allow_nan=False)
    assert result["passages"][0]["book_title"] == "A Study in Scarlet"


async def test_lore_search_refuses_at_the_cap(live_config, fake_models, at_the_cap):
    result = await lore_search(live_config, "Who?", models=fake_models)
    assert result["error"]["code"] == "budget_exhausted"
    assert "resets" in result["error"]["message"]


async def test_one_capped_model_does_not_refuse_the_search(live_config, fake_models):
    _fill(live_config, FIRST)
    result = await lore_search(live_config, "Who?", models=fake_models)
    assert "error" not in result
    assert result["answer"]


async def test_a_refused_search_makes_no_request_and_closes_the_models(
    live_config, at_the_cap
):
    closed = []
    models = _models(live_config, closed=closed)
    await lore_search(live_config, "Who?", models=models)
    check = sqlite3.connect(live_config.sqlite_path)
    assert calls_today(check, SECOND, today()) == limits_for(SECOND).rpd
    check.close()
    assert closed == [True]


async def test_the_cheap_tools_still_work_at_the_cap(live_config, at_the_cap):
    assert "books" in library(live_config)
    assert entity(live_config, "Sherlock Holmes").get("error") is None
    assert subgraph(live_config, "Sherlock Holmes").get("error") is None


async def test_a_search_records_its_calls(live_config, fake_models, sqlite_conn):
    before = calls_today(sqlite_conn, SECOND, today())
    await lore_search(live_config, "Who?", models=fake_models)
    assert calls_today(sqlite_conn, SECOND, today()) > before


async def test_no_graph_is_the_no_graph_envelope(tmp_path):
    result = await lore_search(empty_config(tmp_path), "Who?", models=None)
    assert result["error"]["code"] == "no_graph"


# Gemini failing for a reason that is NOT the budget must not be reported as
# budget_exhausted: that would send the user to wait for midnight.
@pytest.mark.parametrize("failure,fragment", [
    (GeminiTransportError("could not reach Gemini"), "could not reach"),
    (GeminiQuotaError("every model refused the request"), "refused"),
    (GeminiOutputError("model returned blank text"), "blank text"),
    (GeminiAuthError("the key was rejected (HTTP 403)"), "rejected"),
])
async def test_gemini_failures_are_upstream_unavailable(live_config, failure, fragment):
    closed = []
    models = _models(live_config, synthesize=failure, closed=closed)
    result = await lore_search(live_config, "Who?", models=models)
    assert result["error"]["code"] == "upstream_unavailable"
    assert fragment in result["error"]["message"].lower()
    assert closed == [True]


async def test_a_transport_failure_says_it_is_not_the_budget(live_config):
    models = _models(live_config, synthesize=GeminiTransportError("dns"))
    message = (await lore_search(live_config, "Who?", models=models))["error"]["message"]
    assert "not the daily budget" in message


async def test_the_models_close_when_an_unexpected_error_escapes(live_config):
    closed = []
    models = _models(live_config, synthesize=KeyError("boom"), closed=closed)
    with pytest.raises(KeyError):
        await lore_search(live_config, "Who?", models=models)
    assert closed == [True]


# --- the key ---------------------------------------------------------------------

async def test_a_missing_key_is_upstream_unavailable_without_a_secret(live_config):
    with patch("lore.mcp.tools.resolve_api_key", return_value=None):
        result = await lore_search(live_config, "Who?")
    assert result["error"]["code"] == "upstream_unavailable"
    assert "gemini.env" in result["error"]["message"]


async def test_a_bad_key_file_is_upstream_unavailable(live_config):
    exc = GeminiKeyFileError("refusing to read ~/.config/lore/gemini.env: mode 644")
    with patch("lore.mcp.tools.resolve_api_key", side_effect=exc):
        result = await lore_search(live_config, "Who?")
    assert result["error"]["code"] == "upstream_unavailable"
    assert "mode 644" in result["error"]["message"]


async def test_the_real_wiring_records_each_request_and_never_leaks_the_key(
    live_config, sqlite_conn
):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": '{"claims": [], "can_answer": false}'}]}}]})

    factory = functools.partial(GeminiClient, transport=httpx.MockTransport(handler))
    with patch("lore.mcp.tools.resolve_api_key", return_value=SECRET), \
            patch("lore.query.agent.GeminiClient", factory):
        result = await lore_search(live_config, "Who?")
    assert result["answer"]
    assert len(seen) == 1
    assert calls_today(sqlite_conn, FIRST, today()) == 1
    assert SECRET not in json.dumps(result)


async def test_a_key_the_provider_rejects_does_not_leak_it(live_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "denied"}})

    factory = functools.partial(GeminiClient, transport=httpx.MockTransport(handler))
    with patch("lore.mcp.tools.resolve_api_key", return_value=SECRET), \
            patch("lore.query.agent.GeminiClient", factory):
        result = await lore_search(live_config, "Who?")
    assert result["error"]["code"] == "upstream_unavailable"
    assert SECRET not in json.dumps(result)


# --- a library migrated only to v3 -------------------------------------------------

def _stamp_migrations_through(sqlite_path: Path, version: int) -> None:
    """Apply MIGRATIONS up to `version` directly and stamp schema_migrations,
    rather than open_db-then-drop: the real failure is a library never
    migrated past v3, like the repo's own data/lore.db."""
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
            " applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
        for applied, statements in MIGRATIONS:
            if applied > version:
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)",
                         (applied,))
        conn.commit()
    finally:
        conn.close()


async def test_lore_search_works_on_a_v3_library_and_the_ledger_row_lands(tmp_path):
    config = build_store_triple(tmp_path, books=[("b1", "A Study in Scarlet")])
    config = config.model_copy(update={
        "synthesis": config.synthesis.model_copy(
            update={"provider": "gemini", "models": [FIRST, SECOND]}),
    })
    config.sqlite_path.unlink()
    _stamp_migrations_through(config.sqlite_path, 3)
    conn = sqlite3.connect(config.sqlite_path)
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('b1', 'T', 'x')")
    conn.commit()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert "gemini_ledger" not in tables  # really v3

    result = await lore_search(config, "Who?", models=_models(config))
    assert "error" not in result, result
    check = sqlite3.connect(config.sqlite_path)
    assert calls_today(check, SECOND, today()) == 1
    check.close()


# --- review fixes ------------------------------------------------------------------

def _local_config(config: LoreConfig) -> LoreConfig:
    return config.model_copy(update={
        "synthesis": config.synthesis.model_copy(update={"provider": "ollama"}),
        "selection": config.selection.model_copy(update={"provider": "ollama"}),
    })


async def test_ollama_synthesis_ignores_a_spent_gemini_ledger(live_config):
    from lore.inference.gemini import DEFAULT_MODELS
    config = _local_config(live_config)
    _fill(config, *DEFAULT_MODELS)
    result = await lore_search(config, "Who?", models=_models(config))
    assert "error" not in result, result


async def test_an_all_local_search_never_resolves_the_key(live_config):
    config = _local_config(live_config)
    seen = {}

    def fake_make(cfg, api_key=None, **kwargs):
        seen["key"] = api_key
        return _models(cfg)

    boom = GeminiKeyFileError("mode 644")
    with patch("lore.mcp.tools.resolve_api_key", side_effect=boom), \
            patch("lore.mcp.tools.make_query_models", fake_make):
        result = await lore_search(config, "Who?")
    assert "error" not in result, result
    assert seen["key"] is None


async def test_an_empty_model_list_is_not_reported_as_a_key_problem(live_config):
    config = live_config.model_copy(update={
        "synthesis": live_config.synthesis.model_copy(update={"models": []}),
    })
    with patch("lore.mcp.tools.resolve_api_key", return_value=SECRET), \
            pytest.raises(ValueError, match="model id"):
        await lore_search(config, "Who?")


# --- the local embedder ----------------------------------------------------------
#
# Ollama is usually stopped on this machine and must be started by hand. Its
# httpx errors used to escape, and mcp 2.3.0 shows an escaped exception only
# as "Error executing tool ...".

async def test_ollama_down_is_upstream_unavailable_naming_ollama(live_config):
    closed = []
    base = _models(live_config, closed=closed)

    async def embed(texts):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    models = dataclasses.replace(base, embed=embed)

    async def answer(conn, kuzu, lance, question, models, *, baseline=False):
        await models.embed([question])
        return ANSWER

    with patch("lore.mcp.tools.answer_question", answer):
        result = await lore_search(live_config, "Who?", models=models)
    assert result["error"]["code"] == "upstream_unavailable"
    message = result["error"]["message"]
    assert "Ollama" in message and "Connection refused" in message
    assert "CUDA_VISIBLE_DEVICES=-1" in message
    assert closed == [True]
