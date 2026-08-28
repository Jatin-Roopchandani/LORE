import json
from datetime import datetime
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

from lore.config import OllamaConfig
from lore.extract.embed import write_unit_outputs
from lore.extract.runner import ChunkResult, DeadLetter, ExtractionResult
from lore.extract.schemas import SCHEMA_VERSION, ChunkExtraction
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
    started_at = "2026-08-25T00:00:00+00:00"
    write_unit_outputs(
        tmp_path,
        unit_id=unit.unit_id,
        book_id=BOOK,
        chunk_ids=[chunk.id for chunk in unit.chunks],
        result=result,
        vectors=[[0.0] * 768, [0.1] * 768],
        model="local-3b", embed_model="nomic-embed-text", gpu_seconds=12.0,
        schema_version=SCHEMA_VERSION,
        started_at=started_at,
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["chunks_in"] == 2
    assert manifest["chunks_ok"] == 1
    assert manifest["chunks_dead"] == 1
    assert manifest["schema_version"] == SCHEMA_VERSION
    # FIX 2: manifest carries both timestamps, both parseable ISO-8601 UTC.
    assert manifest["started_at"] == started_at
    datetime.fromisoformat(manifest["started_at"])
    finished = datetime.fromisoformat(manifest["finished_at"])
    assert finished >= datetime.fromisoformat(started_at)
    triplets = pq.read_table(tmp_path / "triplets.parquet")
    assert triplets.num_rows == 1
    assert triplets.column("predicate").to_pylist() == ["LOVES"]
    # FIX 1: book_id travels with every triplet row.
    assert triplets.column("book_id").to_pylist() == [BOOK]
    # FIX 3: entity_kinds is index-aligned with entity_names.
    assert triplets.column("entity_names").to_pylist() == [["Darcy"]]
    assert triplets.column("entity_kinds").to_pylist() == [["Character"]]
    assert pq.read_table(tmp_path / "embeddings.parquet").num_rows == 2
    dead = (tmp_path / "dead_letter.jsonl").read_text().strip().splitlines()
    assert json.loads(dead[0])["chunk_id"] == f"{BOOK}:1:1"
