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
from lore.inference.gemini import GeminiClient
from lore.inference.ollama import OllamaProvider
from lore.ingest.registry import list_books
from lore.query.models import Answer, Evidence
from lore.query.seed import embed_question, seed_entities
from lore.query.select import select_template
from lore.query.subgraph import (
    assemble,
    passages_from_chunks,
    relevance,
    run_template,
)
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
# How many facts a template may return: the CANDIDATE POOL, which is ranked
# against the question before the evidence budget (MAX_EVIDENCE_CHARS) cuts
# it. Truncating inside Cypher happens before relevance is known, so the
# pool should be the whole neighbourhood, not a sample of it. 250 is set
# above the largest single-entity degree in the graph, so no one-entity
# template truncates for any entity: measured 2026-09-16 on the Gemini-built
# Holmes graph (958 distinct facts), the biggest hub, Mr. Sherlock Holmes,
# has 225 facts (128 in The Sign of the Four, 97 in A Study in Scarlet), and
# across_books for him at 250 returns all 225 in ~25 ms. across_books reads
# `limit` per book; the others read it in total, so a two-entity or two-hop
# pool can still be cut on a larger graph. Ranking 250 passages is one
# LanceDB lookup and 250 dot products.
CANDIDATE_LIMIT = 250
# Below this, nothing in the library is about the question: answer `empty`
# without selecting or synthesising. Vector search always returns SOME
# nearest chunk, so without a floor `empty` was unreachable and nonsense got
# a graph traversal. Value = cosine of the question to its nearest chunk
# (1 - squared_L2/2 on unit vectors). Measured 2026-09-16 on the Holmes
# library, nomic-embed-text with QUERY_PREFIX:
#   the 8 acceptance questions: 0.676 (lowest, "Who owned the steam launch
#     Aurora?"), 0.722, 0.736, 0.740, 0.742, 0.759, 0.765, 0.771
#   off-topic: 0.568 (bread dough), 0.558 ("zqxjkv nonsense that matches
#     nothing"), 0.508 (tomato fertiliser), 0.507 (reversing a linked list)
# A clean gap of 0.108 with nothing in it; 0.62 sits near its middle, 0.056
# under the lowest real question and 0.052 over the highest off-topic one.
# Twelve questions on one library is a small sample, and short questions
# score lowest, so re-measure before moving this on another corpus.
RELEVANCE_FLOOR = 0.62


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
    once, in `seed.embed_question`, and `answer_question` embeds each
    question once and reuses that vector, so it is never doubled.

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

    call = await select_template(question, seeds, generate, limit=CANDIDATE_LIMIT)
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


async def _vector_answer(lance, question, vector, models, titles, *, mode,
                         note) -> Answer:
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

    # Embedded once: the same vector seeds the traversal, ranks its evidence
    # and, on a fallback, searches the passages.
    vector = await embed_question(question, models.embed)

    if baseline:
        return await _vector_answer(lance, question, vector, models, titles,
                                    mode="vector-baseline", note=None)

    nearest = lance.search_chunks(vector, limit=1)
    best = max((1 - float(row.get("_distance", float("inf"))) / 2 for row in nearest),
               default=float("-inf"))
    seeds = ([] if best < RELEVANCE_FLOOR else
             await seed_entities(question, lance, kuzu, models.embed, vector=vector))
    if not seeds:
        # A legitimate empty answer, not a failure: nothing in the library
        # is close enough to the question to start a traversal from, either
        # because the nearest passage is under RELEVANCE_FLOOR or because it
        # bridges to no live entity.
        return await _synthesize(
            question, Evidence(edges=(), passages=(), truncated=False), models,
            mode="empty", book_titles=titles,
        )

    call, chosen_by = await _select(question, seeds, models)
    rows = run_template(kuzu, call) if call is not None else []
    if not rows:
        answer = await _vector_answer(lance, question, vector, models, titles,
                                      mode="vector-fallback", note=GRAPH_EMPTY_NOTE)
    else:
        # The graph decided what is connected; the question decides what
        # matters within it.
        candidates = sorted({row.chunk_id for row in rows})
        scores = relevance(vector, lance.chunk_vectors(candidates))
        evidence = assemble(conn, rows, scores=scores)
        answer = await _synthesize(question, evidence, models, mode="graph",
                                   template=call.template, book_titles=titles)
    return replace(answer, selection_model=chosen_by)
