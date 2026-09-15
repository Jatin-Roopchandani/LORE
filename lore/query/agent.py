"""Orchestration only. Every stage lives in its own module; this file
decides which of the spec's four outcomes a question landed in, and which
model does each of the two LLM jobs.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Awaitable, Callable

from pydantic import BaseModel

from lore.config import LoreConfig
from lore.extract.embed import QUERY_PREFIX
from lore.inference.gemini import GeminiClient
from lore.inference.ollama import OllamaProvider
from lore.ingest.registry import list_books
from lore.query.models import Answer, Evidence
from lore.query.seed import seed_entities
from lore.query.select import select_template
from lore.query.subgraph import assemble, passages_from_chunks, run_template
from lore.query.synthesize import synthesize

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]
Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

GRAPH_EMPTY_NOTE = (
    "the graph had no relationships for this question — "
    "answered from passages alone"
)

# Selection must be reproducible: temperature 0 AND seed 0 (see
# lore/graph/adjudicate.py for why temperature alone was not enough).
SELECTION_OPTIONS = {"temperature": 0, "seed": 0}
# Synthesis also needs room. Ollama's default context is 4096, and a prompt
# past num_ctx is cut from the START with no API error -- the instructions
# and question vanish. 12288 holds the largest measured prompt, a structural
# worst case (~6.5k) and the 4096-token output budget (Phase 3 Task 7 note).
SYNTHESIS_OPTIONS = {"temperature": 0, "seed": 0, "num_ctx": 12288}
# Per request. On CPU (the only safe Ollama mode on this machine) Task 7b's
# real synthesis calls took 23-170 s at num_ctx 12288, and three of eight
# exceeded the provider's 120 s default, which would turn a slow answer
# into `ask failed: ReadTimeout`. 600 s is what the measured runs used.
OLLAMA_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class QueryModels:
    """The query side's models: one embedder and two generate callables.

    Two callables, not one, because the jobs differ. Each runs on the local
    model (selection with SELECTION_OPTIONS, synthesis with
    SYNTHESIS_OPTIONS) or on Gemini, as `[lore.selection]` and
    `[lore.synthesis]` say. `selection_model` and `synthesis_model` name the
    model that answered that step's most recent call.
    """

    embed: Embed
    select: Generate
    synthesize: Generate
    synthesis_model: Callable[[], str | None]
    aclose: Callable[[], Awaitable[None]]
    selection_model: Callable[[], str | None]


def make_query_models(
    config: LoreConfig,
    api_key: str | None = None,
    *,
    ollama: OllamaProvider | None = None,
    gemini: GeminiClient | None = None,
) -> QueryModels:
    """Build the query side's models. `ollama`/`gemini` are for tests.

    The embedder is deliberately NOT the one from
    `lore.graph.adjudicate.make_ollama_pair`: that applies DOCUMENT_PREFIX,
    and a query embedded with the document prefix retrieves worse without
    ever erroring. This one adds no prefix; QUERY_PREFIX is applied exactly
    once where a question is embedded (inside `seed_entities`, and in
    `_vector_answer` below), so it is never doubled.

    With either step on "gemini" and neither a key nor a client, this raises
    rather than quietly using the local model: that would hide the quality
    drop the user opted out of.

    When both steps are on Gemini they share ONE client (an injected
    `gemini` is always shared), so per-model throttling and the RPM budget
    are counted once and `aclose` closes it once. Only two different
    `models` lists get two clients, because a client's cascade is fixed.
    """
    remote_steps = [name for name, section in (("selection", config.selection),
                                               ("synthesis", config.synthesis))
                    if section.provider == "gemini"]
    if remote_steps and gemini is None and not api_key:
        # Checked before any client opens, so a refusal leaks nothing.
        sections = " and ".join(f"[lore.{name}]" for name in remote_steps)
        raise ValueError(f'{sections} provider = "gemini" needs an API key')
    local = ollama if ollama is not None else OllamaProvider(
        config.ollama, timeout=OLLAMA_TIMEOUT_S
    )
    remotes: dict[tuple[str, ...], GeminiClient] = {}

    def remote(models: list[str]) -> GeminiClient:
        if gemini is not None:
            return gemini
        key = tuple(models)
        if key not in remotes:
            remotes[key] = GeminiClient(api_key, models=models)
        return remotes[key]

    async def embed(texts: list[str]) -> list[list[float]]:
        return await local.embed(texts, model=config.extraction.embed_model_ollama)

    if config.selection.provider == "gemini":
        selector = remote(config.selection.models)

        async def select(prompt: str, schema: type[BaseModel]) -> BaseModel:
            return await selector.generate_json(prompt, schema)

        def selection_model() -> str | None:
            return selector.last_model
    else:
        async def select(prompt: str, schema: type[BaseModel]) -> BaseModel:
            return await local.generate(prompt, schema, options=SELECTION_OPTIONS)

        def selection_model() -> str | None:
            return config.ollama.model

    if config.synthesis.provider == "gemini":
        writer = remote(config.synthesis.models)

        async def synthesize_with(prompt: str, schema: type[BaseModel]) -> BaseModel:
            return await writer.generate_json(prompt, schema)

        def synthesis_model() -> str | None:
            return writer.last_model
    else:
        async def synthesize_with(prompt: str, schema: type[BaseModel]) -> BaseModel:
            return await local.generate(prompt, schema, options=SYNTHESIS_OPTIONS)

        def synthesis_model() -> str | None:
            return config.ollama.model

    opened = [gemini] if gemini is not None and remote_steps else list(remotes.values())

    async def aclose() -> None:
        try:
            for client in opened:
                await client.aclose()
        finally:
            await local.aclose()

    return QueryModels(embed, select, synthesize_with, synthesis_model, aclose,
                       selection_model)


async def _select(question, seeds, models: QueryModels):
    """select_template(), plus the model that chose, read when it chose.

    Read straight after the call, not at the end: with both steps on one
    Gemini client, synthesis moves the client's `last_model` afterwards.
    """
    chosen_by = None

    async def generate(prompt: str, schema: type[BaseModel]) -> BaseModel:
        nonlocal chosen_by
        result = await models.select(prompt, schema)
        chosen_by = models.selection_model()
        return result

    call = await select_template(question, seeds, generate)
    return call, chosen_by


async def _synthesize(question, evidence, models: QueryModels, **kwargs) -> Answer:
    """synthesize(), with Answer.model set only if a model was actually asked."""
    asked = False

    async def generate(prompt: str, schema: type[BaseModel]) -> BaseModel:
        nonlocal asked
        asked = True
        return await models.synthesize(prompt, schema)

    answer = await synthesize(question, evidence, generate, **kwargs)
    return replace(answer, model=models.synthesis_model() if asked else None)


async def _vector_answer(lance, question, models, titles, *, mode, note) -> Answer:
    vector = (await models.embed([QUERY_PREFIX + question]))[0]
    passages = passages_from_chunks(lance.search_chunks(vector, limit=5))
    evidence = Evidence(edges=(), passages=passages, truncated=False)
    return await _synthesize(question, evidence, models, mode=mode, note=note,
                             book_titles=titles)


async def answer_question(
    conn: sqlite3.Connection,
    kuzu,
    lance,
    question: str,
    models: QueryModels,
    *,
    baseline: bool = False,
) -> Answer:
    titles = {book["id"]: book["title"] for book in list_books(conn)}

    if baseline:
        return await _vector_answer(lance, question, models, titles,
                                    mode="vector-baseline", note=None)

    seeds = await seed_entities(question, lance, kuzu, models.embed)
    if not seeds:
        # A legitimate empty answer, not a failure: nothing in the library
        # is close enough to the question to start a traversal from.
        return await _synthesize(
            question, Evidence(edges=(), passages=(), truncated=False), models,
            mode="empty", book_titles=titles,
        )

    call, chosen_by = await _select(question, seeds, models)
    rows = run_template(kuzu, call) if call is not None else []
    if not rows:
        answer = await _vector_answer(lance, question, models, titles,
                                      mode="vector-fallback", note=GRAPH_EMPTY_NOTE)
    else:
        evidence = assemble(conn, rows)
        answer = await _synthesize(question, evidence, models, mode="graph",
                                   template=call.template, book_titles=titles)
    return replace(answer, selection_model=chosen_by)
