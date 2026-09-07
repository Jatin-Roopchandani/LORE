"""LORE extraction kernel — runs on Kaggle T4s, never locally.

Installs vLLM, loads the AWQ model once per GPU, and drives the SAME
lore.extract.runner the local tier uses, so prompts, schema validation,
retries and dead-lettering cannot drift between tiers.
"""
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
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
    started_at = datetime.now(timezone.utc)
    started = time.time()
    _install()
    unit_dir = _find_input()
    sys.path.insert(0, str(unit_dir))  # the shipped `lore` package

    from lore.extract.runner import ChunkInput, extract_chunks
    from lore.extract.schemas import MAX_OUTPUT_TOKENS, ChunkExtraction
    # MAX_OUTPUT_TOKENS lives in lore/extract/schemas.py (imported from the
    # shipped `lore` package, see write_unit() in workunit.py) so this tier
    # and the Ollama tier cannot drift apart on the cap (SPEC §3 rule 1).
    # 4096 is measured against observed truncations of 6367-8329 raw
    # characters (~1800-2400 tokens) - see schemas.py for the full note.
    # Previously hardcoded here at 2048, which the comment on this line
    # once already raised from 1024 for the same reason it needed raising
    # again.

    rows = [
        json.loads(line)
        for line in (unit_dir / "unit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    unit_manifest = json.loads((unit_dir / "unit-manifest.json").read_text())
    chunks = [ChunkInput(chunk_id=r["chunk_id"], text=r["text"]) for r in rows]

    import vllm
    from vllm import LLM, SamplingParams
    import vllm.sampling_params as vsp

    print("vllm version:", getattr(vllm, "__version__", "unknown"), flush=True)

    llm = LLM(
        model=MODEL_ID,
        quantization="awq",
        dtype="float16",
        gpu_memory_utilization=0.90,
        max_model_len=4096,
    )

    schema_json = ChunkExtraction.model_json_schema()

    def _make_params(temperature: float):
        """vLLM renamed its structured-output API between major versions;
        pick whichever this image ships rather than pinning a name."""
        if hasattr(vsp, "StructuredOutputsParams"):
            return SamplingParams(
                temperature=temperature, max_tokens=MAX_OUTPUT_TOKENS,
                structured_outputs=vsp.StructuredOutputsParams(json=schema_json),
            )
        if hasattr(vsp, "GuidedDecodingParams"):
            return SamplingParams(
                temperature=temperature, max_tokens=MAX_OUTPUT_TOKENS,
                guided_decoding=vsp.GuidedDecodingParams(json=schema_json),
            )
        raise RuntimeError(
            "no structured-output API found in vllm "
            f"{getattr(vllm, '__version__', '?')}; available names: "
            + ", ".join(sorted(n for n in dir(vsp) if not n.startswith('_')))
        )

    # Used only when generate() is asked for a prompt a second time (a
    # runner retry, or two chunks sharing byte-identical text): a repeat at
    # temperature=0.0 would reproduce the same failing text and defeat the
    # retry, so re-generate live at a higher temperature instead.
    params = _make_params(0.0)
    retry_params = _make_params(0.2)

    class VllmProvider:
        """Batches the whole unit through vLLM in one pass, caching each
        prompt's completion for single use. The first request for a prompt
        is served from that batched cache; any further request for the
        same prompt text (a runner retry, or a duplicate-text chunk) is a
        genuine live re-generation at a raised temperature, so retries
        actually have a chance to recover."""

        def __init__(self) -> None:
            self._answers: dict[str, str] = {}

        def prime(self, prompts: dict[str, str]) -> None:
            ordered = list(prompts.items())
            outputs = llm.generate([p for _, p in ordered], params)
            for (_, prompt), output in zip(ordered, outputs):
                self._answers[prompt] = output.outputs[0].text

        async def generate(self, prompt, schema):
            from lore.inference.base import ProviderOutputError

            cached = self._answers.pop(prompt, None)
            if cached is not None:
                try:
                    return schema.model_validate_json(cached)
                except Exception as exc:
                    raise ProviderOutputError(str(exc), raw_output=cached) from exc
            text = llm.generate([prompt], retry_params)[0].outputs[0].text
            try:
                return schema.model_validate_json(text)
            except Exception as exc:
                raise ProviderOutputError(str(exc), raw_output=text) from exc

    from lore.extract.prompts import build_prompt

    provider = VllmProvider()
    provider.prime({c.chunk_id: build_prompt(c.text) for c in chunks})
    result = asyncio.run(extract_chunks(chunks, provider))

    from sentence_transformers import SentenceTransformer

    from lore.extract.embed import DOCUMENT_PREFIX, write_unit_outputs

    embedder = SentenceTransformer(EMBED_MODEL_ID, trust_remote_code=True)
    vectors = embedder.encode(
        [DOCUMENT_PREFIX + c.text for c in chunks],
        normalize_embeddings=True, batch_size=32,
    ).tolist()

    write_unit_outputs(
        dest=OUT,
        unit_id=unit_manifest["unit_id"],
        book_id=unit_manifest["book_id"],
        chunk_ids=[c.chunk_id for c in chunks],
        result=result,
        vectors=vectors,
        model=MODEL_ID,
        embed_model=EMBED_MODEL_ID,
        gpu_seconds=time.time() - started,
        schema_version=unit_manifest["schema_version"],
        started_at=started_at.isoformat(),
    )
    print(json.dumps({
        "ok": len(result.results), "dead": len(result.dead_letters),
        "seconds": round(time.time() - started, 1),
    }))


main()
