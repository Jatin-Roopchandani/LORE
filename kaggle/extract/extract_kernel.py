"""LORE extraction kernel — runs on Kaggle T4s, never locally.

Installs vLLM, loads the AWQ model once per GPU, and drives the SAME
lore.extract.runner the local tier uses, so prompts, schema validation,
retries and dead-lettering cannot drift between tiers.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

MODEL_ID = "MODEL_ID"
EMBED_MODEL_ID = "EMBED_MODEL_ID"
VLLM_SPEC = "VLLM_SPEC"
OUT = Path("/kaggle/working")


def _install() -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", VLLM_SPEC,
         "sentence-transformers", "pyarrow", "pydantic>=2.7"],
        check=True,
    )


def _find_input() -> Path:
    """Kaggle mounts datasets under /kaggle/input/datasets/<owner>/<slug>/;
    locate by search rather than a hardcoded path."""
    matches = sorted(Path("/kaggle/input").rglob("unit.jsonl"))
    if not matches:
        raise FileNotFoundError("unit.jsonl not found under /kaggle/input")
    return matches[0].parent


def main() -> None:
    started = time.time()
    _install()
    unit_dir = _find_input()
    sys.path.insert(0, str(unit_dir))  # the shipped `lore` package

    from lore.extract.runner import ChunkInput, extract_chunks
    from lore.extract.schemas import ChunkExtraction

    rows = [
        json.loads(line)
        for line in (unit_dir / "unit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    unit_manifest = json.loads((unit_dir / "unit-manifest.json").read_text())
    chunks = [ChunkInput(chunk_id=r["chunk_id"], text=r["text"]) for r in rows]

    from vllm import LLM, SamplingParams
    from vllm.sampling_params import GuidedDecodingParams

    llm = LLM(
        model=MODEL_ID,
        quantization="awq",
        dtype="float16",
        gpu_memory_utilization=0.90,
        max_model_len=4096,
    )
    guided = GuidedDecodingParams(json=ChunkExtraction.model_json_schema())
    params = SamplingParams(
        temperature=0.0, max_tokens=1024, guided_decoding=guided
    )

    class VllmProvider:
        """Batches the whole unit through vLLM, then serves the runner
        one validated result per chunk from that batch."""

        def __init__(self) -> None:
            self._answers: dict[str, str] = {}

        def prime(self, prompts: dict[str, str]) -> None:
            ordered = list(prompts.items())
            outputs = llm.generate([p for _, p in ordered], params)
            for (chunk_id, _), output in zip(ordered, outputs):
                self._answers[chunk_id] = output.outputs[0].text
            self._by_prompt = {p: cid for cid, p in ordered}

        async def generate(self, prompt, schema):
            chunk_id = self._by_prompt[prompt]
            return schema.model_validate_json(self._answers[chunk_id])

    from lore.extract.prompts import build_prompt

    provider = VllmProvider()
    provider.prime({c.chunk_id: build_prompt(c.text) for c in chunks})
    result = asyncio.run(extract_chunks(chunks, provider))

    from sentence_transformers import SentenceTransformer

    embedder = SentenceTransformer(EMBED_MODEL_ID, trust_remote_code=True)
    vectors = embedder.encode(
        [c.text for c in chunks], normalize_embeddings=True, batch_size=32
    ).tolist()

    import pyarrow as pa
    import pyarrow.parquet as pq

    triplet_rows = {k: [] for k in (
        "chunk_id", "subject", "predicate", "object", "evidence",
        "confidence", "entity_names", "extractor_model", "run_id",
    )}
    for item in result.results:
        names = [e.name for e in item.extraction.entities]
        for triplet in item.extraction.triplets:
            triplet_rows["chunk_id"].append(item.chunk_id)
            triplet_rows["subject"].append(triplet.subject)
            triplet_rows["predicate"].append(triplet.predicate)
            triplet_rows["object"].append(triplet.object)
            triplet_rows["evidence"].append(triplet.evidence)
            triplet_rows["confidence"].append(triplet.confidence)
            triplet_rows["entity_names"].append(names)
            triplet_rows["extractor_model"].append(MODEL_ID)
            triplet_rows["run_id"].append(unit_manifest["unit_id"])
    pq.write_table(pa.table(triplet_rows), OUT / "triplets.parquet")
    pq.write_table(
        pa.table({
            "chunk_id": [c.chunk_id for c in chunks],
            "vector": vectors,
            "embed_model": [EMBED_MODEL_ID] * len(chunks),
        }),
        OUT / "embeddings.parquet",
    )
    with (OUT / "dead_letter.jsonl").open("w") as handle:
        for dead in result.dead_letters:
            handle.write(json.dumps({
                "chunk_id": dead.chunk_id,
                "raw_output": dead.raw_output,
                "error": dead.error,
            }) + "\n")
    (OUT / "manifest.json").write_text(json.dumps({
        "unit_id": unit_manifest["unit_id"],
        "chunks_in": len(chunks),
        "chunks_ok": len(result.results),
        "chunks_dead": len(result.dead_letters),
        "gpu_seconds": time.time() - started,
        "model": MODEL_ID,
        "embed_model": EMBED_MODEL_ID,
        "schema_version": unit_manifest["schema_version"],
    }, indent=2))
    print(json.dumps({
        "ok": len(result.results), "dead": len(result.dead_letters),
        "seconds": round(time.time() - started, 1),
    }))


main()
