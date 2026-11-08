"""Concurrent processing requests (lore/orchestrator/concurrency.py): the bound
holds, results land in a deterministic order, and every stop, resume and
error path of the sequential run still holds. No network: a fake text model
with random delays stands in for Claude/OpenAI/Gemini."""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.extract.runner import ExtractionHalted
from lore.inference import key_scope
from lore.inference.gemini import GeminiCreditError, GeminiOutputError
from lore.inference.providers.base import ProviderKeyError, ProviderQuotaError
from lore.ingest.models import Chunk
from lore.orchestrator.concurrency import bounded_fetch, llm_concurrency
from lore.orchestrator.local import (
    GeminiTier,
    QuotaHalted,
    parts_dir,
    parts_done,
    run_unit_gemini,
)
from lore.orchestrator.pipeline import QuotaStop
from lore.orchestrator.workunit import pack_units

BOOK = "abcdef0123456789"


def _config(provider: str = "anthropic", *, paid: bool = False, **processing) -> LoreConfig:
    base = LoreConfig.load()
    return base.model_copy(update={
        "llm": base.llm.model_copy(update={"provider": provider, "gemini_paid": paid}),
        "processing": base.processing.model_copy(update=processing),
    })


def _units(n_chunks: int, size: int = 10):
    chunks = [Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1, scene_seq=0,
                    seq=i, text=f"passage number {i:03d}") for i in range(n_chunks)]
    return pack_units(BOOK, chunks, size=size)


def _who(prompt: str) -> int:
    marker = "passage number "
    at = prompt.rindex(marker) + len(marker)
    return int(prompt[at:at + 3])


class FakeModel:
    """A TextModel: random delays, tracks how many requests are in flight.

    `dead`: chunk numbers whose every answer is unusable (dead letters).
    `fail`: {chunk number: exception} raised for that chunk's request.
    """

    models = ("fake-model",)

    def __init__(self, *, seed: int = 0, delay: float = 0.01, dead=(), fail=None,
                 timeline: list | None = None) -> None:
        self._random = random.Random(seed)
        self._delay = delay
        self.dead = set(dead)
        self.fail = dict(fail or {})
        self.timeline = timeline if timeline is not None else []
        self.in_flight = 0
        self.peak = 0
        self.asked: list[int] = []
        self.finished: list[int] = []
        self.last_model: str | None = None
        self.requests_made = 0
        self.attempts: dict[str, int] = {}

    async def generate_json(self, prompt, schema, *, exhausted_hint=None, avoid=None):
        who = _who(prompt)
        self.asked.append(who)
        self.timeline.append(("ask", who))
        self.requests_made += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self._random.uniform(0, self._delay))
            if who in self.fail:
                self.timeline.append(("fail", who))
                raise self.fail[who]
            self.last_model = "fake-model"
            if who in self.dead:
                raise GeminiOutputError(f"unusable {who}", f"raw {who}", "fake-model")
            return schema.model_validate({
                "entities": [{"name": f"E{who}", "kind": "Character"}],
                "triplets": [{"subject": f"E{who}", "predicate": "KNOWS",
                              "object": f"F{who}", "evidence": f"ev {who}",
                              "confidence": 0.5}],
            })
        finally:
            self.in_flight -= 1
            self.finished.append(who)
            self.timeline.append(("answer", who))

    async def aclose(self):
        return None


class FakeEmbedder:
    def __init__(self, delay: float = 0.0, fail: Exception | None = None,
                 timeline: list | None = None) -> None:
        self.delay = delay
        self.fail = fail
        self.calls: list[list[str]] = []
        self.in_flight = 0
        self.peak = 0
        self.log = timeline if timeline is not None else []

    async def embed(self, texts, model):
        self.calls.append(list(texts))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.log.append(("embed", texts[0]))
        try:
            await asyncio.sleep(self.delay)
            if self.fail is not None:
                raise self.fail
            return [[float(len(t)), 1.0] + [0.0] * 766 for t in texts]
        finally:
            self.in_flight -= 1
            self.log.append(("embedded", texts[0]))

    async def aclose(self):
        return None


def _files(dest: Path) -> dict[str, bytes]:
    out = {}
    for path in sorted(dest.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(dest))
            data = path.read_bytes()
            if rel == "manifest.json":
                body = json.loads(data)
                for timing in ("gpu_seconds", "started_at", "finished_at"):
                    body.pop(timing)
                data = json.dumps(body, sort_keys=True).encode()
            out[rel] = data
    return out


# --- the settings ----------------------------------------------------------

def test_free_tier_gemini_stays_sequential_and_billed_providers_get_four() -> None:
    assert llm_concurrency(_config("gemini")) == 1
    assert llm_concurrency(_config("gemini", paid=True)) == 4
    assert llm_concurrency(_config("anthropic")) == 4
    assert llm_concurrency(_config("openai")) == 4


def test_the_concurrency_is_configurable() -> None:
    assert llm_concurrency(_config("anthropic", llm_concurrency=2)) == 2
    assert llm_concurrency(_config("gemini", gemini_free_concurrency=3)) == 3


def test_lore_toml_sets_the_concurrency(tmp_path: Path) -> None:
    path = tmp_path / "lore.toml"
    path.write_text("[lore.processing]\nllm_concurrency = 6\n")
    assert LoreConfig.load(path).processing.llm_concurrency == 6
    path.write_text("[lore.processing]\nllm_concurrency = 0\n")
    with pytest.raises(ValueError):
        LoreConfig.load(path)


def test_a_bound_user_on_free_gemini_is_sequential() -> None:
    config = _config("anthropic")
    scope = key_scope.KeyScope(user_id="u1", key="AIza-x", provider="gemini")
    with key_scope.bound(scope):
        assert llm_concurrency(config) == 1


# --- bounded_fetch -----------------------------------------------------------

@pytest.mark.asyncio
async def test_bounded_fetch_never_exceeds_the_bound_and_keeps_each_result() -> None:
    rng = random.Random(3)
    live = peak = 0

    async def fetch(key: int) -> int:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(rng.uniform(0, 0.01))
        live -= 1
        return key * key

    async with bounded_fetch(range(40), fetch, 4) as futures:
        results = [await futures[k] for k in range(40)]
    assert results == [k * k for k in range(40)]
    assert peak == 4


@pytest.mark.asyncio
async def test_bounded_fetch_stops_starting_after_a_failure_and_finishes_in_flight() -> None:
    started: list[int] = []
    finished: list[int] = []

    async def fetch(key: int) -> int:
        started.append(key)
        await asyncio.sleep(0.02 if key != 2 else 0.001)
        if key == 2:
            raise RuntimeError("stop")
        finished.append(key)
        return key

    with pytest.raises(RuntimeError, match="stop"):
        async with bounded_fetch(range(10), fetch, 3) as futures:
            for k in range(10):
                await futures[k]
    # 0, 1 and 2 started together; 2's failure stopped the queue before any
    # other key started, and 0 and 1, already in flight, still finished.
    assert started == [0, 1, 2]
    assert sorted(finished) == [0, 1]
    assert all(futures[k].cancelled() for k in range(3, 10))


# --- extraction --------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [0, 1, 2])
async def test_concurrent_extraction_is_byte_identical_to_sequential(tmp_path, seed) -> None:
    unit = _units(10)[0]
    config = _config("anthropic")
    dead = {3, 7}
    sequential = FakeModel(seed=seed, dead=dead)
    await run_unit_gemini(config, unit, tmp_path / "seq", client=sequential,
                          embedder=FakeEmbedder(), concurrency=1)
    concurrent = FakeModel(seed=seed + 100, dead=dead)
    await run_unit_gemini(config, unit, tmp_path / "con", client=concurrent,
                          embedder=FakeEmbedder(), concurrency=4)
    assert sequential.peak == 1
    assert concurrent.peak == 4
    # The answers really did come back out of order...
    assert concurrent.finished != sorted(concurrent.finished)
    # ...and every artifact and part is the same, byte for byte.
    assert _files(tmp_path / "seq") == _files(tmp_path / "con")
    # Dead letters keep the old path: retried once, then dead-lettered.
    assert concurrent.asked.count(3) == 2 and concurrent.asked.count(0) == 1
    lines = (tmp_path / "con" / "dead_letter.jsonl").read_text().splitlines()
    assert [json.loads(line)["chunk_id"] for line in lines] == [
        unit.chunks[3].id, unit.chunks[7].id]


@pytest.mark.asyncio
async def test_the_bound_comes_from_the_provider(tmp_path) -> None:
    unit = _units(10)[0]
    for provider, peak in (("gemini", 1), ("anthropic", 4)):
        model = FakeModel()
        await run_unit_gemini(_config(provider), unit, tmp_path / provider,
                              client=model, embedder=FakeEmbedder())
        assert model.peak == peak, provider


@pytest.mark.asyncio
async def test_before_request_runs_before_every_request_and_progress_is_monotonic(
        tmp_path) -> None:
    unit = _units(10)[0]
    model = FakeModel(dead={4})
    checks: list[int] = []
    progress: list[int] = []

    def before() -> None:
        checks.append(model.requests_made)

    await run_unit_gemini(_config(), unit, tmp_path, client=model, embedder=FakeEmbedder(),
                          before_request=before, on_chunk=progress.append,
                          concurrency=4)
    # One check per request, the retry of the dead chunk included, and each
    # one before its request was sent.
    assert len(checks) == model.requests_made == 11
    assert sorted(checks) == list(range(11))
    assert progress == list(range(11))


@pytest.mark.asyncio
async def test_a_spend_stop_with_requests_in_flight_saves_what_returned(tmp_path) -> None:
    unit = _units(10)[0]
    model = FakeModel(delay=0.02)
    calls = 0
    stopped, whole = tmp_path / "stopped", tmp_path / "whole"

    def before() -> None:
        nonlocal calls
        calls += 1
        if calls == 6:
            raise QuotaStop("reserve reached")

    with pytest.raises(QuotaHalted, match="reserve reached"):
        await run_unit_gemini(_config(), unit, stopped, client=model,
                              embedder=FakeEmbedder(), before_request=before,
                              concurrency=4)
    # Five requests were sent; all five finished and were saved; nothing
    # else started after the stop; no unit artifacts were written.
    assert model.requests_made == 5
    assert parts_done(stopped, unit) == 5
    assert not (stopped / "manifest.json").exists()
    for part in parts_dir(stopped).iterdir():
        json.loads(part.read_text())  # whole, never half-written

    # The resume asks only for the five missing chunks.
    again = FakeModel()
    await run_unit_gemini(_config(), unit, stopped, client=again,
                          embedder=FakeEmbedder(), concurrency=4)
    assert sorted(again.asked) == sorted(set(range(10)) - set(model.asked))
    assert len(again.asked) == 5

    # And the result equals an uninterrupted run.
    await run_unit_gemini(_config(), unit, whole, client=FakeModel(),
                          embedder=FakeEmbedder(), concurrency=4)
    assert _files(stopped) == _files(whole)


@pytest.mark.asyncio
@pytest.mark.parametrize("error, halt", [
    (ProviderQuotaError("429", provider="anthropic"), QuotaHalted),
    (GeminiCreditError("402"), QuotaHalted),
    (ProviderKeyError("401", provider="anthropic"), ExtractionHalted),
])
async def test_a_provider_error_on_a_concurrent_chunk_follows_the_old_path(
        tmp_path, error, halt) -> None:
    unit = _units(10)[0]
    model = FakeModel(fail={5: error})
    with pytest.raises(halt) as caught:
        await run_unit_gemini(_config(), unit, tmp_path, client=model,
                              embedder=FakeEmbedder(), concurrency=4)
    assert type(caught.value) is halt
    assert caught.value.__cause__ is error  # the 402 and quota mapping is kept
    assert not (parts_dir(tmp_path) / f"{unit.chunks[5].id}.json").exists()
    # Never dead-lettered, never retried, and no request started after it;
    # the ones already in flight were saved.
    assert model.asked.count(5) == 1
    failed_at = model.timeline.index(("fail", 5))
    assert not [e for e in model.timeline[failed_at:] if e[0] == "ask"]
    assert parts_done(tmp_path, unit) == len(model.asked) - 1
    assert not (tmp_path / "dead_letter.jsonl").exists()


# --- overlapping the embedding with the extraction ---------------------------

def test_the_next_unit_embeds_while_this_one_extracts(tmp_path) -> None:
    config = _config("anthropic").model_copy(update={"data_dir": tmp_path})
    units = _units(30)
    timeline: list = []
    model = FakeModel(delay=0.05, timeline=timeline)
    embedder = FakeEmbedder(delay=0.05, timeline=timeline)
    tier = GeminiTier(config=config, client=model, embedder=embedder)
    with asyncio.Runner() as loop:
        try:
            for i, unit in enumerate(units):
                nxt = units[i + 1] if i + 1 < len(units) else None
                loop.run(tier.run(unit, tmp_path / unit.unit_id, next_unit=nxt))
        finally:
            loop.run(tier.aclose())
    # One embed per unit, never two at once, never repeated.
    assert len(embedder.calls) == 3 and embedder.peak == 1
    assert [c[0] for c in embedder.calls] == [
        f"search_document: {u.chunks[0].text}" for u in units]
    # Unit n+1's embed starts once unit n's own embed is done, before unit
    # n's chunks are answered, and unit n's requests only after its embed.
    for n in (0, 1):
        this, nxt = units[n], units[n + 1]
        embedded = timeline.index(("embedded", f"search_document: {this.chunks[0].text}"))
        next_embed = timeline.index(("embed", f"search_document: {nxt.chunks[0].text}"))
        numbers = {int(c.text[-3:]) for c in this.chunks}
        asks = [i for i, e in enumerate(timeline) if e[0] == "ask" and e[1] in numbers]
        answers = [i for i, e in enumerate(timeline) if e[0] == "answer" and e[1] in numbers]
        assert embedded < min(asks)
        assert embedded < next_embed < max(answers)
    assert all((tmp_path / u.unit_id / "manifest.json").exists() for u in units)


def test_a_unit_whose_embedding_failed_spends_no_request(tmp_path) -> None:
    config = _config("anthropic").model_copy(update={"data_dir": tmp_path})
    units = _units(20)
    model = FakeModel()

    class FailSecond(FakeEmbedder):
        async def embed(self, texts, model):
            if len(self.calls) == 1:
                self.calls.append(list(texts))
                raise ConnectionError("ollama stopped")
            return await super().embed(texts, model)

    tier = GeminiTier(config=config, client=model, embedder=FailSecond())
    with asyncio.Runner() as loop:
        try:
            loop.run(tier.run(units[0], tmp_path / "u0", next_unit=units[1]))
            asked = len(model.asked)
            with pytest.raises(ExtractionHalted, match="no Gemini request was spent"):
                loop.run(tier.run(units[1], tmp_path / "u1"))
            assert len(model.asked) == asked == 10
        finally:
            loop.run(tier.aclose())


def test_a_stopped_run_cancels_the_prefetched_embedding(tmp_path) -> None:
    config = _config("anthropic").model_copy(update={"data_dir": tmp_path})
    units = _units(20)
    embedder = FakeEmbedder(delay=0.2)
    tier = GeminiTier(config=config, client=FakeModel(fail={2: ProviderQuotaError(
        "429", provider="anthropic")}), embedder=embedder)
    with asyncio.Runner() as loop:
        try:
            with pytest.raises(QuotaHalted):
                loop.run(tier.run(units[0], tmp_path / "u0", next_unit=units[1]))
        finally:
            loop.run(tier.aclose())
    assert embedder.in_flight == 0
