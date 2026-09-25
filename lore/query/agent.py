"""Orchestration only. Every stage lives in its own module; this file
decides which of the spec's four outcomes a question landed in, and which
model does each of the two LLM jobs.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from pydantic import BaseModel

from lore.config import LoreConfig
from lore.extract.embed import QUERY_PREFIX
from lore.inference.gemini import GeminiClient
from lore.inference.ollama import OllamaProvider
from lore.ingest.registry import list_books
from lore.query.decompose import split
from lore.query.models import Answer, EdgeRow, Evidence, SeedEntity
from lore.query.seed import (
    embed_question,
    load_name_index,
    named_entity_ids,
    named_seeds,
    seed_entities,
)
from lore.query.select import select_template
from lore.query.subgraph import (
    assemble_ranked,
    passages_from_chunks,
    relevance,
    run_template,
)
from lore.query.synthesize import synthesize, synthesize_parts
from lore.query.templates import MAX_EVIDENCE_CHARS

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
# How many nearest passages --baseline answers from, and so how many every
# graph answer is also given, per sub-question (Task 10d): Task 10a found
# 31% of the gold chunks in the vector top 8 never reached the graph
# evidence. The same k and the same question vector as the baseline.
VECTOR_K = 5
# The evidence budget when Gemini writes the answer. MAX_EVIDENCE_CHARS
# (12,000) was sized for the local 3B's 12,288-token context and stays the
# Ollama budget. On the Holmes library the median chunk is ~1,473 chars, so
# 12,000 holds ~8 passages: 2 per part for a 4-part question, and Task 10a
# found gold cut by the budget at a median 0.014 cosine below the line.
# 36,000 holds ~24 median passages: 6 per part at 4 parts, 12 at 2.
# Request size: ~9k tokens of passages (~4 chars/token), plus relationship
# lines, instructions and the answer, keeps the one synthesis call under
# ~20k tokens and a whole ask (decomposition, up to 4 selections, synthesis)
# near 25k -- about a tenth of the Lite tier's free per-minute token
# allowance, so the 15 RPM ceiling, not tokens, still bounds throughput.
GEMINI_EVIDENCE_CHARS = 36_000


def evidence_budget(provider: str) -> int:
    """Characters of passages synthesis may read, by who writes the answer."""
    return GEMINI_EVIDENCE_CHARS if provider == "gemini" else MAX_EVIDENCE_CHARS


@dataclass(frozen=True)
class QueryModels:
    """The query side's models: one embedder and two generate callables.

    Two callables, not one, because the jobs differ. Each runs on the local
    model (selection with SELECTION_OPTIONS, synthesis with
    SYNTHESIS_OPTIONS) or on Gemini, as `[lore.selection]` and
    `[lore.synthesis]` say. `selection_model` and `synthesis_model` name the
    model that answered that step's most recent call.

    `decompose` splits the question into parts; it runs on the synthesis
    provider at temperature 0. None means no decomposition: every question
    is one part. `evidence_chars` is the evidence budget, which follows the
    synthesis provider (`evidence_budget`).

    `check` judges each kept claim against the passage it cites, in one
    batched call per answer (Task 12). It runs on the synthesis provider
    too, at temperature 0 and on the same client, so its requests land in
    the same budget the rest of the answer spends. None means the answer is
    composed straight from the quote-checked claims, as before.
    """

    embed: Embed
    select: Generate
    synthesize: Generate
    synthesis_model: Callable[[], str | None]
    aclose: Callable[[], Awaitable[None]]
    selection_model: Callable[[], str | None]
    decompose: Generate | None = field(default=None)
    evidence_chars: int = MAX_EVIDENCE_CHARS
    check: Generate | None = field(default=None)


def make_query_models(
    config: LoreConfig,
    api_key: str | None = None,
    *,
    ollama: OllamaProvider | None = None,
    gemini: GeminiClient | None = None,
    recorder: Callable[[str], None] | None = None,
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
            remotes[key] = GeminiClient(api_key, models=models, recorder=recorder)
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

        # The synthesis client and cascade; generate_json runs at temperature 0.
        decompose_with = synthesize_with

        def synthesis_model() -> str | None:
            return writer.last_model
    else:
        async def synthesize_with(prompt: str, schema: type[BaseModel]) -> BaseModel:
            return await local.generate(prompt, schema, options=SYNTHESIS_OPTIONS)

        async def decompose_with(prompt: str, schema: type[BaseModel]) -> BaseModel:
            # A short prompt, deterministic like selection.
            return await local.generate(prompt, schema, options=SELECTION_OPTIONS)

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
                       selection_model, decompose=decompose_with,
                       evidence_chars=evidence_budget(config.synthesis.provider),
                       # The same callable as synthesis: same provider, same
                       # client, same cascade, same temperature 0, and so the
                       # same request bookkeeping.
                       check=synthesize_with)


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


async def _synthesize(question, evidence, models: QueryModels, *,
                      sub_questions=None, **kwargs) -> Answer:
    """synthesize() -- or synthesize_parts() when `sub_questions` is given --
    with Answer.model set only if a model was actually asked."""
    asked = False

    async def generate(prompt: str, schema: type[BaseModel]) -> BaseModel:
        nonlocal asked
        asked = True
        return await models.synthesize(prompt, schema)

    if sub_questions is None:
        answer = await synthesize(question, evidence, generate, check=models.check,
                                  **kwargs)
    else:
        answer = await synthesize_parts(question, sub_questions, evidence, generate,
                                        check=models.check, **kwargs)
    return replace(answer, model=models.synthesis_model() if asked else None)


async def _vector_answer(lance, question, vector, models, titles, *, mode,
                         note) -> Answer:
    passages = passages_from_chunks(lance.search_chunks(vector, limit=VECTOR_K))
    evidence = Evidence(edges=(), passages=passages, truncated=False)
    return await _synthesize(question, evidence, models, mode=mode, note=note,
                             book_titles=titles)


def _cosine(row: dict) -> float:
    # Squared L2 on unit vectors -> cosine; a row with no distance is farthest.
    return 1 - float(row.get("_distance", float("inf"))) / 2


async def _parts(question, vector, models: QueryModels, kuzu):
    """(sub-question, its vector) pairs, and whether decomposition succeeded.
    A part identical to the question reuses its vector; the others are
    embedded together in one call."""
    parts = None
    if models.decompose is not None:
        # The graph's books, not the registry's: only those can be searched,
        # and "in each novel" should split into exactly those.
        graphed = sorted({str(row[0]) for row in kuzu.execute(
            "MATCH (b:Book) RETURN b.title") if row[0]})
        parts = await split(question, models.decompose, book_titles=graphed)
    decomposed = parts is not None
    parts = parts or [question]
    fresh = [part for part in parts if part != question]
    vectors = {question: vector}
    if fresh:
        vectors.update(zip(fresh, await models.embed([QUERY_PREFIX + p for p in fresh])))
    return [(part, vectors[part]) for part in parts], decomposed


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

    # Embedded once: the question's vector finds the baseline's passages on
    # every path, and ranks the evidence when the question has one part.
    vector = await embed_question(question, models.embed)

    if baseline:
        # Unchanged by Task 10d: no decomposition, no union.
        return await _vector_answer(lance, question, vector, models, titles,
                                    mode="vector-baseline", note=None)

    empty = Evidence(edges=(), passages=(), truncated=False)
    # The baseline's passages for the whole question, then each part's.
    nearest: dict[tuple, list[dict]] = {tuple(vector): lance.search_chunks(
        vector, limit=VECTOR_K)}
    if not nearest[tuple(vector)]:
        # Vector search returns a nearest chunk whenever there is any chunk,
        # so no part could find one either: skip decomposition.
        return await _synthesize(question, empty, models, mode="empty",
                                 book_titles=titles)

    parts, decomposed = await _parts(question, vector, models, kuzu)
    sub_questions = tuple(part for part, _ in parts)
    for v in (v for _, v in parts):
        if tuple(v) not in nearest:
            nearest[tuple(v)] = lance.search_chunks(v, limit=VECTOR_K)
    best = max((_cosine(row) for _, v in parts for row in nearest[tuple(v)]),
               default=float("-inf"))
    if best < RELEVANCE_FLOOR:
        # A legitimate empty answer, not a failure: nothing in the library
        # is close enough to any part of the question to start from.
        return await _synthesize(question, empty, models, mode="empty",
                                 book_titles=titles)

    # Every name and alias in one read per question, not one per part.
    names = load_name_index(kuzu)
    named_in_question = named_entity_ids(question, names)
    rows: list[EdgeRow] = []
    templates: list[str] = []
    chosen_by: list[str] = []
    # Every seed any part found, in first-found order, deduplicated by id:
    # the entities a subgraph view would draw as the question's starting
    # nodes. Deduplicating on the whole SeedEntity value is wrong -- a
    # named seed always scores 0.0 (named_seeds), while a vector-picked
    # seed carries that part's own distance, so the same entity named in
    # one part and only vector-found in another would compare unequal and
    # be kept twice, at two different scores. Keeping the first-seen score
    # is the right call: the first part to find a seed is the earliest,
    # highest-ranked context it appeared in.
    all_seeds: list[SeedEntity] = []
    seen_seed_ids: set[str] = set()
    seeded = False
    for part, part_vector in parts:
        # A part's own names first, then the whole question's: a rewritten
        # part may have dropped a name the question gave.
        named = await named_seeds(kuzu, list(dict.fromkeys(
            named_entity_ids(part, names) + named_in_question)))
        seeds = await seed_entities(part, lance, kuzu, models.embed,
                                    vector=part_vector, named=named)
        if not seeds:
            continue
        seeded = True
        for s in seeds:
            if s.id not in seen_seed_ids:
                seen_seed_ids.add(s.id)
                all_seeds.append(s)
        call, model = await _select(part, seeds, models)
        if model and model not in chosen_by:
            chosen_by.append(model)
        if call is None:
            continue
        if call.template not in templates:
            templates.append(call.template)
        rows.extend(run_template(kuzu, call))

    if not seeded:
        # None of the nearest passages bridges to a live entity.
        return await _synthesize(question, empty, models, mode="empty",
                                 book_titles=titles)

    # The graph decided what is connected and vector search what is near;
    # each part of the question decides what matters for it.
    extra = [(str(row["chunk_id"]), str(row.get("book_id", "")),
              int(row.get("chapter_seq", 0)))
             for found in nearest.values() for row in found]
    candidates = sorted({row.chunk_id for row in rows} | {c for c, _, _ in extra})
    stored = lance.chunk_vectors(candidates)
    # Task 10e: within a part, relevance alone decides; a question about
    # each book is already one part per book. Book fairness is kept only on
    # the no-decomposition fallback -- decomposition failed or was not run,
    # or one part was searched across books -- so Task 6's guarantee that a
    # cross-book answer keeps both books survives there.
    book_fair = len(parts) == 1 and (not decomposed or "across_books" in templates)
    evidence = assemble_ranked(conn, rows, extra_chunks=extra,
                               max_chars=models.evidence_chars,
                               score_lists=[relevance(v, stored) for _, v in parts],
                               book_fair=book_fair)
    # One synthesis call per part, over its own share (synthesize_parts).
    if rows:
        answer = await _synthesize(question, evidence, models, mode="graph",
                                   template="+".join(templates), book_titles=titles,
                                   sub_questions=sub_questions)
    else:
        answer = await _synthesize(question, evidence, models, mode="vector-fallback",
                                   note=GRAPH_EMPTY_NOTE, book_titles=titles,
                                   sub_questions=sub_questions)
    # The subgraph the UI draws: the evidence edges rows came from, and the
    # entities every part seeded from. Not populated on the empty/no-seed
    # early returns above -- there is no graph evidence or seed there, and
    # the Answer defaults (`()`) are already the honest value.
    return replace(answer, selection_model="+".join(chosen_by) or None,
                   sub_questions=sub_questions, edges=evidence.edges,
                   seeds=tuple(all_seeds))
