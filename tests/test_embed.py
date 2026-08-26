import json
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

from lore.config import OllamaConfig
from lore.extract.embed import write_unit_outputs
from lore.extract.runner import ChunkResult, DeadLetter, ExtractionResult
from lore.extract.schemas import ChunkExtraction
from lore.ingest.models import Chunk
from lore.inference.ollama import OllamaProvider
from lore.orchestrator.workunit import pack_units

BOOK = "abcdef0123456789"


@pytest.mark.asyncio
async def test_embed_posts_and_returns_vectors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "nomic-embed-text"
        assert body["input"] == ["a", "b"]
        return httpx.Response(200, json={"embeddings": [[0.1] * 768, [0.2] * 768]})

    provider = OllamaProvider(OllamaConfig())
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=provider._client.base_url
    )
    vectors = await provider.embed(["a", "b"], model="nomic-embed-text")
    assert len(vectors) == 2
    assert len(vectors[0]) == 768


def test_write_unit_outputs_matches_the_kernel_contract(tmp_path: Path) -> None:
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
              scene_seq=0, seq=i, text=f"text {i}")
        for i in range(2)
    ]
    unit = pack_units(BOOK, chunks, size=10)[0]
    extraction = ChunkExtraction.model_validate({
        "entities": [{"name": "Darcy", "kind": "Character"}],
        "triplets": [{
            "subject": "Darcy", "predicate": "LOVES", "object": "Elizabeth",
            "evidence": "quote", "confidence": 0.9,
        }],
    })
    result = ExtractionResult(
        results=[ChunkResult(f"{BOOK}:1:0", extraction)],
        dead_letters=[DeadLetter(f"{BOOK}:1:1", "{bad", "invalid json")],
    )
    write_unit_outputs(
        unit, tmp_path, result,
        vectors=[[0.0] * 768, [0.1] * 768],
        model="local-3b", embed_model="nomic-embed-text", gpu_seconds=12.0,
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["chunks_in"] == 2
    assert manifest["chunks_ok"] == 1
    assert manifest["chunks_dead"] == 1
    triplets = pq.read_table(tmp_path / "triplets.parquet")
    assert triplets.num_rows == 1
    assert triplets.column("predicate").to_pylist() == ["LOVES"]
    assert pq.read_table(tmp_path / "embeddings.parquet").num_rows == 2
    dead = (tmp_path / "dead_letter.jsonl").read_text().strip().splitlines()
    assert json.loads(dead[0])["chunk_id"] == f"{BOOK}:1:1"
