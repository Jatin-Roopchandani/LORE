# LORE Phase 2A — Extraction Pipeline Design

**Status:** approved in chat 2026-08-25. Implements SPEC.md §9 Phase 2 (first half).

**One sentence:** `lore extract <book>` turns a chunked book into validated relationship triplets and embeddings on disk, running bulk inference on free Kaggle T4s, resumable across killed sessions, and reproducible through local Ollama with a one-line config change.

---

## 1. Why Phase 2 is split

SPEC §9's Phase 2 bundles five independent subsystems. This spec covers **2A — the extraction pipeline**, whose deliverable is inspectable data files. **2B** (entity resolution, transactional Kuzu/LanceDB load, `lore process`) gets its own spec once 2A's output exists and can be eyeballed.

The split matters for a practical reason: 2A's output is the input to every downstream design decision in 2B (how noisy are the triplets? how many alias candidates? what confidence distribution?). Designing resolution before seeing real extraction output would be guesswork.

## 2. Locked decisions

| Decision | Choice | Rationale |
|---|---|---|
| Extraction model | **Qwen2.5-7B-Instruct-AWQ** | ~5GB AWQ weights fit one T4 (16GB, sm_75, fp16-only) with room for KV cache → one worker per GPU, ~2× throughput. Qwen2.5 is strong at strict structured output, which is the entire job. |
| Kernel networking | **internet enabled** | Notebook pip-installs vLLM and pulls weights from HuggingFace at session start. Book text already crosses to Kaggle in this tier, so the privacy posture is unchanged. Costs ~3–5 min warmup per session. |
| Extraction scope | **entities + triplets only** | SPEC §5.3's minimum, and exactly what graph traversal consumes. No chapter-metadata or salience passes in 2A. |
| Decoding | **vLLM `guided_json`** (schema-forced) | Near-eliminates parse failures versus free-form JSON, so retries and dead-letters stay rare and meaningful. |
| Work-unit size | configurable, **default 250 chunks** | Calibrated from the first real run's measured throughput rather than guessed. |

## 3. Prerequisite (Task 0)

The Phase 1 final review parked one debt that must land before any extraction touches real data: `lore/ingest/chunker.py`'s isolation branch fires on raw length (`len(sentence) > budget_chars // 2`), stripping the 2-sentence overlap from ~70 genuine long-dialogue sentences per novel. Narrow it to fragments actually produced by `_split_oversized` (provenance-based), and restore a regression test exercising the carried-overlap path below `budget_chars // 2`.

**Why first:** chunk IDs become provenance keys on every extracted triplet the moment extraction runs. Changing chunking afterwards means re-extracting the whole library.

## 4. Architecture

```
  lore extract <book_id>
         │
         ▼
  ┌──────────────────┐   pending chunks (SQLite)
  │ Run Orchestrator │◀──────────────────────────┐
  │  run.py          │                           │
  └────┬─────────────┘                           │
       │ 1. pack work units (JSONL + manifest)   │ unit state
       │ 2. quota check ─────► quota.py          │ (jobs table)
       │ 3. push dataset ─┐                      │
       │ 4. wait ready    │  KaggleClient        │
       │ 5. push kernel   │  (Phase 0, reused)   │
       │ 6. poll ─────────┘                      │
       │ 7. pull + validate outputs ─────────────┘
       ▼
  data/extractions/<book_id>/{triplets,embeddings}.parquet
                              dead_letter.jsonl, manifest.json

  KAGGLE KERNEL (2×T4, internet on)
  extract_kernel.py  ── thin shim ──►  lore.extract.runner
     · pip install vllm; load Qwen2.5-7B-Instruct-AWQ
     · worker per GPU (data-parallel), guided_json decoding
     · nomic-embed-text-v1.5 embeddings (same model the query side uses)
     · writes Parquet + dead-letter + manifest (counts, timings, model id)

  TIER 2 (zero-egress): same runner, OllamaProvider, no Kaggle at all
```

The kernel is deliberately thin. All extraction logic — prompt construction, schema validation, retry, dead-lettering — lives in `lore/extract/runner.py`, which ships to Kaggle inside the work-unit dataset and is imported there. This is SPEC §3 rule 1 (single source of truth for extraction logic) made concrete: a prompt fix applies to both tiers with no duplication and no drift.

## 5. Modules

| File | Responsibility |
|---|---|
| `lore/extract/schemas.py` | pydantic contracts: `ExtractedEntity`, `ExtractedTriplet`, `ChunkExtraction`. The JSON schema handed to `guided_json`. |
| `lore/extract/prompts.py` | Extraction prompt template + few-shot examples; `build_prompt(chunk_text) -> str`. |
| `lore/extract/runner.py` | `extract_chunks(chunks, provider, on_progress) -> ExtractionResult`: per-chunk generate → validate → retry once → dead-letter. Provider-agnostic. |
| `lore/orchestrator/workunit.py` | `pack_units(chunks, size) -> list[WorkUnit]`; write JSONL + manifest; read/validate returned Parquet. |
| `lore/orchestrator/quota.py` | Weekly GPU-hour ledger on the existing `quota_ledger` table; `check_budget(est_hours)`, `record_actual(run_ref, hours)`. |
| `lore/orchestrator/run.py` | The dispatch state machine; `--resume` semantics; unit state in `jobs`. |
| `kaggle/extract/extract_kernel.py` | Session shim: install deps, spin workers, call the runner, write artifacts. |
| `kaggle/extract/kernel-metadata.json` | T4 pinned (`machine_shape: NvidiaTeslaT4`), `enable_internet: true`, private. |
| `lore/cli.py` | `lore extract <book_id> [--resume] [--provider ollama]`, `lore quota`. |

## 6. Data contracts

**Up (work unit dataset):** `unit.jsonl` — one row per chunk `{chunk_id, text, chapter_seq}`; `unit-manifest.json` — `{book_id, unit_id, chunk_count, model, schema_version}`; plus the `lore` package source so the kernel imports the same runner.

**Down (per unit):**
- `triplets.parquet` — `chunk_id, book_id, subject, predicate, object, evidence, confidence, entity_names[], extractor_model, run_id`
- `embeddings.parquet` — `chunk_id, vector(768), embed_model`
- `dead_letter.jsonl` — `{chunk_id, raw_output, error}` for chunks that failed validation twice
- `manifest.json` — `{unit_id, chunks_in, chunks_ok, chunks_dead, gpu_seconds, model, started_at, finished_at}`

**Acceptance rule:** a unit is accepted only if `chunks_ok + chunks_dead == chunks_in` and every `chunk_id` in the outputs belongs to the unit. Anything else re-queues the unit.

## 7. Resume semantics

Each work unit is a row in `jobs` with state `queued → dispatched → completed | failed`. A unit is idempotent: re-running it reproduces the same chunk set (chunk IDs are deterministic) and overwrites its own artifacts. `--resume` skips `completed` units, re-queues `dispatched` (Kaggle session died mid-run) and `failed` ones. No partial-unit state is ever trusted — validation happens before a unit is marked complete.

## 8. Testing

- **Unit:** schemas, prompt building, packing/validation, quota arithmetic, resume state transitions — all mocked, no GPU, no network.
- **Runner:** driven by a `FakeProvider` returning canned/malformed payloads to prove retry and dead-lettering.
- **Parity (SPEC-mandated):** one fixture work unit through `OllamaProvider` and through the kernel code path locally (stub model) — schema-identical output shapes.
- **Live (opt-in, `-m live`):** a real Kaggle run on a small unit, recording actual GPU-hours into the ledger.
- **Acceptance:** Pride & Prejudice's 738 chunks extracted end-to-end on free quota; manifest timings recorded; a killed session resumed successfully.

## 9. Risks

| Risk | Mitigation |
|---|---|
| vLLM install/version drift on Kaggle's image | Pin the vLLM version in the kernel; manifest records it; failure surfaces as a failed unit, not corrupt data |
| T4 fp16-only, no flash-attn 2 | AWQ + fp16 is the tested path for sm_75; `machine_shape` pins T4 so a P100 can never be assigned (Phase 0 lesson) |
| Session dies mid-unit | Unit-level idempotency + `--resume`; nothing is accepted without validation |
| 7B extraction quality below par | Dead-letter rate and confidence distribution are measured on the real run; prompt/few-shot iterated against them; 14B remains a config-level upgrade |
| Weekly quota exhaustion | Ledger refuses dispatch before it happens; work spreads across weeks |
| Embedding drift between tiers | Same pinned model id on both sides; recorded per run in the manifest |

## 10. Exit criteria

1. `uv run pytest -m 'not live'` green, output pristine.
2. `lore extract <book_id>` takes a real ingested novel to `triplets.parquet` + `embeddings.parquet` using only free Kaggle quota, hands-off.
3. Killing the Kaggle session mid-run loses no completed unit; `--resume` finishes the book.
4. `lore quota` reports GPU-hours used/remaining for the current ISO week, matching the manifests.
5. The same pipeline with `extraction.provider: ollama` completes end-to-end on a short fixture book, no other change.
6. Every commit authored `jatinroopchandani3007@gmail.com`, pushed to `origin/main` as it lands.
