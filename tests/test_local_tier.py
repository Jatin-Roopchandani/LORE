from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.ingest.models import Chunk
from lore.orchestrator.local import run_unit_local
from lore.orchestrator.workunit import pack_units, validate_output

BOOK = "abcdef0123456789"


class FakeLocalProvider:
    async def generate(self, prompt, schema):
        return schema.model_validate({
            "entities": [{"name": "X", "kind": "Character"}],
            "triplets": [{
                "subject": "X", "predicate": "KNOWS", "object": "Y",
                "evidence": "e", "confidence": 0.5,
            }],
        })

    async def embed(self, texts, model):
        return [[0.0] * 768 for _ in texts]

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_local_unit_produces_kernel_shaped_output(tmp_path: Path) -> None:
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
              scene_seq=0, seq=i, text=f"text {i}")
        for i in range(3)
    ]
    unit = pack_units(BOOK, chunks, size=10)[0]
    config = LoreConfig.load()
    summary = await run_unit_local(
        config, unit, tmp_path, provider=FakeLocalProvider()
    )
    assert summary.chunks_ok == 3
    # The SPEC parity requirement: local output validates against the very
    # same checker the Kaggle path uses.
    assert validate_output(unit, tmp_path).chunks_ok == 3


# --- Task R1: extraction_provider = "gemini" --------------------------------

import json  # noqa: E402

import httpx  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from lore.extract.embed import DOCUMENT_PREFIX  # noqa: E402
from lore.extract.prompts import build_prompt  # noqa: E402
from lore.extract.runner import ExtractionHalted  # noqa: E402
from lore.inference.gemini import LITE_MODELS, GeminiClient  # noqa: E402
from lore.orchestrator.local import run_unit_gemini  # noqa: E402

SECRET = "AIza-not-a-real-key-0000"
GOOD = json.dumps({
    "entities": [{"name": "Holmes", "kind": "Character"}],
    "triplets": [{"subject": "Holmes", "predicate": "INTERACTS_WITH",
                  "object": "Watson", "evidence": "e", "confidence": 0.9}],
})


def _body(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _model_of(request: httpx.Request) -> str:
    return str(request.url).rsplit("/", 1)[-1].split(":")[0]


class FakeEmbedder:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.fail = fail

    async def embed(self, texts, model):
        self.calls.append((list(texts), model))
        if self.fail is not None:
            raise self.fail
        return [[0.0] * 768 for _ in texts]

    async def aclose(self):
        return None


def _unit(n: int = 3):
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
              scene_seq=0, seq=i, text=f"passage number {i}")
        for i in range(n)
    ]
    return pack_units(BOOK, chunks, size=10)[0]


def _gemini(handler, models=LITE_MODELS) -> GeminiClient:
    async def no_sleep(_seconds: float) -> None:
        return None

    return GeminiClient(SECRET, models=list(models),
                        transport=httpx.MockTransport(handler), sleep=no_sleep)


@pytest.mark.asyncio
async def test_gemini_unit_sends_one_chunk_per_request_and_embeds_locally(
    tmp_path: Path,
) -> None:
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content)["contents"][0]["parts"][0]["text"])
        return httpx.Response(200, json=_body(GOOD))

    unit = _unit(3)
    config = LoreConfig.load()
    client, embedder = _gemini(handler), FakeEmbedder()
    summary = await run_unit_gemini(config, unit, tmp_path, client=client,
                                    embedder=embedder)
    await client.aclose()

    assert summary.chunks_ok == 3 and summary.chunks_dead == 0
    assert validate_output(unit, tmp_path).chunks_ok == 3
    # One chunk per request, with the unchanged extraction prompt.
    assert client.requests_made == 3
    for chunk, prompt in zip(unit.chunks, prompts):
        assert prompt.startswith(build_prompt(chunk.text))
    # Embeddings stay local: nomic on Ollama with the document prefix, the
    # same embedder the query side uses.
    assert embedder.calls == [
        ([DOCUMENT_PREFIX + c.text for c in unit.chunks],
         config.extraction.embed_model_ollama)
    ]
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["model"] == "gemini-3.1-flash-lite"
    assert manifest["embed_model"] == config.extraction.embed_model_ollama
    rows = pq.read_table(tmp_path / "triplets.parquet").to_pylist()
    assert {row["extractor_model"] for row in rows} == {"gemini-3.1-flash-lite"}


@pytest.mark.asyncio
async def test_gemini_manifest_names_every_model_that_answered(tmp_path: Path) -> None:
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        # The second chunk's first model is out of budget; 3.5 answers it.
        if calls[0] == 2 and _model_of(request) == LITE_MODELS[0]:
            return httpx.Response(429, text="nope")
        return httpx.Response(200, json=_body(GOOD))

    unit = _unit(3)
    client = _gemini(handler)
    await run_unit_gemini(LoreConfig.load(), unit, tmp_path, client=client,
                          embedder=FakeEmbedder())
    await client.aclose()
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["model"] == "gemini-3.1-flash-lite,gemini-3.5-flash-lite"


@pytest.mark.asyncio
async def test_bad_gemini_output_still_retries_then_dead_letters(tmp_path: Path) -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        # chunk 0 gets garbage twice; chunks 1 and 2 are fine.
        return httpx.Response(200, json=_body("{broken" if len(seen) <= 2 else GOOD))

    unit = _unit(3)
    client = _gemini(handler)
    summary = await run_unit_gemini(LoreConfig.load(), unit, tmp_path,
                                    client=client, embedder=FakeEmbedder())
    await client.aclose()
    assert client.requests_made == 4
    assert (summary.chunks_ok, summary.chunks_dead) == (2, 1)
    dead = [json.loads(line) for line in
            (tmp_path / "dead_letter.jsonl").read_text().splitlines()]
    assert [d["chunk_id"] for d in dead] == [unit.chunks[0].id]
    assert dead[0]["raw_output"] == "{broken"


@pytest.mark.asyncio
async def test_an_exhausted_cascade_halts_the_unit_and_writes_nothing(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "RESOURCE_EXHAUSTED"}})

    client = _gemini(handler)
    with pytest.raises(ExtractionHalted) as exc:
        await run_unit_gemini(LoreConfig.load(), _unit(3), tmp_path,
                              client=client, embedder=FakeEmbedder())
    await client.aclose()
    message = str(exc.value)
    assert "429" in message and "budget" in message
    assert "--resume" in message
    assert SECRET not in message
    # One request per model for the first chunk, then stop: no retry, no
    # further chunks, no dead letters, no artifacts.
    assert client.requests_made == len(LITE_MODELS)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_a_rejected_key_halts_loudly(tmp_path: Path, status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="denied")

    client = _gemini(handler)
    with pytest.raises(ExtractionHalted) as exc:
        await run_unit_gemini(LoreConfig.load(), _unit(2), tmp_path,
                              client=client, embedder=FakeEmbedder())
    await client.aclose()
    assert "rejected" in str(exc.value)
    assert client.requests_made == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_dead_embedder_halts_before_any_gemini_request(tmp_path: Path) -> None:
    # Embedding first means a stopped Ollama costs no daily quota.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json=_body(GOOD))

    client = _gemini(handler)
    embedder = FakeEmbedder(fail=httpx.ConnectError("refused"))
    with pytest.raises(ExtractionHalted) as exc:
        await run_unit_gemini(LoreConfig.load(), _unit(2), tmp_path,
                              client=client, embedder=embedder)
    await client.aclose()
    assert client.requests_made == 0
    assert "embed" in str(exc.value)
