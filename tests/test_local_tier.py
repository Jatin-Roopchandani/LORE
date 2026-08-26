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
