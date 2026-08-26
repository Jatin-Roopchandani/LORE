# LORE Phase 2A — Extraction Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `lore extract <book_id>` turns a chunked book into validated relationship triplets and embeddings on disk — bulk inference on free Kaggle T4s, resumable across killed sessions, and reproducible through local Ollama with one config line.

**Architecture:** All extraction logic (prompt, schema validation, retry, dead-letter) lives in `lore/extract/runner.py`, which is provider-agnostic. The Kaggle kernel is a thin shim that ships this package inside the work-unit dataset and imports it, so cloud and local tiers can never drift. The orchestrator packs chunks into idempotent work units, dispatches them through the Phase 0 `KaggleClient`, validates returned Parquet before accepting, and tracks unit state in SQLite for `--resume`.

**Tech Stack:** Python 3.12, uv, pydantic v2, pyarrow (Parquet), vLLM + Qwen2.5-7B-Instruct-AWQ (on Kaggle), Ollama (local tier + embeddings), typer, existing SQLite/KaggleClient from Phase 0.

**Spec:** `docs/superpowers/specs/2026-08-25-phase-2a-extraction-design.md` (and `SPEC.md` §3, §5, §9 Phase 2)

## Global Constraints

- Python **3.12** via **uv** (`uv run`, `uv add` — never bare pip).
- **Git identity (HARD GATE):** before EVERY commit, `git config user.email` must print exactly `jatinroopchandani3007@gmail.com`; anything else → STOP. Every commit message ends with the two trailer lines shown in Task 1 Step 6. **Push to `origin/main` after every commit** (`git push`) — standing user instruction.
- TDD per task: failing test → implement → pass. Full suite (`uv run pytest -m 'not live' -v`) green before each commit; output pristine (zero warnings).
- Tests use `tmp_path` and fakes; nothing touches `data/`, no network, no GPU. Live checks are `@pytest.mark.live` only.
- Embedding dimension is **768** everywhere (`EMBED_DIM` in `lore/db/lance_store.py`).
- Extraction model id: **`Qwen/Qwen2.5-7B-Instruct-AWQ`**. Embedding model: **`nomic-embed-text`** (Ollama) / **`nomic-ai/nomic-embed-text-v1.5`** (Kaggle) — both 768-dim.
- Kaggle kernel metadata must keep `"machine_shape": "NvidiaTeslaT4"` (a P100 crashes Kaggle's torch build) and set `"enable_internet": "true"` for this phase.
- Kaggle mounts datasets at `/kaggle/input/datasets/<owner>/<slug>/` — **always locate files by `rglob`**, never a hardcoded mount path.
- Chunk IDs are provenance keys from this phase on. Task 1 is the last chunking change.

---

### Task 1: Chunker — bounded packs with overlap preserved (parked Phase 1 debt)

**Files:**
- Modify: `lore/ingest/chunker.py` (the `_pack` function and its docstring)
- Test: `tests/test_chunker.py` (add three tests)

**Interfaces:**
- Consumes: nothing new.
- Produces: `chunk_book(book, book_id, budget_chars=1600, overlap_sentences=2) -> list[Chunk]` — unchanged signature, two strengthened guarantees: (a) **every** chunk is ≤ `budget_chars`; (b) consecutive chunks in a scene share overlap whenever the overlap fits in the budget.

**Design note for the implementer and reviewer:** the spec prescribed provenance-tracking (isolate only fragments produced by `_split_oversized`). This task supersedes that with a strictly better mechanism: **trim the carried overlap until the incoming sentence fits**. That makes packs unconditionally bounded *and* keeps overlap for real long sentences, so the `len(sentence) > budget_chars // 2` isolation branch is deleted outright rather than narrowed. Same goal, less machinery.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chunker.py`:
```python
def test_every_chunk_respects_budget_even_with_large_sentences() -> None:
    # Two near-budget real sentences used to compound through overlap.
    big_a = "A" * 900 + "."
    big_b = "B" * 900 + "."
    book = _book([("One", f"{big_a} {big_b}")])
    for chunk in chunk_book(book, BOOK_ID):
        assert len(chunk.text) <= 1600


def test_long_sentences_still_participate_in_overlap() -> None:
    # A 900-char sentence followed by short ones must not be isolated:
    # the short sentences that follow it share a chunk with it.
    big = "A" * 900 + "."
    tail = " ".join(f"Short sentence number {i}." for i in range(6))
    book = _book([("One", f"{big} {tail}")])
    chunks = chunk_book(book, BOOK_ID)
    first = chunks[0].text
    assert "A" * 900 in first
    assert "Short sentence number 0." in first  # packed together, not isolated


def test_overlap_survives_below_half_budget_boundary() -> None:
    # Restores coverage of the carried-overlap path for sentences just
    # under half the budget, which the old isolation branch intercepted.
    sentences = " ".join("C" * 700 + "." for _ in range(4))
    book = _book([("One", sentences)])
    chunks = chunk_book(book, BOOK_ID)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk.text) <= 1600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chunker.py -v`
Expected: `test_long_sentences_still_participate_in_overlap` FAILS (the 900-char sentence is currently isolated into its own pack, so `"Short sentence number 0."` is not in the first chunk).

- [ ] **Step 3: Rewrite `_pack` in `lore/ingest/chunker.py`**

Replace the whole `_pack` function (docstring included) with:
```python
def _pack(
    sentences: list[str], budget_chars: int, overlap_sentences: int
) -> list[list[str]]:
    """Greedily pack sentences into chunks with configurable overlap.

    Every emitted pack is <= budget_chars. Two mechanisms keep that true
    without sacrificing overlap:

    * a sentence is only appended when it still fits, otherwise the pack
      is flushed first;
    * on flush, the carried overlap is trimmed from the front until the
      incoming sentence fits alongside it. Large sentences therefore
      carry less overlap (or none) rather than being isolated, so real
      long-dialogue sentences keep their context while bounded fragments
      from _split_oversized() can never compound past the budget.

    Sentences arrive pre-cut: chunk_book runs them through
    _expand_oversized() first, so none exceeds budget_chars on its own.
    """
    packs: list[list[str]] = []
    current: list[str] = []
    carried = 0  # sentences in `current` that came from the previous pack
    size = 0
    for sentence in sentences:
        if current and size + len(sentence) + 1 > budget_chars:
            packs.append(current)
            carry = current[-overlap_sentences:] if overlap_sentences else []
            while carry and sum(len(s) + 1 for s in carry) + len(sentence) + 1 > budget_chars:
                carry.pop(0)
            current = carry
            carried = len(current)
            size = sum(len(s) + 1 for s in current)
        current.append(sentence)
        size += len(sentence) + 1
    # Only append if the pack holds at least one new sentence (not pure overlap).
    if current and len(current) > carried:
        packs.append(current)
    return packs
```

- [ ] **Step 4: Run the chunker tests**

Run: `uv run pytest tests/test_chunker.py -v`
Expected: all pass, including the pre-existing `test_no_sentence_is_ever_lost`, `test_overlap_between_consecutive_chunks`, and `test_oversized_pysbd_sentence_hard_split_is_deterministic`. If `test_overlap_between_consecutive_chunks` fails, the trim loop is too aggressive — re-read Step 3, do not weaken the test.

- [ ] **Step 5: Re-verify the real novel**

```bash
rm -rf data/lore.db data/kuzu data/lancedb
uv run lore ingest data/inbox/pride-and-prejudice.epub
uv run python -c "import sqlite3; c=sqlite3.connect('data/lore.db'); print(c.execute('SELECT COUNT(*), MIN(LENGTH(text)), MAX(LENGTH(text)) FROM chunks').fetchone()); print('over budget:', c.execute('SELECT COUNT(*) FROM chunks WHERE LENGTH(text) > 1600').fetchone()[0])"
```
Expected: ~66 chapters, chunk count in the 700–800 range, **max length ≤ 1600 and `over budget: 0`**. Record the exact numbers in the report. If any chunk exceeds 1600, the trim loop is wrong — fix it.

- [ ] **Step 6: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/ingest/chunker.py tests/test_chunker.py
git commit -m "fix: bound every chunk to the budget while preserving overlap

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 2: Extraction schemas and prompt

**Files:**
- Create: `lore/extract/__init__.py`, `lore/extract/schemas.py`, `lore/extract/prompts.py`
- Test: `tests/test_extract_schemas.py`, `tests/test_extract_prompts.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `EntityKind = Literal["Character", "Place", "Faction", "Object", "Concept", "Event"]`
  - `ExtractedEntity(name: str, kind: EntityKind)`
  - `ExtractedTriplet(subject: str, predicate: str, object: str, evidence: str, confidence: float)`
  - `ChunkExtraction(entities: list[ExtractedEntity], triplets: list[ExtractedTriplet])`
  - `SCHEMA_VERSION: str = "2a.1"`
  - `PREFERRED_PREDICATES: tuple[str, ...]`
  - `build_prompt(chunk_text: str) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_extract_schemas.py`:
```python
import pytest
from pydantic import ValidationError

from lore.extract.schemas import (
    SCHEMA_VERSION,
    ChunkExtraction,
    ExtractedEntity,
    ExtractedTriplet,
)


def test_valid_extraction_parses() -> None:
    payload = {
        "entities": [{"name": "Elizabeth Bennet", "kind": "Character"}],
        "triplets": [
            {
                "subject": "Elizabeth Bennet",
                "predicate": "OPPOSES",
                "object": "Mr Darcy",
                "evidence": "She could not think of Darcy without anger.",
                "confidence": 0.8,
            }
        ],
    }
    extraction = ChunkExtraction.model_validate(payload)
    assert extraction.entities[0].kind == "Character"
    assert extraction.triplets[0].confidence == 0.8


def test_unknown_entity_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractedEntity.model_validate({"name": "X", "kind": "Vehicle"})


def test_confidence_must_be_a_probability() -> None:
    base = {
        "subject": "A",
        "predicate": "KNOWS",
        "object": "B",
        "evidence": "text",
    }
    for bad in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            ExtractedTriplet.model_validate({**base, "confidence": bad})


def test_empty_names_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractedEntity.model_validate({"name": "  ", "kind": "Character"})


def test_empty_lists_are_valid() -> None:
    # A chunk of pure scene-setting legitimately yields nothing.
    assert ChunkExtraction.model_validate({"entities": [], "triplets": []}).triplets == []


def test_json_schema_is_guided_decoding_ready() -> None:
    schema = ChunkExtraction.model_json_schema()
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"entities", "triplets"}


def test_schema_version_is_recorded() -> None:
    assert SCHEMA_VERSION == "2a.1"
```

`tests/test_extract_prompts.py`:
```python
from lore.extract.prompts import PREFERRED_PREDICATES, build_prompt


def test_prompt_embeds_the_chunk_text() -> None:
    prompt = build_prompt("Elizabeth walked to Netherfield.")
    assert "Elizabeth walked to Netherfield." in prompt


def test_prompt_steers_toward_the_curated_predicates() -> None:
    prompt = build_prompt("text")
    for predicate in ("ALLEGIANCE_TO", "INTERACTS_WITH", "BECOMES"):
        assert predicate in prompt
    assert "ALLEGIANCE_TO" in PREFERRED_PREDICATES


def test_prompt_demands_evidence_quoted_from_the_passage() -> None:
    prompt = build_prompt("text").lower()
    assert "evidence" in prompt
    assert "verbatim" in prompt


def test_prompt_is_deterministic() -> None:
    assert build_prompt("same text") == build_prompt("same text")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_extract_schemas.py tests/test_extract_prompts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lore.extract'`.

- [ ] **Step 3: Implement**

`lore/extract/__init__.py`: empty file.

`lore/extract/schemas.py`:
```python
"""The JSON contract for one chunk's extraction.

This module is the single source of truth: the local tier validates
against it, and the Kaggle kernel hands its JSON schema to vLLM's
guided decoding so the model cannot emit anything else.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "2a.1"

EntityKind = Literal["Character", "Place", "Faction", "Object", "Concept", "Event"]


class ExtractedEntity(BaseModel):
    name: str = Field(min_length=1)
    kind: EntityKind

    @field_validator("name")
    @classmethod
    def _strip_and_require_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("entity name must contain text")
        return cleaned


class ExtractedTriplet(BaseModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    evidence: str = Field(default="", description="verbatim quote from the chunk")
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("subject", "predicate", "object")
    @classmethod
    def _strip_and_require_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("triplet fields must contain text")
        return cleaned


class ChunkExtraction(BaseModel):
    # No defaults: guided decoding needs both keys in the schema's
    # `required` list so the model always emits them, even when empty.
    entities: list[ExtractedEntity]
    triplets: list[ExtractedTriplet]
```

`lore/extract/prompts.py`:
```python
"""Extraction prompt. Imported by both tiers — never duplicated in the kernel."""
from __future__ import annotations

PREFERRED_PREDICATES: tuple[str, ...] = (
    "ALLEGIANCE_TO",
    "MEMBER_OF",
    "LOCATED_IN",
    "POSSESSES",
    "INTERACTS_WITH",
    "OPPOSES",
    "LOVES",
    "KILLS",
    "BECOMES",
    "EXPERIENCES",
)

_TEMPLATE = """You extract a knowledge graph from a passage of a novel.

Read the PASSAGE and return every entity it names and every relationship it \
states or clearly implies.

Rules:
- Use the entity's fullest name as written in the passage.
- Prefer these predicates when one fits: {predicates}. Otherwise use a short \
SCREAMING_SNAKE_CASE verb phrase.
- `evidence` must be a verbatim span copied from the passage that supports the \
relationship. Never invent or paraphrase it.
- `confidence` is 0.0-1.0: use 0.9+ when the passage states it outright, 0.5-0.7 \
when it is implied, below 0.5 when it is a guess.
- Extract only what this passage supports. Return empty lists rather than \
speculating.

PASSAGE:
{chunk_text}
"""


def build_prompt(chunk_text: str) -> str:
    return _TEMPLATE.format(
        predicates=", ".join(PREFERRED_PREDICATES), chunk_text=chunk_text
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract_schemas.py tests/test_extract_prompts.py -v`
Expected: 11 passed.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/extract/ tests/test_extract_schemas.py tests/test_extract_prompts.py
git commit -m "feat: extraction schemas and prompt as the single source of truth

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 3: Provider-agnostic extraction runner

**Files:**
- Create: `lore/extract/runner.py`
- Test: `tests/test_extract_runner.py`

**Interfaces:**
- Consumes: `ChunkExtraction` and `build_prompt` from Task 2; `InferenceProvider` protocol from `lore/inference/base.py` (`async def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel`).
- Produces:
  - `ChunkInput(chunk_id: str, text: str)` (frozen dataclass)
  - `ChunkResult(chunk_id: str, extraction: ChunkExtraction)` (frozen dataclass)
  - `DeadLetter(chunk_id: str, raw_output: str, error: str)` (frozen dataclass)
  - `ExtractionResult(results: list[ChunkResult], dead_letters: list[DeadLetter])` (dataclass)
  - `async def extract_chunks(chunks: list[ChunkInput], provider, *, max_attempts: int = 2, on_progress: Callable[[int, int], None] | None = None) -> ExtractionResult`

- [ ] **Step 1: Write the failing tests**

`tests/test_extract_runner.py`:
```python
import pytest

from lore.extract.runner import ChunkInput, extract_chunks
from lore.extract.schemas import ChunkExtraction

VALID = ChunkExtraction.model_validate(
    {
        "entities": [{"name": "Darcy", "kind": "Character"}],
        "triplets": [
            {
                "subject": "Darcy",
                "predicate": "LOVES",
                "object": "Elizabeth",
                "evidence": "he ardently admired her",
                "confidence": 0.9,
            }
        ],
    }
)


class ScriptedProvider:
    """Returns/raises whatever the script says, per call."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    async def generate(self, prompt: str, schema):
        self.calls.append(prompt)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
async def test_happy_path_collects_results() -> None:
    provider = ScriptedProvider([VALID, VALID])
    result = await extract_chunks(
        [ChunkInput("b:1:0", "text one"), ChunkInput("b:1:1", "text two")], provider
    )
    assert [r.chunk_id for r in result.results] == ["b:1:0", "b:1:1"]
    assert result.dead_letters == []
    assert "text one" in provider.calls[0]  # the chunk text reached the prompt


@pytest.mark.asyncio
async def test_retries_once_then_succeeds() -> None:
    provider = ScriptedProvider([ValueError("bad json"), VALID])
    result = await extract_chunks([ChunkInput("b:1:0", "t")], provider)
    assert len(result.results) == 1
    assert result.dead_letters == []
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_dead_letters_after_repeated_failure() -> None:
    provider = ScriptedProvider([ValueError("bad json"), ValueError("still bad")])
    result = await extract_chunks([ChunkInput("b:1:0", "t")], provider)
    assert result.results == []
    assert len(result.dead_letters) == 1
    dead = result.dead_letters[0]
    assert dead.chunk_id == "b:1:0"
    assert "still bad" in dead.error


@pytest.mark.asyncio
async def test_one_bad_chunk_does_not_abort_the_batch() -> None:
    provider = ScriptedProvider(
        [ValueError("x"), ValueError("y"), VALID]  # chunk 1 dies, chunk 2 succeeds
    )
    result = await extract_chunks(
        [ChunkInput("b:1:0", "a"), ChunkInput("b:1:1", "b")], provider
    )
    assert [r.chunk_id for r in result.results] == ["b:1:1"]
    assert [d.chunk_id for d in result.dead_letters] == ["b:1:0"]


@pytest.mark.asyncio
async def test_progress_callback_reports_each_chunk() -> None:
    seen: list[tuple[int, int]] = []
    provider = ScriptedProvider([VALID, VALID])
    await extract_chunks(
        [ChunkInput("b:1:0", "a"), ChunkInput("b:1:1", "b")],
        provider,
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert seen == [(1, 2), (2, 2)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_extract_runner.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.extract.runner`.

- [ ] **Step 3: Implement `lore/extract/runner.py`**

```python
"""Per-chunk extraction loop, independent of which backend runs the model.

This is the code the Kaggle kernel imports and the local Ollama tier
calls, so both tiers share prompt, validation, retry and dead-letter
behaviour exactly (SPEC.md section 3).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from lore.extract.prompts import build_prompt
from lore.extract.schemas import ChunkExtraction


@dataclass(frozen=True)
class ChunkInput:
    chunk_id: str
    text: str


@dataclass(frozen=True)
class ChunkResult:
    chunk_id: str
    extraction: ChunkExtraction


@dataclass(frozen=True)
class DeadLetter:
    chunk_id: str
    raw_output: str
    error: str


@dataclass
class ExtractionResult:
    results: list[ChunkResult] = field(default_factory=list)
    dead_letters: list[DeadLetter] = field(default_factory=list)


def _raw_output_of(exc: Exception) -> str:
    """Providers may attach the model's raw text; otherwise we log nothing."""
    return str(getattr(exc, "raw_output", "") or "")


async def extract_chunks(
    chunks: list[ChunkInput],
    provider,
    *,
    max_attempts: int = 2,
    on_progress: Callable[[int, int], None] | None = None,
) -> ExtractionResult:
    result = ExtractionResult()
    total = len(chunks)
    for done, chunk in enumerate(chunks, start=1):
        prompt = build_prompt(chunk.text)
        last_error: Exception | None = None
        for _ in range(max_attempts):
            try:
                extraction = await provider.generate(prompt, ChunkExtraction)
            except Exception as exc:  # provider or validation failure
                last_error = exc
                continue
            result.results.append(ChunkResult(chunk.chunk_id, extraction))
            last_error = None
            break
        if last_error is not None:
            result.dead_letters.append(
                DeadLetter(
                    chunk_id=chunk.chunk_id,
                    raw_output=_raw_output_of(last_error),
                    error=str(last_error),
                )
            )
        if on_progress is not None:
            on_progress(done, total)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract_runner.py -v`
Expected: 5 passed.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/extract/runner.py tests/test_extract_runner.py
git commit -m "feat: provider-agnostic extraction runner with retry and dead-letter

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 4: Config, chunk reader, and work units

**Files:**
- Modify: `lore/config.py` (add `ExtractionConfig`, extend `KaggleConfig`), `lore.toml`, `lore/ingest/registry.py` (add one reader)
- Create: `lore/orchestrator/__init__.py`, `lore/orchestrator/workunit.py`
- Test: `tests/test_workunit.py`, `tests/test_registry.py` (add one test)

**Interfaces:**
- Consumes: `Chunk` from `lore/ingest/models.py` (`id, book_id, chapter_seq, scene_seq, seq, text`); `open_db` from `lore/db/sqlite.py`.
- Produces:
  - `ExtractionConfig(model: str, embed_model: str, embed_model_ollama: str, work_unit_size: int, vllm_version: str)` and `LoreConfig.extraction`
  - `KaggleConfig.extract_kernel_slug` / `.extract_dataset_slug`
  - `chunks_for_book(conn, book_id) -> list[Chunk]` (ordered by `chapter_seq`, `seq`)
  - `WorkUnit(unit_id: str, book_id: str, chunks: list[Chunk])` (frozen dataclass)
  - `pack_units(book_id: str, chunks: list[Chunk], size: int) -> list[WorkUnit]`
  - `write_unit(unit: WorkUnit, dest: Path, package_root: Path | None = None) -> Path`
  - `OutputSummary(chunks_in: int, chunks_ok: int, chunks_dead: int, gpu_seconds: float, model: str)`
  - `validate_output(unit: WorkUnit, out_dir: Path) -> OutputSummary` (raises `OutputError`)
  - `class OutputError(RuntimeError)`

- [ ] **Step 1: Write the failing tests**

`tests/test_workunit.py`:
```python
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lore.ingest.models import Chunk
from lore.orchestrator.workunit import (
    OutputError,
    pack_units,
    validate_output,
    write_unit,
)

BOOK = "abcdef0123456789"


def _chunks(n: int) -> list[Chunk]:
    return [
        Chunk(
            id=f"{BOOK}:1:{i}",
            book_id=BOOK,
            chapter_seq=1,
            scene_seq=0,
            seq=i,
            text=f"chunk text {i}",
        )
        for i in range(n)
    ]


def test_pack_units_splits_and_labels() -> None:
    units = pack_units(BOOK, _chunks(5), size=2)
    assert [u.unit_id for u in units] == [
        f"{BOOK}-u0000",
        f"{BOOK}-u0001",
        f"{BOOK}-u0002",
    ]
    assert [len(u.chunks) for u in units] == [2, 2, 1]
    assert units[0].chunks[0].id == f"{BOOK}:1:0"


def test_pack_units_is_deterministic() -> None:
    assert pack_units(BOOK, _chunks(7), size=3) == pack_units(BOOK, _chunks(7), size=3)


def test_write_unit_emits_jsonl_and_manifest(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    unit_dir = write_unit(unit, tmp_path)
    rows = [
        json.loads(line)
        for line in (unit_dir / "unit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert [r["chunk_id"] for r in rows] == [c.id for c in unit.chunks]
    assert rows[0]["text"] == "chunk text 0"
    manifest = json.loads((unit_dir / "unit-manifest.json").read_text())
    assert manifest["unit_id"] == unit.unit_id
    assert manifest["book_id"] == BOOK
    assert manifest["chunk_count"] == 3


def test_write_unit_ships_the_lore_package(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(1), size=10)[0]
    unit_dir = write_unit(unit, tmp_path, package_root=Path("lore"))
    assert (unit_dir / "lore" / "extract" / "runner.py").is_file()
    assert (unit_dir / "lore" / "extract" / "prompts.py").is_file()


def _write_outputs(
    out_dir: Path,
    chunk_ids: list[str],
    *,
    chunks_in: int,
    chunks_ok: int,
    chunks_dead: int = 0,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"chunk_id": chunk_ids, "subject": ["s"] * len(chunk_ids)}),
        out_dir / "triplets.parquet",
    )
    pq.write_table(
        pa.table({"chunk_id": chunk_ids, "vector": [[0.0] * 768] * len(chunk_ids)}),
        out_dir / "embeddings.parquet",
    )
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "unit_id": f"{BOOK}-u0000",
                "chunks_in": chunks_in,
                "chunks_ok": chunks_ok,
                "chunks_dead": chunks_dead,
                "gpu_seconds": 120.0,
                "model": "Qwen/Qwen2.5-7B-Instruct-AWQ",
            }
        )
    )


def test_validate_output_accepts_a_complete_unit(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    ids = [c.id for c in unit.chunks]
    _write_outputs(tmp_path, ids, chunks_in=3, chunks_ok=3)
    summary = validate_output(unit, tmp_path)
    assert summary.chunks_ok == 3
    assert summary.gpu_seconds == 120.0
    assert summary.model == "Qwen/Qwen2.5-7B-Instruct-AWQ"


def test_validate_output_rejects_missing_artifacts(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    with pytest.raises(OutputError, match="missing"):
        validate_output(unit, tmp_path)


def test_validate_output_rejects_unaccounted_chunks(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    ids = [c.id for c in unit.chunks]
    _write_outputs(tmp_path, ids, chunks_in=3, chunks_ok=2)  # 2 + 0 != 3
    with pytest.raises(OutputError, match="accounted"):
        validate_output(unit, tmp_path)


def test_validate_output_rejects_foreign_chunk_ids(tmp_path: Path) -> None:
    unit = pack_units(BOOK, _chunks(3), size=10)[0]
    _write_outputs(tmp_path, ["someone-elses:1:0"], chunks_in=3, chunks_ok=3)
    with pytest.raises(OutputError, match="foreign"):
        validate_output(unit, tmp_path)
```

Append to `tests/test_registry.py`:
```python
def test_chunks_for_book_returns_narrative_order(tmp_path: Path) -> None:
    from lore.ingest.registry import chunks_for_book

    conn = open_db(tmp_path / "lore.db")
    out_of_order = [
        Chunk(id=f"{BOOK_ID}:2:0", book_id=BOOK_ID, chapter_seq=2, scene_seq=0, seq=0, text="c"),
        Chunk(id=f"{BOOK_ID}:1:1", book_id=BOOK_ID, chapter_seq=1, scene_seq=0, seq=1, text="b"),
        Chunk(id=f"{BOOK_ID}:1:0", book_id=BOOK_ID, chapter_seq=1, scene_seq=0, seq=0, text="a"),
    ]
    ingest_book(
        conn, book_id=BOOK_ID, title="T", author=None,
        source="upload", file_path="/x/b.epub", chunks=out_of_order,
    )
    assert [c.text for c in chunks_for_book(conn, BOOK_ID)] == ["a", "b", "c"]
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_workunit.py tests/test_registry.py -v`
Expected: workunit tests FAIL with `ModuleNotFoundError` for `lore.orchestrator`; the registry test fails on `ImportError: cannot import name 'chunks_for_book'`.

- [ ] **Step 3: Implement**

Add to `lore/config.py` (a new model, plus two fields on `KaggleConfig` and one on `LoreConfig`):
```python
class ExtractionConfig(BaseModel):
    model: str = "Qwen/Qwen2.5-7B-Instruct-AWQ"
    embed_model: str = "nomic-ai/nomic-embed-text-v1.5"
    embed_model_ollama: str = "nomic-embed-text"
    work_unit_size: int = 250
    vllm_version: str = ""  # empty = install latest on the kernel
```
In `KaggleConfig` add:
```python
    extract_kernel_slug: str = "lore-extract"
    extract_dataset_slug: str = "lore-extract-data"
```
In `LoreConfig` add:
```python
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
```
and a property:
```python
    @property
    def extractions_dir(self) -> Path:
        return self.data_dir / "extractions"
```
Add `self.extractions_dir` to the tuple `ensure_dirs()` creates.

Append to `lore.toml`:
```toml
[lore.extraction]
model = "Qwen/Qwen2.5-7B-Instruct-AWQ"
embed_model = "nomic-ai/nomic-embed-text-v1.5"
embed_model_ollama = "nomic-embed-text"
work_unit_size = 250
vllm_version = ""            # empty installs the latest vLLM on the kernel
```
and inside the existing `[lore.kaggle]` block:
```toml
extract_kernel_slug = "lore-extract"
extract_dataset_slug = "lore-extract-data"
```

Append to `lore/ingest/registry.py`:
```python
def chunks_for_book(conn: sqlite3.Connection, book_id: str) -> list[Chunk]:
    rows = conn.execute(
        "SELECT id, book_id, chapter_seq, scene_seq, seq, text FROM chunks"
        " WHERE book_id = ? ORDER BY chapter_seq, seq",
        (book_id,),
    ).fetchall()
    return [
        Chunk(
            id=r[0], book_id=r[1], chapter_seq=r[2],
            scene_seq=r[3], seq=r[4], text=r[5],
        )
        for r in rows
    ]
```
(`Chunk` is already imported there.)

`lore/orchestrator/__init__.py`: empty file.

`lore/orchestrator/workunit.py`:
```python
"""Work units: the transport format between the local pipeline and Kaggle."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from lore.extract.schemas import SCHEMA_VERSION
from lore.ingest.models import Chunk

_ARTIFACTS = ("triplets.parquet", "embeddings.parquet", "manifest.json")


class OutputError(RuntimeError):
    """A returned unit failed validation and must be re-queued."""


@dataclass(frozen=True)
class WorkUnit:
    unit_id: str
    book_id: str
    chunks: list[Chunk]


@dataclass(frozen=True)
class OutputSummary:
    chunks_in: int
    chunks_ok: int
    chunks_dead: int
    gpu_seconds: float
    model: str


def pack_units(book_id: str, chunks: list[Chunk], size: int) -> list[WorkUnit]:
    if size < 1:
        raise ValueError("work unit size must be >= 1")
    units: list[WorkUnit] = []
    for index, start in enumerate(range(0, len(chunks), size)):
        units.append(
            WorkUnit(
                unit_id=f"{book_id}-u{index:04d}",
                book_id=book_id,
                chunks=chunks[start : start + size],
            )
        )
    return units


def write_unit(
    unit: WorkUnit, dest: Path, package_root: Path | None = None
) -> Path:
    """Materialize a unit's upload directory: chunks, manifest, and the
    `lore` package the kernel imports so both tiers share one runner."""
    unit_dir = dest / unit.unit_id
    unit_dir.mkdir(parents=True, exist_ok=True)
    with (unit_dir / "unit.jsonl").open("w") as handle:
        for chunk in unit.chunks:
            handle.write(
                json.dumps(
                    {
                        "chunk_id": chunk.id,
                        "text": chunk.text,
                        "chapter_seq": chunk.chapter_seq,
                    }
                )
                + "\n"
            )
    (unit_dir / "unit-manifest.json").write_text(
        json.dumps(
            {
                "unit_id": unit.unit_id,
                "book_id": unit.book_id,
                "chunk_count": len(unit.chunks),
                "schema_version": SCHEMA_VERSION,
            },
            indent=2,
        )
    )
    root = package_root if package_root is not None else Path(__file__).resolve().parents[1]
    shutil.copytree(
        root,
        unit_dir / "lore",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return unit_dir


def validate_output(unit: WorkUnit, out_dir: Path) -> OutputSummary:
    missing = [name for name in _ARTIFACTS if not (out_dir / name).is_file()]
    if missing:
        raise OutputError(f"unit {unit.unit_id}: missing artifacts {missing}")

    manifest = json.loads((out_dir / "manifest.json").read_text())
    expected = {chunk.id for chunk in unit.chunks}
    chunks_in = int(manifest.get("chunks_in", -1))
    chunks_ok = int(manifest.get("chunks_ok", -1))
    chunks_dead = int(manifest.get("chunks_dead", -1))
    if chunks_in != len(expected):
        raise OutputError(
            f"unit {unit.unit_id}: manifest says {chunks_in} chunks in, "
            f"expected {len(expected)}"
        )
    if chunks_ok + chunks_dead != chunks_in:
        raise OutputError(
            f"unit {unit.unit_id}: {chunks_ok} ok + {chunks_dead} dead do not "
            f"account for {chunks_in} chunks"
        )
    for artifact in ("triplets.parquet", "embeddings.parquet"):
        table = pq.read_table(out_dir / artifact, columns=["chunk_id"])
        foreign = set(table.column("chunk_id").to_pylist()) - expected
        if foreign:
            raise OutputError(
                f"unit {unit.unit_id}: {artifact} contains foreign chunk ids "
                f"{sorted(foreign)[:3]}"
            )
    return OutputSummary(
        chunks_in=chunks_in,
        chunks_ok=chunks_ok,
        chunks_dead=chunks_dead,
        gpu_seconds=float(manifest.get("gpu_seconds", 0.0)),
        model=str(manifest.get("model", "")),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_workunit.py tests/test_registry.py tests/test_config.py -v`
Expected: all pass. `test_write_unit_ships_the_lore_package` proves the kernel will find `lore/extract/runner.py` in its dataset.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/config.py lore.toml lore/ingest/registry.py lore/orchestrator/ tests/test_workunit.py tests/test_registry.py
git commit -m "feat: extraction config, chunk reader, and work unit packaging

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 5: Weekly GPU-hour quota ledger

**Files:**
- Create: `lore/orchestrator/quota.py`
- Test: `tests/test_quota.py`

**Interfaces:**
- Consumes: the `quota_ledger` table from migration v1 (`id, run_ref, gpu_hours_est, gpu_hours_actual, week, created_at`); `open_db`.
- Produces:
  - `WEEKLY_LIMIT_HOURS: float = 30.0`
  - `class QuotaExceeded(RuntimeError)`
  - `iso_week(now: datetime | None = None) -> str` → e.g. `"2026-W35"`
  - `hours_used(conn, week: str) -> float` (actual where recorded, else estimate)
  - `check_budget(conn, est_hours: float, week: str, limit_hours: float = WEEKLY_LIMIT_HOURS) -> None`
  - `record_dispatch(conn, run_ref: str, est_hours: float, week: str) -> None`
  - `record_actual(conn, run_ref: str, actual_hours: float) -> None`
  - `week_report(conn, week: str) -> dict` with keys `week, used_hours, remaining_hours, runs`

- [ ] **Step 1: Write the failing tests**

`tests/test_quota.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lore.db.sqlite import open_db
from lore.orchestrator.quota import (
    QuotaExceeded,
    check_budget,
    hours_used,
    iso_week,
    record_actual,
    record_dispatch,
    week_report,
)


def test_iso_week_formats_year_and_week() -> None:
    assert iso_week(datetime(2026, 8, 25, tzinfo=timezone.utc)) == "2026-W35"


def test_hours_used_starts_at_zero(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    assert hours_used(conn, "2026-W35") == 0.0
    conn.close()


def test_estimate_counts_until_actual_replaces_it(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    record_dispatch(conn, "run-1", est_hours=3.0, week="2026-W35")
    assert hours_used(conn, "2026-W35") == 3.0
    record_actual(conn, "run-1", actual_hours=1.5)
    assert hours_used(conn, "2026-W35") == 1.5
    conn.close()


def test_other_weeks_do_not_count(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    record_dispatch(conn, "run-1", est_hours=5.0, week="2026-W34")
    assert hours_used(conn, "2026-W35") == 0.0
    conn.close()


def test_check_budget_allows_within_limit(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    record_dispatch(conn, "run-1", est_hours=20.0, week="2026-W35")
    check_budget(conn, est_hours=5.0, week="2026-W35")  # 25 <= 30, no raise
    conn.close()


def test_check_budget_refuses_over_limit(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    record_dispatch(conn, "run-1", est_hours=28.0, week="2026-W35")
    with pytest.raises(QuotaExceeded, match="30"):
        check_budget(conn, est_hours=5.0, week="2026-W35")
    conn.close()


def test_week_report_summarizes(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    record_dispatch(conn, "run-1", est_hours=2.0, week="2026-W35")
    record_actual(conn, "run-1", actual_hours=1.0)
    record_dispatch(conn, "run-2", est_hours=4.0, week="2026-W35")
    report = week_report(conn, "2026-W35")
    assert report["week"] == "2026-W35"
    assert report["used_hours"] == 5.0
    assert report["remaining_hours"] == 25.0
    assert report["runs"] == 2
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_quota.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.orchestrator.quota`.

- [ ] **Step 3: Implement `lore/orchestrator/quota.py`**

```python
"""Weekly GPU-hour budget against Kaggle's free-tier allowance.

An unfinished run counts at its estimate; once the kernel reports real
GPU seconds the actual replaces it, so the ledger neither overbooks nor
strands quota.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

WEEKLY_LIMIT_HOURS = 30.0


class QuotaExceeded(RuntimeError):
    """Dispatching this run would exceed the weekly free-tier allowance."""


def iso_week(now: datetime | None = None) -> str:
    moment = now if now is not None else datetime.now(timezone.utc)
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def hours_used(conn: sqlite3.Connection, week: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(gpu_hours_actual, gpu_hours_est)), 0.0)"
        " FROM quota_ledger WHERE week = ?",
        (week,),
    ).fetchone()
    return float(row[0])


def check_budget(
    conn: sqlite3.Connection,
    est_hours: float,
    week: str,
    limit_hours: float = WEEKLY_LIMIT_HOURS,
) -> None:
    used = hours_used(conn, week)
    if used + est_hours > limit_hours:
        raise QuotaExceeded(
            f"{week}: {used:.1f}h used + {est_hours:.1f}h estimated exceeds "
            f"the {limit_hours:.0f}h weekly allowance"
        )


def record_dispatch(
    conn: sqlite3.Connection, run_ref: str, est_hours: float, week: str
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO quota_ledger (run_ref, gpu_hours_est, week) VALUES (?, ?, ?)",
            (run_ref, est_hours, week),
        )


def record_actual(
    conn: sqlite3.Connection, run_ref: str, actual_hours: float
) -> None:
    with conn:
        conn.execute(
            "UPDATE quota_ledger SET gpu_hours_actual = ? WHERE run_ref = ?",
            (actual_hours, run_ref),
        )


def week_report(conn: sqlite3.Connection, week: str) -> dict:
    used = hours_used(conn, week)
    runs = conn.execute(
        "SELECT COUNT(*) FROM quota_ledger WHERE week = ?", (week,)
    ).fetchone()[0]
    return {
        "week": week,
        "used_hours": used,
        "remaining_hours": max(0.0, WEEKLY_LIMIT_HOURS - used),
        "runs": int(runs),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_quota.py -v`
Expected: 7 passed.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/orchestrator/quota.py tests/test_quota.py
git commit -m "feat: weekly gpu-hour quota ledger

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 6: Kaggle extraction kernel and its materializer

**Files:**
- Create: `kaggle/extract/kernel-metadata.json`, `kaggle/extract/extract_kernel.py`, `kaggle/extract_data/dataset-metadata.json`, `lore/orchestrator/templates.py`
- Test: `tests/test_extract_templates.py`

**Interfaces:**
- Consumes: `KaggleConfig` and `ExtractionConfig` from Task 4; `write_unit` from Task 4.
- Produces: `materialize_extract_run(config: LoreConfig, unit: WorkUnit, work_dir: Path) -> tuple[Path, Path]` returning `(kernel_dir, dataset_dir)`, with `USERNAME`, `KERNEL_SLUG`, `DATASET_SLUG`, `MODEL_ID`, `EMBED_MODEL_ID`, `VLLM_SPEC` substituted.

**Note on the kernel script:** it runs only on Kaggle (imports `vllm`, `torch`, `sentence_transformers`) and must never be imported by a local test. Tests here cover the materializer and the metadata, exactly as Phase 0's hello round trip did.

- [ ] **Step 1: Write the template files**

`kaggle/extract/kernel-metadata.json`:
```json
{
  "id": "USERNAME/KERNEL_SLUG",
  "title": "KERNEL_SLUG",
  "code_file": "extract_kernel.py",
  "language": "python",
  "kernel_type": "script",
  "is_private": "true",
  "enable_gpu": "true",
  "machine_shape": "NvidiaTeslaT4",
  "enable_internet": "true",
  "dataset_sources": ["USERNAME/DATASET_SLUG"],
  "competition_sources": [],
  "kernel_sources": []
}
```

`kaggle/extract_data/dataset-metadata.json`:
```json
{
  "title": "DATASET_SLUG",
  "id": "USERNAME/DATASET_SLUG",
  "licenses": [{"name": "CC0-1.0"}]
}
```

`kaggle/extract/extract_kernel.py`:
```python
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
```

- [ ] **Step 2: Write the failing tests**

`tests/test_extract_templates.py`:
```python
import json
from pathlib import Path

from lore.config import LoreConfig
from lore.ingest.models import Chunk
from lore.orchestrator.templates import materialize_extract_run
from lore.orchestrator.workunit import pack_units

BOOK = "abcdef0123456789"


def _config() -> LoreConfig:
    config = LoreConfig.load()
    return config.model_copy(
        update={"kaggle": config.kaggle.model_copy(update={"username": "realuser"})}
    )


def _unit():
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
              scene_seq=0, seq=i, text=f"text {i}")
        for i in range(2)
    ]
    return pack_units(BOOK, chunks, size=10)[0]


def test_materialize_substitutes_identity_and_models(tmp_path: Path) -> None:
    kernel_dir, dataset_dir = materialize_extract_run(_config(), _unit(), tmp_path)

    meta = json.loads((kernel_dir / "kernel-metadata.json").read_text())
    assert meta["id"] == "realuser/lore-extract"
    assert meta["dataset_sources"] == ["realuser/lore-extract-data"]
    assert meta["enable_gpu"] == "true"
    assert meta["machine_shape"] == "NvidiaTeslaT4"
    assert meta["enable_internet"] == "true"

    ds_meta = json.loads((dataset_dir / "dataset-metadata.json").read_text())
    assert ds_meta["id"] == "realuser/lore-extract-data"

    script = (kernel_dir / "extract_kernel.py").read_text()
    assert 'MODEL_ID = "Qwen/Qwen2.5-7B-Instruct-AWQ"' in script
    assert 'EMBED_MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"' in script
    assert "USERNAME" not in script


def test_materialize_places_the_unit_and_package_in_the_dataset(tmp_path: Path) -> None:
    _, dataset_dir = materialize_extract_run(_config(), _unit(), tmp_path)
    assert (dataset_dir / "unit.jsonl").is_file()
    assert (dataset_dir / "unit-manifest.json").is_file()
    assert (dataset_dir / "lore" / "extract" / "runner.py").is_file()


def test_vllm_spec_defaults_to_unpinned(tmp_path: Path) -> None:
    kernel_dir, _ = materialize_extract_run(_config(), _unit(), tmp_path)
    assert 'VLLM_SPEC = "vllm"' in (kernel_dir / "extract_kernel.py").read_text()


def test_vllm_spec_honours_a_pin(tmp_path: Path) -> None:
    config = _config()
    config = config.model_copy(
        update={"extraction": config.extraction.model_copy(update={"vllm_version": "0.7.2"})}
    )
    kernel_dir, _ = materialize_extract_run(config, _unit(), tmp_path)
    assert 'VLLM_SPEC = "vllm==0.7.2"' in (kernel_dir / "extract_kernel.py").read_text()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_extract_templates.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.orchestrator.templates`.

- [ ] **Step 4: Implement `lore/orchestrator/templates.py`**

```python
"""Materialize the extraction kernel and its work-unit dataset."""
from __future__ import annotations

import shutil
from pathlib import Path

from lore.config import LoreConfig
from lore.orchestrator.workunit import WorkUnit, write_unit

TEMPLATES = Path(__file__).resolve().parents[2] / "kaggle"


def materialize_extract_run(
    config: LoreConfig, unit: WorkUnit, work_dir: Path
) -> tuple[Path, Path]:
    if not config.kaggle.username:
        raise ValueError("kaggle.username is not set in lore.toml")

    kernel_dir = work_dir / "extract"
    shutil.copytree(TEMPLATES / "extract", kernel_dir, dirs_exist_ok=True)

    staging = work_dir / "staging"
    unit_dir = write_unit(unit, staging)
    dataset_dir = work_dir / "extract_data"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    for item in unit_dir.iterdir():
        target = dataset_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    shutil.copy2(
        TEMPLATES / "extract_data" / "dataset-metadata.json",
        dataset_dir / "dataset-metadata.json",
    )

    spec = (
        f"vllm=={config.extraction.vllm_version}"
        if config.extraction.vllm_version
        else "vllm"
    )
    # EMBED_MODEL_ID first: MODEL_ID is a substring of it, and plain text
    # replacement would otherwise mangle the embed line.
    substitutions = {
        "EMBED_MODEL_ID": config.extraction.embed_model,
        "USERNAME": config.kaggle.username,
        "KERNEL_SLUG": config.kaggle.extract_kernel_slug,
        "DATASET_SLUG": config.kaggle.extract_dataset_slug,
        "MODEL_ID": config.extraction.model,
        "VLLM_SPEC": spec,
    }
    for path in (
        kernel_dir / "kernel-metadata.json",
        kernel_dir / "extract_kernel.py",
        dataset_dir / "dataset-metadata.json",
    ):
        text = path.read_text()
        for key, value in substitutions.items():
            text = text.replace(key, value)
        path.write_text(text)
    return kernel_dir, dataset_dir
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract_templates.py -v`
Expected: 4 passed.

- [ ] **Step 6: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add kaggle/extract/ kaggle/extract_data/ lore/orchestrator/templates.py tests/test_extract_templates.py
git commit -m "feat: kaggle extraction kernel and run materializer

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 7: Run orchestrator with resume

**Files:**
- Create: `lore/orchestrator/run.py`
- Test: `tests/test_orchestrator_run.py`

**Interfaces:**
- Consumes: `pack_units`, `validate_output`, `OutputError`, `OutputSummary` (Task 4); `materialize_extract_run` (Task 6); quota functions (Task 5); `KaggleClient`/`KaggleRunError` (Phase 0: `push_dataset(dir)`, `wait_dataset_ready()`, `run_and_wait(kernel_dir, dest, on_tick=None) -> Path`); `chunks_for_book` (Task 4).
- Produces:
  - `RunReport(book_id, units_total, units_completed, units_failed, chunks_ok, chunks_dead, gpu_hours)` (dataclass)
  - `unit_state(conn, unit_id) -> str | None`
  - `set_unit_state(conn, book_id, unit_id, state, detail="") -> None`
  - `run_extraction(config, conn, book_id, *, resume=False, client_factory=None, on_event=None) -> RunReport`
  - Unit states: `"queued" | "dispatched" | "completed" | "failed"`; `jobs.kind = "extract_unit"`, `jobs.detail = unit_id`.

- [ ] **Step 1: Write the failing tests**

`tests/test_orchestrator_run.py`:
```python
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book
from lore.orchestrator.run import RunReport, run_extraction, unit_state

BOOK = "abcdef0123456789"


def _config(tmp_path: Path) -> LoreConfig:
    config = LoreConfig.load()
    return config.model_copy(
        update={
            "data_dir": tmp_path / "data",
            "kaggle": config.kaggle.model_copy(update={"username": "realuser"}),
            "extraction": config.extraction.model_copy(update={"work_unit_size": 2}),
        }
    )


def _seed(conn, n: int) -> None:
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
              scene_seq=0, seq=i, text=f"text {i}")
        for i in range(n)
    ]
    ingest_book(conn, book_id=BOOK, title="T", author=None,
                source="upload", file_path="/x/b.epub", chunks=chunks)


class FakeClient:
    """Stands in for KaggleClient: writes plausible outputs, counts runs."""

    def __init__(self, config, *, fail_units: set[str] | None = None) -> None:
        self.fail_units = fail_units or set()
        self.runs: list[str] = []
        self._unit_ids: list[str] = []

    def push_dataset(self, dir: Path) -> None:
        manifest = json.loads((dir / "unit-manifest.json").read_text())
        self._unit_ids.append(manifest["unit_id"])
        self._chunk_ids = [
            json.loads(line)["chunk_id"]
            for line in (dir / "unit.jsonl").read_text().splitlines()
            if line.strip()
        ]

    def wait_dataset_ready(self, timeout_s: int = 300) -> None:
        return None

    def run_and_wait(self, kernel_dir: Path, dest: Path, on_tick=None) -> Path:
        unit_id = self._unit_ids[-1]
        self.runs.append(unit_id)
        dest.mkdir(parents=True, exist_ok=True)
        if unit_id in self.fail_units:
            return dest  # no artifacts -> validation fails
        ids = self._chunk_ids
        pq.write_table(
            pa.table({"chunk_id": ids, "subject": ["s"] * len(ids)}),
            dest / "triplets.parquet",
        )
        pq.write_table(
            pa.table({"chunk_id": ids, "vector": [[0.0] * 768] * len(ids)}),
            dest / "embeddings.parquet",
        )
        (dest / "manifest.json").write_text(json.dumps({
            "unit_id": unit_id, "chunks_in": len(ids), "chunks_ok": len(ids),
            "chunks_dead": 0, "gpu_seconds": 3600.0,
            "model": "Qwen/Qwen2.5-7B-Instruct-AWQ",
        }))
        return dest


def test_runs_every_unit_and_reports(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 5)  # 3 units at size 2
    clients: list[FakeClient] = []

    def factory(cfg):
        client = FakeClient(cfg)
        clients.append(client)
        return client

    report = run_extraction(config, conn, BOOK, client_factory=factory)
    assert isinstance(report, RunReport)
    assert report.units_total == 3
    assert report.units_completed == 3
    assert report.chunks_ok == 5
    assert report.gpu_hours == pytest.approx(3.0)
    conn.close()


def test_artifacts_land_under_the_book_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)
    run_extraction(config, conn, BOOK, client_factory=lambda cfg: FakeClient(cfg))
    unit_dir = config.extractions_dir / BOOK / f"{BOOK}-u0000"
    assert (unit_dir / "triplets.parquet").is_file()
    assert (unit_dir / "manifest.json").is_file()
    conn.close()


def test_failed_unit_is_marked_and_counted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 4)  # units u0000, u0001

    def factory(cfg):
        return FakeClient(cfg, fail_units={f"{BOOK}-u0001"})

    report = run_extraction(config, conn, BOOK, client_factory=factory)
    assert report.units_completed == 1
    assert report.units_failed == 1
    assert unit_state(conn, f"{BOOK}-u0000") == "completed"
    assert unit_state(conn, f"{BOOK}-u0001") == "failed"
    conn.close()


def test_resume_skips_completed_and_retries_failed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 4)
    run_extraction(
        config, conn, BOOK,
        client_factory=lambda cfg: FakeClient(cfg, fail_units={f"{BOOK}-u0001"}),
    )
    second: list[FakeClient] = []

    def factory(cfg):
        client = FakeClient(cfg)
        second.append(client)
        return client

    report = run_extraction(config, conn, BOOK, resume=True, client_factory=factory)
    assert second[0].runs == [f"{BOOK}-u0001"]  # only the failed unit re-ran
    assert report.units_completed == 1
    assert unit_state(conn, f"{BOOK}-u0001") == "completed"
    conn.close()


def test_resume_is_a_no_op_when_everything_completed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)
    run_extraction(config, conn, BOOK, client_factory=lambda cfg: FakeClient(cfg))
    seen: list[FakeClient] = []

    def factory(cfg):
        client = FakeClient(cfg)
        seen.append(client)
        return client

    report = run_extraction(config, conn, BOOK, resume=True, client_factory=factory)
    assert report.units_total == 1
    assert report.units_completed == 0  # nothing left to do
    assert seen == [] or seen[0].runs == []
    conn.close()


def test_quota_is_recorded_per_unit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)
    run_extraction(config, conn, BOOK, client_factory=lambda cfg: FakeClient(cfg))
    rows = conn.execute(
        "SELECT run_ref, gpu_hours_actual FROM quota_ledger"
    ).fetchall()
    assert rows[0][0] == f"{BOOK}-u0000"
    assert rows[0][1] == pytest.approx(1.0)  # 3600 gpu_seconds
    conn.close()


def test_unknown_book_raises(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    with pytest.raises(ValueError, match="no chunks"):
        run_extraction(config, conn, "nosuchbook", client_factory=lambda cfg: FakeClient(cfg))
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_orchestrator_run.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.orchestrator.run`.

- [ ] **Step 3: Implement `lore/orchestrator/run.py`**

```python
"""Dispatch loop: work units to Kaggle and back, resumably.

Each unit is idempotent — chunk IDs are deterministic, so re-running a
unit reproduces exactly the same work and overwrites its own artifacts.
Nothing is marked complete until its outputs validate.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lore.config import LoreConfig
from lore.ingest.registry import chunks_for_book
from lore.kaggle_api.client import KaggleClient
from lore.orchestrator import quota
from lore.orchestrator.templates import materialize_extract_run
from lore.orchestrator.workunit import OutputError, WorkUnit, pack_units, validate_output

JOB_KIND = "extract_unit"


@dataclass
class RunReport:
    book_id: str
    units_total: int = 0
    units_completed: int = 0
    units_failed: int = 0
    chunks_ok: int = 0
    chunks_dead: int = 0
    gpu_hours: float = 0.0


def unit_state(conn: sqlite3.Connection, unit_id: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM jobs WHERE kind = ? AND detail = ?"
        " ORDER BY id DESC LIMIT 1",
        (JOB_KIND, unit_id),
    ).fetchone()
    return row[0] if row else None


def set_unit_state(
    conn: sqlite3.Connection, book_id: str, unit_id: str, state: str
) -> None:
    with conn:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE kind = ? AND detail = ?", (JOB_KIND, unit_id)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE jobs SET state = ?, updated_at = datetime('now') WHERE id = ?",
                (state, existing[0]),
            )
        else:
            conn.execute(
                "INSERT INTO jobs (kind, book_id, state, detail) VALUES (?, ?, ?, ?)",
                (JOB_KIND, book_id, state, unit_id),
            )


def _kaggle_config_for_extraction(config: LoreConfig):
    """Point the Phase 0 client at the extraction kernel/dataset slugs."""
    return config.kaggle.model_copy(
        update={
            "kernel_slug": config.kaggle.extract_kernel_slug,
            "dataset_slug": config.kaggle.extract_dataset_slug,
        }
    )


def _estimate_hours(unit: WorkUnit) -> float:
    """Rough pre-flight estimate for the quota gate: ~1.5s of GPU per chunk
    plus a fixed session warmup. Replaced by the manifest's real number."""
    return 0.25 + len(unit.chunks) * 1.5 / 3600.0


def _run_unit(
    config: LoreConfig,
    conn: sqlite3.Connection,
    unit: WorkUnit,
    client,
    on_event: Callable[[str], None] | None,
) -> tuple[bool, float, int, int]:
    dest = config.extractions_dir / unit.book_id / unit.unit_id
    week = quota.iso_week()
    quota.check_budget(conn, _estimate_hours(unit), week)
    quota.record_dispatch(conn, unit.unit_id, _estimate_hours(unit), week)
    set_unit_state(conn, unit.book_id, unit.unit_id, "dispatched")

    work_dir = Path(tempfile.mkdtemp(prefix=f"lore-{unit.unit_id}-"))
    try:
        kernel_dir, dataset_dir = materialize_extract_run(config, unit, work_dir)
        client.push_dataset(dataset_dir)
        client.wait_dataset_ready()
        out_dir = work_dir / "out"
        client.run_and_wait(kernel_dir, out_dir, on_tick=on_event)
        summary = validate_output(unit, out_dir)
        # Copy the artifacts out BEFORE the finally clause deletes work_dir.
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("triplets.parquet", "embeddings.parquet", "manifest.json",
                     "dead_letter.jsonl"):
            source = out_dir / name
            if source.is_file():
                shutil.copy2(source, dest / name)
    except Exception as exc:  # any failure re-queues the unit
        set_unit_state(conn, unit.book_id, unit.unit_id, "failed")
        if on_event is not None:
            on_event(f"{unit.unit_id}: failed — {exc}")
        return False, 0.0, 0, 0
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    hours = summary.gpu_seconds / 3600.0
    quota.record_actual(conn, unit.unit_id, hours)
    set_unit_state(conn, unit.book_id, unit.unit_id, "completed")
    if on_event is not None:
        on_event(f"{unit.unit_id}: {summary.chunks_ok} ok, {summary.chunks_dead} dead")
    return True, hours, summary.chunks_ok, summary.chunks_dead
```

Continue the module:
```python
def run_extraction(
    config: LoreConfig,
    conn: sqlite3.Connection,
    book_id: str,
    *,
    resume: bool = False,
    client_factory: Callable[[object], object] | None = None,
    on_event: Callable[[str], None] | None = None,
) -> RunReport:
    chunks = chunks_for_book(conn, book_id)
    if not chunks:
        raise ValueError(f"no chunks registered for book {book_id}")

    units = pack_units(book_id, chunks, config.extraction.work_unit_size)
    report = RunReport(book_id=book_id, units_total=len(units))

    pending = [
        unit
        for unit in units
        if not (resume and unit_state(conn, unit.unit_id) == "completed")
    ]
    for unit in pending:
        set_unit_state(conn, book_id, unit.unit_id, "queued")

    if not pending:
        return report

    make_client = client_factory or (lambda cfg: KaggleClient(cfg))
    client = make_client(_kaggle_config_for_extraction(config))

    for unit in pending:
        ok, hours, chunks_ok, chunks_dead = _run_unit(
            config, conn, unit, client, on_event
        )
        report.gpu_hours += hours
        report.chunks_ok += chunks_ok
        report.chunks_dead += chunks_dead
        if ok:
            report.units_completed += 1
        else:
            report.units_failed += 1
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orchestrator_run.py -v`
Expected: 7 passed.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/orchestrator/run.py tests/test_orchestrator_run.py
git commit -m "feat: extraction run orchestrator with resumable work units

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 8: Local (zero-egress) tier, embeddings, and CLI

**Files:**
- Create: `lore/extract/embed.py`, `lore/orchestrator/local.py`
- Modify: `lore/inference/ollama.py` (add `embed`), `lore/orchestrator/run.py` (route by provider), `lore/cli.py` (two commands)
- Test: `tests/test_embed.py`, `tests/test_local_tier.py`, `tests/test_cli_extract.py`

**Interfaces:**
- Consumes: everything above; `OllamaProvider(config.ollama)` with `generate`/`healthy`/`aclose`.
- Produces:
  - `OllamaProvider.embed(texts: list[str]) -> list[list[float]]` (POST `/api/embed`, returns 768-dim vectors)
  - `write_unit_outputs(unit, dest, result, vectors, model, embed_model, gpu_seconds) -> None` in `lore/extract/embed.py`  — writes the same four artifacts the kernel writes
  - `run_unit_local(config, unit, dest) -> OutputSummary` in `lore/orchestrator/local.py`
  - CLI: `lore extract <book_id> [--resume]`, `lore quota`

- [ ] **Step 1: Write the failing tests**

`tests/test_embed.py`:
```python
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
```

`tests/test_local_tier.py`:
```python
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
```

`tests/test_cli_extract.py`:
```python
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from lore.cli import app
from lore.db.sqlite import open_db
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book
from lore.orchestrator.run import RunReport

runner = CliRunner()
BOOK = "abcdef0123456789"


def _seed(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "data" / "lore.db")
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
              scene_seq=0, seq=i, text=f"t{i}")
        for i in range(3)
    ]
    ingest_book(conn, book_id=BOOK, title="T", author=None,
                source="upload", file_path="/x/b.epub", chunks=chunks)
    conn.close()


def test_extract_reports_the_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)
    report = RunReport(book_id=BOOK, units_total=1, units_completed=1,
                       chunks_ok=3, chunks_dead=0, gpu_hours=0.5)
    with patch("lore.cli.run_extraction", return_value=report) as run:
        result = runner.invoke(app, ["extract", BOOK])
    assert result.exit_code == 0, result.output
    assert "3 chunks" in result.output
    assert run.call_args.kwargs["resume"] is False


def test_extract_passes_resume(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)
    report = RunReport(book_id=BOOK, units_total=1, units_completed=0)
    with patch("lore.cli.run_extraction", return_value=report) as run:
        result = runner.invoke(app, ["extract", BOOK, "--resume"])
    assert result.exit_code == 0
    assert run.call_args.kwargs["resume"] is True


def test_extract_fails_cleanly_for_unknown_book(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["extract", "nosuchbook"])
    assert result.exit_code == 1
    assert "no chunks" in result.output


def test_quota_reports_the_week(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 0
    assert "remaining" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_embed.py tests/test_local_tier.py tests/test_cli_extract.py -v`
Expected: FAIL — `lore.extract.embed` and `lore.orchestrator.local` do not exist; `extract`/`quota` are not commands.

- [ ] **Step 3: Implement**

Add to `lore/inference/ollama.py` (inside `OllamaProvider`):
```python
    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        response = await self._client.post(
            "/api/embed", json={"model": model, "input": texts}
        )
        response.raise_for_status()
        return response.json()["embeddings"]
```

`lore/extract/embed.py`:
```python
"""Write the four artifacts a unit produces, identically in both tiers."""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from lore.extract.runner import ExtractionResult
from lore.orchestrator.workunit import WorkUnit


def write_unit_outputs(
    unit: WorkUnit,
    dest: Path,
    result: ExtractionResult,
    vectors: list[list[float]],
    model: str,
    embed_model: str,
    gpu_seconds: float,
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    columns: dict[str, list] = {
        key: []
        for key in (
            "chunk_id", "subject", "predicate", "object", "evidence",
            "confidence", "entity_names", "extractor_model", "run_id",
        )
    }
    for item in result.results:
        names = [entity.name for entity in item.extraction.entities]
        for triplet in item.extraction.triplets:
            columns["chunk_id"].append(item.chunk_id)
            columns["subject"].append(triplet.subject)
            columns["predicate"].append(triplet.predicate)
            columns["object"].append(triplet.object)
            columns["evidence"].append(triplet.evidence)
            columns["confidence"].append(triplet.confidence)
            columns["entity_names"].append(names)
            columns["extractor_model"].append(model)
            columns["run_id"].append(unit.unit_id)
    pq.write_table(pa.table(columns), dest / "triplets.parquet")
    pq.write_table(
        pa.table({
            "chunk_id": [chunk.id for chunk in unit.chunks],
            "vector": vectors,
            "embed_model": [embed_model] * len(unit.chunks),
        }),
        dest / "embeddings.parquet",
    )
    with (dest / "dead_letter.jsonl").open("w") as handle:
        for dead in result.dead_letters:
            handle.write(
                json.dumps({
                    "chunk_id": dead.chunk_id,
                    "raw_output": dead.raw_output,
                    "error": dead.error,
                })
                + "\n"
            )
    (dest / "manifest.json").write_text(
        json.dumps({
            "unit_id": unit.unit_id,
            "chunks_in": len(unit.chunks),
            "chunks_ok": len(result.results),
            "chunks_dead": len(result.dead_letters),
            "gpu_seconds": gpu_seconds,
            "model": model,
            "embed_model": embed_model,
        }, indent=2)
    )
```

`lore/orchestrator/local.py`:
```python
"""Tier 2: run a work unit entirely on this machine, zero egress."""
from __future__ import annotations

import time
from pathlib import Path

from lore.config import LoreConfig
from lore.extract.embed import write_unit_outputs
from lore.extract.runner import ChunkInput, extract_chunks
from lore.inference.ollama import OllamaProvider
from lore.orchestrator.workunit import OutputSummary, WorkUnit, validate_output


async def run_unit_local(
    config: LoreConfig, unit: WorkUnit, dest: Path, provider=None
) -> OutputSummary:
    started = time.monotonic()
    owned = provider is None
    active = provider if provider is not None else OllamaProvider(config.ollama)
    try:
        inputs = [ChunkInput(chunk.id, chunk.text) for chunk in unit.chunks]
        result = await extract_chunks(inputs, active)
        vectors = await active.embed(
            [chunk.text for chunk in unit.chunks],
            model=config.extraction.embed_model_ollama,
        )
    finally:
        if owned:
            await active.aclose()
    write_unit_outputs(
        unit, dest, result, vectors,
        model=config.ollama.model,
        embed_model=config.extraction.embed_model_ollama,
        gpu_seconds=time.monotonic() - started,
    )
    return validate_output(unit, dest)
```

Route the local tier inside `lore/orchestrator/run.py`'s `_run_unit`: when `config.extraction_provider == "ollama"`, skip Kaggle entirely —
```python
    if config.extraction_provider == "ollama":
        import asyncio

        from lore.orchestrator.local import run_unit_local

        set_unit_state(conn, unit.book_id, unit.unit_id, "dispatched")
        try:
            summary = asyncio.run(run_unit_local(config, unit, dest))
        except Exception as exc:
            set_unit_state(conn, unit.book_id, unit.unit_id, "failed")
            if on_event is not None:
                on_event(f"{unit.unit_id}: failed — {exc}")
            return False, 0.0, 0, 0
        set_unit_state(conn, unit.book_id, unit.unit_id, "completed")
        return True, 0.0, summary.chunks_ok, summary.chunks_dead
```
Place this block immediately after `_run_unit`'s `dest = ...` line and before the `week = quota.iso_week()` line — local runs consume no Kaggle quota, so they must skip the quota gate entirely.

Add to `lore/cli.py` — imports first:
```python
from lore.orchestrator.quota import iso_week, week_report
from lore.orchestrator.run import run_extraction
```
then the commands (after `books`):
```python
@app.command()
def extract(
    book_id: str,
    resume: bool = typer.Option(False, "--resume", help="skip completed work units"),
) -> None:
    """Extract entities and relationships for an ingested book."""
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        report = run_extraction(
            config, conn, book_id, resume=resume,
            on_event=lambda message: typer.echo(f"       {message}"),
        )
    except ValueError as exc:
        typer.echo(f"extract failed: {exc}")
        raise typer.Exit(1)
    finally:
        conn.close()
    typer.echo(
        f"{report.book_id}: {report.units_completed}/{report.units_total} units, "
        f"{report.chunks_ok} chunks extracted, {report.chunks_dead} dead-lettered, "
        f"{report.gpu_hours:.2f} GPU-hours"
    )
    if report.units_failed:
        typer.echo(f"{report.units_failed} unit(s) failed — rerun with --resume")
        raise typer.Exit(1)


@app.command()
def quota() -> None:
    """Show this week's Kaggle GPU-hour budget."""
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        report = week_report(conn, iso_week())
    finally:
        conn.close()
    typer.echo(
        f"{report['week']}: {report['used_hours']:.2f}h used, "
        f"{report['remaining_hours']:.2f}h remaining across {report['runs']} run(s)"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_embed.py tests/test_local_tier.py tests/test_cli_extract.py -v`
Expected: 8 passed.

- [ ] **Step 5: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git add lore/extract/embed.py lore/orchestrator/local.py lore/orchestrator/run.py lore/inference/ollama.py lore/cli.py tests/test_embed.py tests/test_local_tier.py tests/test_cli_extract.py
git commit -m "feat: zero-egress local tier, embeddings, and extract/quota commands

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

### Task 9: Live acceptance — real Kaggle extraction, local parity, README

**Files:**
- Modify: `README.md` (quickstart gains extract/quota)
- No new production code. This task proves the spec's exit criteria on real infrastructure.

**Interfaces:**
- Consumes: the finished CLI.
- Produces: recorded evidence — real chunk counts, dead-letter rate, GPU-hours, a resumed run, and a sample of extracted triplets.

- [ ] **Step 1: Confirm the local prerequisites**

```bash
uv run lore doctor
~/.local/ollama/bin/ollama list | grep -c nomic-embed-text || ~/.local/ollama/bin/ollama pull nomic-embed-text
```
Expected: all six doctor checks `[ ok ]`; `nomic-embed-text` present after the pull (needed for the Tier 2 parity run). If the Ollama server is not running, start it: `~/.local/ollama/bin/ollama serve &`.

- [ ] **Step 2: Tier 2 parity run on a small book**

Build a short fixture book, ingest it, and extract locally:
```bash
uv run python - <<'PY'
from pathlib import Path
from tests.fixtures import make_epub
body = "<p>" + " ".join(
    f"Captain Vane commanded the ship Meridian and distrusted Governor Hale." 
    for _ in range(12)
) + "</p>"
make_epub(Path("data/inbox/fixture.epub"), "Fixture Book", "Tester", [("One", body)])
print("built data/inbox/fixture.epub")
PY
uv run lore ingest data/inbox/fixture.epub
BOOK=$(uv run python -c "import sqlite3;print(sqlite3.connect('data/lore.db').execute(\"SELECT id FROM books WHERE title='Fixture Book'\").fetchone()[0])")
uv run lore extract "$BOOK"
```
The provider is chosen in `lore.toml`, not by environment variable: set `extraction_provider = "ollama"` before this step and back to `"kaggle"` in Step 5.
Expected: the command completes; `data/extractions/$BOOK/<unit>/triplets.parquet` exists. Inspect it:
```bash
uv run python -c "import pyarrow.parquet as pq,glob;t=pq.read_table(glob.glob('data/extractions/*/*/triplets.parquet')[0]);print(t.num_rows,'triplets');print(t.slice(0,5).to_pydict())"
```
Record the row count and a couple of triplets in the report. A 3B local model will be rough — that is expected and is exactly why Kaggle runs the real thing. **Judge only:** does the output validate and have the right shape? Quality is not this step's gate.

- [ ] **Step 3: Real Kaggle extraction on one unit of Pride & Prejudice**

Set `extraction_provider = "kaggle"` in `lore.toml`, then run one unit's worth by temporarily setting `work_unit_size` small:
Edit `lore.toml`: set `extraction_provider = "kaggle"` and `work_unit_size = 40` (a small first unit keeps the first real run cheap). Then:
```bash
PP=$(uv run python -c "import sqlite3;print(sqlite3.connect('data/lore.db').execute(\"SELECT id FROM books WHERE title LIKE '%Pride%'\").fetchone()[0])")
uv run lore extract "$PP" 2>&1 | tail -20
```
Expected: the first unit dispatches, the kernel installs vLLM, loads Qwen2.5-7B-Instruct-AWQ, and returns artifacts; the CLI prints chunks extracted and GPU-hours. **This is the phase's headline acceptance.** If the kernel errors, pull its log with `uv run kaggle kernels output <user>/lore-extract -p /tmp/klogs` and diagnose before continuing — do not paper over it.
Record: wall-clock, GPU-hours, chunks_ok, chunks_dead, and 3 example triplets from the parquet.

- [ ] **Step 4: Prove resume**

Interrupt the second unit mid-flight (Ctrl-C the CLI while it is polling), then:
```bash
uv run lore extract "$PP" --resume 2>&1 | tail -5
uv run lore quota
```
Expected: `--resume` re-runs only the interrupted/failed unit and skips completed ones; `lore quota` shows GPU-hours used and remaining for the ISO week. Record both outputs.

- [ ] **Step 5: Restore config and update the README**

Set `work_unit_size` back to `250` and `extraction_provider` back to `"kaggle"` in `lore.toml`. Replace the README Quickstart with:
```markdown
## Quickstart

    uv sync
    uv run lore doctor            # checks config, stores, ollama, kaggle auth
    uv run lore doctor --kaggle   # + full free-tier GPU round trip (slow)
    uv run lore ingest book.epub  # parse + chunk + register a book (.epub/.pdf)
    uv run lore books             # list the ingested library
    uv run lore extract <book_id> # extract entities + relationships (Kaggle T4s)
    uv run lore extract <book_id> --resume   # continue after an interrupted run
    uv run lore quota             # this week's free-tier GPU budget

Set `extraction_provider = "ollama"` in `lore.toml` for fully local,
zero-egress extraction (slower, smaller model, no Kaggle involved).
```

- [ ] **Step 6: Full suite, then commit and push**

```bash
uv run pytest -m 'not live' -v
git config user.email   # HARD GATE
git status --short      # data/ must NOT appear
git add README.md lore.toml
git commit -m "docs: quickstart covers extraction and quota commands

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HrWjXfLuqeEWiwvbo3LgPz"
git push
```

---

## Phase 2A Exit Criteria (mirrors the spec)

1. `uv run pytest -m 'not live' -v` — entire suite green, output pristine.
2. `lore extract <book_id>` takes a real ingested novel's unit(s) to `triplets.parquet` + `embeddings.parquet` on free Kaggle quota, hands-off.
3. An interrupted run loses no completed unit; `--resume` continues correctly.
4. `lore quota` reports GPU-hours used/remaining for the current ISO week, matching manifests.
5. `extraction_provider = "ollama"` completes the same pipeline end-to-end on the fixture book with no other change, producing artifacts that pass the same `validate_output` checker.
6. Every chunk is ≤ 1600 chars (Task 1), and every commit is authored `jatinroopchandani3007@gmail.com` and pushed to `origin/main`.
