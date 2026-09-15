# Phase 3 — Hybrid Query Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `lore ask "..."` answers a multi-hop, cross-book question with a cited answer derived from the knowledge graph, and demonstrably beats a pure vector-search baseline on questions it was not tuned against.

**Architecture:** Five stages — embed the question and seed entities from LanceDB; have the LLM pick one hand-written Cypher template and fill its slots; run that template read-only in Kuzu; assemble evidence from edge `evidence` strings plus chunk text from SQLite; synthesize a cited answer. The LLM never writes Cypher. `--baseline` runs only stages 1 and 5, giving the comparison the phase is judged on.

**Tech Stack:** Python 3.12, uv, Typer, Kuzu (Cypher), LanceDB, SQLite, Ollama (`qwen2.5:3b-instruct-q4_K_M` + `nomic-embed-text`), pytest.

**Spec:** `docs/superpowers/specs/2026-09-04-phase-3-query-agent-design.md` — read it before Task 1. The plan argues from the spec; where they differ, the spec wins except where this plan records a deliberate refinement (Task 3).

## Global Constraints

- Python 3.12 via uv. Always `uv run`; never bare `pip`.
- Before every commit: `git config user.email` must print exactly `jatinroopchandani3007@gmail.com`. Anything else → STOP and report BLOCKED.
- Every commit message ends with these two lines:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01GDnBNbkNU43K9BTSeydsrf
  ```
- `git push` after every task. `main` is pushed continuously.
- TDD: write the failing test, watch it fail, implement, watch it pass.
- Full suite `uv run pytest -m 'not live' -v` green AND pristine — zero warnings — before every commit. Baseline entering this plan: **243 passed, 1 deselected**.
- Tests use `tmp_path`. No test may write to the repo's `data/` directory, nor invoke a real Kaggle run, a real Ollama server, or a real GPU. Stub the provider.
- Never match entities by name substring (spec §2.1). Entity lookup matches `Entity.aliases` or traverses `ALIAS_OF`.
- Never interpolate a value into Cypher (spec §2.2). Every slot binds as a `$parameter`.
- `lore/cli.py` keeps `@app.callback()` + `@app.command()`. A single `@app.command()` collapses Typer into single-command mode and breaks every existing subcommand.
- `lore.toml`'s committed default stays `extraction_provider = "kaggle"`. If a task changes it, it changes back before the commit, and `git diff lore.toml` must be empty.

---

## File Structure

| File | Responsibility |
|---|---|
| `lore/query/__init__.py` | empty package marker |
| `lore/query/models.py` | shared frozen dataclasses; no logic, no imports from other query modules |
| `lore/query/templates.py` | the Cypher template library + read-only/parameter guards |
| `lore/query/seed.py` | question → embedding → LanceDB → entity bridge → canonical seed entities |
| `lore/query/select.py` | the one pre-synthesis LLM call: pick a template, fill slots, validate |
| `lore/query/subgraph.py` | run a template; assemble deduped, capped evidence |
| `lore/query/synthesize.py` | build the synthesis prompt; produce a cited answer or a refusal |
| `lore/query/agent.py` | orchestration only — wires stages 1–5, owns the `mode` decision |
| `lore/cli.py` | gains `ask`; `process` sets `books.status = 'graphed'` |
| `lore/ingest/registry.py` | gains `chunks_by_id` |
| `lore/graph/loader.py` | `_entities_where` f-string → bound parameter (spec §2.2) |

---

## Task 1: Load the Holmes corpus

**Files:**
- Modify: `lore.toml` (temporarily, reverted before commit)
- Create: `data/inbox/*.epub` (gitignored — not committed)
- Report: `.superpowers/sdd/2026-09-04-phase-3-query-agent/task-1-report.md`

**Interfaces:**
- Consumes: the existing `lore ingest`, `lore process`, `lore graph` CLI.
- Produces: two Holmes books loaded into Kuzu + LanceDB; the recorded counts that Task 2 reads.

This is a data task, not a code task. No production code changes. It is the first real-data exercise of the cross-book adjudication gate.

- [ ] **Step 1: Fetch both books**

Download *A Study in Scarlet* (Gutenberg #244) and *The Sign of the Four* (#2097) as EPUB into `data/inbox/`. Both are public domain.

```bash
mkdir -p data/inbox
curl -L -o data/inbox/study-in-scarlet.epub https://www.gutenberg.org/ebooks/244.epub.noimages
curl -L -o data/inbox/sign-of-the-four.epub https://www.gutenberg.org/ebooks/2097.epub.noimages
```

If a URL 404s, find the EPUB link on the book's Gutenberg page rather than substituting a different edition — chapter structure varies between editions and the chunker keys off it.

- [ ] **Step 2: Ingest both**

```bash
uv run lore ingest data/inbox/study-in-scarlet.epub
uv run lore ingest data/inbox/sign-of-the-four.epub
uv run lore books
```

Expected: each reports a plausible chapter and chunk count (a short novel should give tens of chapters, not 1, and not hundreds). **If either reports 1 chapter, STOP and report BLOCKED** — that is the EPUB-spine bug Phase 1 fixed, and it means this edition's structure is not being parsed. Record both `book_id`s; every later step needs them.

- [ ] **Step 3: Extract and load the first book**

```bash
time uv run lore process <study_in_scarlet_id>
```

This is a real Kaggle GPU run. Expect tens of work units. Watch the streamed events. If it stops early on quota or a failed unit, `lore process <id> --resume` continues — that path is tested and safe.

- [ ] **Step 4: Extract and load the second book**

```bash
time uv run lore process <sign_of_the_four_id>
```

Loading the second book is what exercises cross-book adjudication: Holmes, Watson and Lestrade already exist as entities from book one.

- [ ] **Step 5: Record the graph state**

```bash
uv run lore graph
```

Then cross-check the counts directly rather than trusting the command:

```bash
uv run python - <<'PY'
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore

config = LoreConfig.load()
kuzu = KuzuStore(config.kuzu_dir / "graph")
for table in ["Book", "Chapter", "Chunk", "Entity"]:
    n = [r[0] for r in kuzu.execute(f"MATCH (n:{table}) RETURN count(n)")][0]
    print(f"{table:10} {n}")
for table in ["REL", "MENTIONS", "ALIAS_OF"]:
    n = [r[0] for r in kuzu.execute(f"MATCH ()-[r:{table}]->() RETURN count(r)")][0]
    print(f"{table:10} {n}")
print("--- entities in BOTH books ---")
for row in kuzu.execute(
    "MATCH (e:Entity) WHERE size(e.books) > 1 RETURN e.id, e.name, e.kind, e.books"
):
    print(tuple(row))
lance = LanceStore(config.lancedb_dir)
print("lance chunks:", lance.count("chunks"), "entities:", lance.count("entities"))
kuzu.close()
PY
```

- [ ] **Step 6: Verify the cross-book merge actually happened**

The `size(e.books) > 1` query above is exit criterion 2. **At least one entity must appear in both books**, and it should be a character who genuinely appears in both — Holmes, Watson or Lestrade.

If **zero** entities span both books, that is a finding, not a failure to hide: report it with the counts, note that the cross-book gate refused every candidate, and include the adjudication evidence if you can obtain it. Do not tune thresholds to force a merge.

If an entity spans both books but is a **false** merge (two different people fused), record that too, with the names and evidence.

- [ ] **Step 7: Write the report and commit**

Nothing in `data/` is committed — it is gitignored. Only the report (also gitignored) and any `lore.toml` revert. Verify `git status --porcelain` shows no unexpected changes and `git diff lore.toml` is empty.

Report must contain: both `book_id`s, chapter/chunk counts, wall-clock and GPU hours for each `process`, the full graph counts, the cross-book entity list, and anything that failed and how it was resolved.

---

## Task 2: Freeze the acceptance question set

**Files:**
- Modify: `docs/superpowers/specs/2026-09-04-phase-3-query-agent-design.md` §8
- Report: `.superpowers/sdd/2026-09-04-phase-3-query-agent/task-2-report.md`

**Interfaces:**
- Consumes: the loaded graph from Task 1.
- Produces: spec §8, populated. Every later task treats it as immutable.

No production code. The deliverable is the frozen acceptance bar, chosen from what the graph actually contains so the questions are answerable, then frozen so they cannot be retuned later to whatever the agent turns out to do well.

- [ ] **Step 1: Survey what the graph can support**

```bash
uv run python - <<'PY'
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore

kuzu = KuzuStore(LoreConfig.load().kuzu_dir / "graph")
print("--- busiest entities ---")
for row in kuzu.execute(
    "MATCH (e:Entity)-[r:REL]-() RETURN e.name, e.kind, e.books, count(r) AS n "
    "ORDER BY n DESC LIMIT 25"
):
    print(tuple(row))
print("--- predicate distribution ---")
for row in kuzu.execute(
    "MATCH ()-[r:REL]->() RETURN r.predicate, count(r) AS n ORDER BY n DESC"
):
    print(tuple(row))
print("--- edges on cross-book entities ---")
for row in kuzu.execute(
    "MATCH (a:Entity)-[r:REL]->(b:Entity) WHERE size(a.books) > 1 "
    "RETURN a.name, r.predicate, b.name, r.book_id, r.chapter_seq LIMIT 40"
):
    print(tuple(row))
kuzu.close()
PY
```

- [ ] **Step 2: Choose 5–8 questions**

Rules for the set — a question qualifies only if all four hold:

1. **It is answerable from the graph.** You can point at the specific entities and edges that answer it, from the survey above.
2. **You verified the answer against the book text**, not against the graph. The graph may be wrong; the acceptance answer is what the novel says.
3. **At least three require more than one hop or more than one book.** Single-hop lookups do not distinguish the agent from vector search, and beating the baseline on three of them is exit criterion 4.
4. **At least one is a cross-book question about an entity in both books** — the `across_books` case, which is the phase's premise made concrete.

Include one or two questions you expect vector search to answer *well*. A set the baseline loses on every question is more likely to be measuring a broken baseline than a good agent.

- [ ] **Step 3: Record each question's vector-only baseline**

`lore ask --baseline` does not exist yet, so approximate it now with the same retrieval the agent will use:

```bash
uv run python - <<'PY'
import asyncio
from lore.config import LoreConfig
from lore.db.lance_store import LanceStore
from lore.extract.embed import QUERY_PREFIX
from lore.inference.ollama import OllamaProvider

QUESTIONS = ["<question 1>", "<question 2>"]  # your chosen set

async def main() -> None:
    config = LoreConfig.load()
    provider = OllamaProvider(config.ollama)
    lance = LanceStore(config.lancedb_dir)
    try:
        for q in QUESTIONS:
            vec = (await provider.embed([QUERY_PREFIX + q],
                                        model=config.extraction.embed_model_ollama))[0]
            print("=" * 70)
            print(q)
            for row in lance.search_chunks(vec, limit=5):
                print(f"  {row['chunk_id']}  {row['text'][:160]!r}")
    finally:
        await provider.aclose()

asyncio.run(main())
PY
```

Record, per question, whether those five chunks alone contain enough to answer it. That is the honest baseline: what a pure vector system would have to work from.

- [ ] **Step 4: Write §8 of the spec**

Replace the placeholder block in §8 with one entry per question, in exactly this shape:

```markdown
### Q1. <the question, verbatim as it will be asked>

- **Answer (verified from the text):** <what the novel actually says>
- **Supporting graph:** <entities and edges that carry it — names and predicates>
- **Hops:** <1 | 2 | cross-book>
- **Vector baseline:** <what the top-5 chunks do and do not contain>
- **Expected verdict:** <graph agent should succeed / baseline should fail>
```

Then replace the "empty by design" note at the top of §8 with:

```markdown
> **Frozen 2026-09-XX by Task 2.** Chosen from the loaded graph so every
> question is answerable, then frozen so none can be retuned to the agent's
> behaviour. No later task may add, remove, or reword an entry. A question the
> agent cannot answer is a finding, recorded in the acceptance report.
```

- [ ] **Step 5: Commit**

```bash
git config user.email   # HARD GATE
git add docs/superpowers/specs/2026-09-04-phase-3-query-agent-design.md
git commit -m "docs: freeze the Phase 3 acceptance question set

Chosen from the real loaded graph so each question is answerable, and
frozen before implementation so none can be retuned to whatever the
agent turns out to do well.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GDnBNbkNU43K9BTSeydsrf"
git push
```

---

## Task 3: Shared models and the Cypher template library

**Files:**
- Create: `lore/query/__init__.py`, `lore/query/models.py`, `lore/query/templates.py`
- Test: `tests/test_query_templates.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SeedEntity(id, name, kind, books, score)`
  - `Template(name, cypher, slots, description)`
  - `TemplateCall(template, slots)`
  - `EdgeRow(subject, predicate, predicate_raw, object, evidence, confidence, book_id, chapter_seq, chunk_id)`
  - `Passage(n, chunk_id, book_id, chapter_seq, text)`
  - `Evidence(edges, passages, truncated)`
  - `Answer(text, passages, mode, template, note)`
  - `TEMPLATES: dict[str, Template]`, `ROW_FIELDS: tuple[str, ...]`, `MAX_EVIDENCE_CHARS = 12000`

**Refinement to spec §6, recorded deliberately:** the spec lists `neighbours` with a `depth` slot of 1–2. This plan splits that into two templates, `neighbours` (depth 1) and `two_hop` — seven templates, none with a slot that switches the query text. A slot that selects between two different Cypher strings *is* two templates, and fewer slots is a materially easier target for a 3B model to fill correctly. Nothing else in §6 changes.

- [ ] **Step 1: Write the failing test**

`tests/test_query_templates.py`:

```python
import re

from lore.query.models import EdgeRow
from lore.query.templates import ROW_FIELDS, TEMPLATES

WRITE_WORDS = ("CREATE", "MERGE", "SET ", "DELETE", "DROP", "ALTER", "COPY")


def test_every_template_is_read_only() -> None:
    for name, template in TEMPLATES.items():
        upper = template.cypher.upper()
        for word in WRITE_WORDS:
            assert word not in upper, f"{name} contains {word!r}"


def test_no_template_interpolates_a_value() -> None:
    # Spec 2.2: every slot binds as a $parameter. An f-string brace or a
    # %s in a template is a value reaching Cypher as text.
    for name, template in TEMPLATES.items():
        assert "{" not in template.cypher, f"{name} has a brace"
        assert "%s" not in template.cypher, f"{name} has a %s"


def test_every_declared_slot_appears_as_a_bound_parameter() -> None:
    for name, template in TEMPLATES.items():
        for slot in template.slots:
            assert f"${slot}" in template.cypher, f"{name} never binds ${slot}"


def test_every_parameter_in_the_cypher_is_a_declared_slot() -> None:
    # The inverse: an unbound $param raises at execution time, and a 3B
    # model filling slots would never know to supply it.
    for name, template in TEMPLATES.items():
        found = set(re.findall(r"\$(\w+)", template.cypher))
        assert found == set(template.slots), f"{name}: {found} != {set(template.slots)}"


def test_every_template_returns_the_uniform_row_shape() -> None:
    # Assembly never branches on which template ran, so every RETURN must
    # alias to the same field names in the same order.
    for name, template in TEMPLATES.items():
        for field in ROW_FIELDS:
            assert f"AS {field}" in template.cypher, f"{name} lacks AS {field}"


def test_row_fields_match_the_edge_row_dataclass() -> None:
    assert ROW_FIELDS == tuple(EdgeRow.__dataclass_fields__)


def test_every_template_is_bounded() -> None:
    for name, template in TEMPLATES.items():
        assert "LIMIT" in template.cypher.upper(), f"{name} is unbounded"


def test_every_template_has_a_description_for_the_model() -> None:
    for name, template in TEMPLATES.items():
        assert len(template.description) > 20, f"{name} description too thin"
        assert template.name == name
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_query_templates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lore.query'`.

- [ ] **Step 3: Write `lore/query/models.py`**

```python
"""Shared types for the query agent. No logic, no cross-module imports."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeedEntity:
    """A canonical entity the question's vector neighbourhood pointed at."""
    id: str
    name: str
    kind: str
    books: tuple[str, ...]
    score: float


@dataclass(frozen=True)
class Template:
    name: str
    cypher: str
    slots: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class TemplateCall:
    template: str
    slots: dict[str, str | int]


@dataclass(frozen=True)
class EdgeRow:
    subject: str
    predicate: str
    predicate_raw: str
    object: str
    evidence: str
    confidence: float
    book_id: str
    chapter_seq: int
    chunk_id: str


@dataclass(frozen=True)
class Passage:
    n: int
    chunk_id: str
    book_id: str
    chapter_seq: int
    text: str


@dataclass(frozen=True)
class Evidence:
    edges: tuple[EdgeRow, ...]
    passages: tuple[Passage, ...]
    truncated: bool


@dataclass(frozen=True)
class Answer:
    """`mode` keeps the spec's four outcomes distinct and testable.

    "graph"           - a template ran and returned rows
    "vector-fallback" - a template ran and returned nothing; answered from
                        chunks alone, and the user is told the graph
                        contributed nothing
    "vector-baseline" - --baseline was asked for
    "empty"           - no seed entities; a legitimate empty answer
    """
    text: str
    passages: tuple[Passage, ...]
    mode: str
    template: str | None
    note: str | None
```

- [ ] **Step 4: Write `lore/query/templates.py`**

`lore/query/__init__.py` is empty.

```python
"""Hand-written Cypher. The LLM picks one and fills its slots; it never
writes Cypher itself.

Read-only holds by construction because these are module constants, not
generated text — see tests/test_query_templates.py, which pins that.
Every slot binds as a $parameter (spec 2.2), and every template aliases
its RETURN to ROW_FIELDS so assembly never branches on which one ran.
"""
from __future__ import annotations

from lore.query.models import Template

MAX_EVIDENCE_CHARS = 12000

ROW_FIELDS: tuple[str, ...] = (
    "subject", "predicate", "predicate_raw", "object", "evidence",
    "confidence", "book_id", "chapter_seq", "chunk_id",
)

_RETURN = (
    "RETURN a.name AS subject, r.predicate AS predicate, "
    "r.predicate_raw AS predicate_raw, b.name AS object, "
    "r.evidence AS evidence, r.confidence AS confidence, "
    "r.book_id AS book_id, r.chapter_seq AS chapter_seq, "
    "r.chunk_id AS chunk_id "
)

TEMPLATES: dict[str, Template] = {
    "neighbours": Template(
        name="neighbours",
        cypher=(
            "MATCH (a:Entity)-[r:REL]->(b:Entity) "
            "WHERE a.id = $entity_id OR b.id = $entity_id "
            + _RETURN + "LIMIT $limit"
        ),
        slots=("entity_id", "limit"),
        description=(
            "Everything one named thing is directly involved in. Use for "
            "'who does X deal with', 'what happens to X', 'tell me about X'."
        ),
    ),
    "two_hop": Template(
        name="two_hop",
        cypher=(
            "MATCH (a:Entity)-[:REL]-(m:Entity)-[r:REL]->(b:Entity) "
            "WHERE a.id = $entity_id AND m.id <> a.id "
            + _RETURN + "LIMIT $limit"
        ),
        slots=("entity_id", "limit"),
        description=(
            "The wider neighbourhood two steps out from one named thing. "
            "Use when the question is about indirect influence or "
            "consequences rather than direct dealings."
        ),
    ),
    "path_between": Template(
        name="path_between",
        cypher=(
            "MATCH (a:Entity)-[:REL]-(m:Entity)-[r:REL]-(b:Entity) "
            "WHERE a.id = $a_id AND b.id = $b_id "
            + _RETURN + "LIMIT $limit"
        ),
        slots=("a_id", "b_id", "limit"),
        description=(
            "How two named things are connected through something in "
            "between. Use for 'how are X and Y related', 'what links X to Y'."
        ),
    ),
    "by_predicate": Template(
        name="by_predicate",
        cypher=(
            "MATCH (a:Entity)-[r:REL]->(b:Entity) "
            "WHERE (a.id = $entity_id OR b.id = $entity_id) "
            "AND r.predicate = $predicate "
            + _RETURN + "LIMIT $limit"
        ),
        slots=("entity_id", "predicate", "limit"),
        description=(
            "One specific kind of relationship involving one named thing. "
            "Use when the question names the relationship: who opposes X, "
            "who works for X, what X is a member of."
        ),
    ),
    "shared_neighbours": Template(
        name="shared_neighbours",
        cypher=(
            "MATCH (a:Entity)-[r:REL]-(b:Entity) "
            "WHERE a.id IN [$a_id, $b_id] "
            + _RETURN + "LIMIT $limit"
        ),
        slots=("a_id", "b_id", "limit"),
        description=(
            "Everything two named things are each involved in, so their "
            "common ground can be compared. Use for 'what do X and Y have "
            "in common', 'how do X and Y differ'."
        ),
    ),
    "across_books": Template(
        name="across_books",
        cypher=(
            "MATCH (a:Entity)-[r:REL]-(b:Entity) "
            "WHERE a.id = $entity_id "
            + _RETURN + "ORDER BY r.book_id, r.chapter_seq LIMIT $limit"
        ),
        slots=("entity_id", "limit"),
        description=(
            "One named thing's involvements across every book it appears "
            "in, ordered by book then chapter. Use when the question "
            "compares books or asks how something differs between them."
        ),
    ),
    "chapter_window": Template(
        name="chapter_window",
        cypher=(
            "MATCH (a:Entity)-[r:REL]-(b:Entity) "
            "WHERE a.id = $entity_id AND r.chapter_seq >= $lo "
            "AND r.chapter_seq <= $hi "
            + _RETURN + "ORDER BY r.chapter_seq LIMIT $limit"
        ),
        slots=("entity_id", "lo", "hi", "limit"),
        description=(
            "One named thing's involvements within a range of chapters. "
            "Use when the question is about a period: early on, later, "
            "by the end, in the first half."
        ),
    ),
}
```

- [ ] **Step 4b: Verify every template against the real Kuzu**

The tests above are static — they never execute Cypher. Phase 2B hit version-specific Kuzu differences (`CALL table_info` column ordering), so **do not assume this Cypher parses.** Run each template against the live database with dummy parameters:

```bash
uv run python - <<'PY'
from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.query.templates import TEMPLATES

kuzu = KuzuStore(LoreConfig.load().kuzu_dir / "graph")
dummy = {"entity_id": "x", "a_id": "x", "b_id": "y", "predicate": "OPPOSES",
         "lo": 0, "hi": 999, "limit": 5}
for name, t in TEMPLATES.items():
    try:
        rows = list(kuzu.execute(t.cypher, {s: dummy[s] for s in t.slots}))
        print(f"{name:20} OK  ({len(rows)} rows)")
    except Exception as exc:
        print(f"{name:20} FAILED: {exc}")
kuzu.close()
PY
```

Every template must print OK. Zero rows is fine — dummy ids match nothing. A parse error is not: fix the Cypher for the installed Kuzu version and **say in your report which templates you had to adjust and why.** If a template cannot be expressed in this Kuzu version, report BLOCKED rather than silently dropping it.

- [ ] **Step 5: Run tests, then commit**

```bash
uv run pytest tests/test_query_templates.py -v      # expect 8 passed
uv run pytest -m 'not live' -v                      # expect 251 passed, 1 deselected
git config user.email   # HARD GATE
git add lore/query/ tests/test_query_templates.py
git commit -m "feat: query models and the read-only Cypher template library

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GDnBNbkNU43K9BTSeydsrf"
git push
```

---

## Task 4: Seeding — question to canonical entities

**Files:**
- Create: `lore/query/seed.py`
- Modify: `lore/ingest/registry.py` (add `chunks_by_id`)
- Test: `tests/test_query_seed.py`

**Interfaces:**
- Consumes: `SeedEntity` (Task 3); `LanceStore.search_chunks(vector, limit) -> list[dict]`; `KuzuStore.execute(cypher, params)`; `QUERY_PREFIX` from `lore.extract.embed`.
- Produces:
  - `async def seed_entities(question, lance, kuzu, embed, *, k=8, max_seeds=6) -> list[SeedEntity]`
  - `def canonical_ids(kuzu, entity_ids) -> list[str]` — resolves alias stubs to canonicals via `ALIAS_OF`
  - `def chunks_by_id(conn, ids) -> dict[str, str]` in `lore/ingest/registry.py`

**Critical interface note.** `make_ollama_pair` in `lore/graph/adjudicate.py` returns an `embed` that applies `DOCUMENT_PREFIX`. **Do not reuse it here.** Query vectors need `QUERY_PREFIX`; using the wrong prefix silently degrades retrieval rather than erroring. `seed_entities` takes `embed` as a parameter and the caller supplies a query-side embedder — Task 8 builds it.

- [ ] **Step 1: Write the failing test**

`tests/test_query_seed.py`:

```python
import sqlite3
from pathlib import Path

import pytest

from lore.db.sqlite import open_db
from lore.extract.embed import QUERY_PREFIX
from lore.ingest.registry import chunks_by_id
from lore.query.seed import canonical_ids, seed_entities


class FakeLance:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.searched_with: list[float] | None = None

    def search_chunks(self, vector: list[float], limit: int = 5) -> list[dict]:
        self.searched_with = vector
        return self._rows[:limit]


class FakeKuzu:
    """Answers the two shapes seed.py issues: ALIAS_OF hop and entity fetch."""

    def __init__(self, aliases: dict[str, str], entities: dict[str, tuple]) -> None:
        self._aliases = aliases
        self._entities = entities

    def execute(self, cypher: str, params: dict | None = None):
        params = params or {}
        if "ALIAS_OF" in cypher:
            return [[i, self._aliases[i]] for i in params["ids"] if i in self._aliases]
        return [list(self._entities[i]) for i in params["ids"] if i in self._entities]


@pytest.mark.asyncio
async def test_query_vector_uses_the_query_prefix(tmp_path: Path) -> None:
    # Index-time vectors carry DOCUMENT_PREFIX; query vectors must carry
    # QUERY_PREFIX or retrieval silently degrades instead of failing.
    seen: list[str] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        seen.extend(texts)
        return [[0.1] * 4 for _ in texts]

    lance = FakeLance([])
    await seed_entities("who is holmes?", lance, FakeKuzu({}, {}), embed)
    assert seen == [QUERY_PREFIX + "who is holmes?"]


@pytest.mark.asyncio
async def test_seeds_come_from_the_chunk_entity_bridge() -> None:
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    lance = FakeLance([
        {"chunk_id": "b:1:0", "entity_ids": ["e1", "e2"], "_distance": 0.10},
        {"chunk_id": "b:1:1", "entity_ids": ["e2"], "_distance": 0.20},
    ])
    kuzu = FakeKuzu({}, {
        "e1": ("e1", "Holmes", "Character", ["b1"]),
        "e2": ("e2", "Watson", "Character", ["b1", "b2"]),
    })
    seeds = await seed_entities("q", lance, kuzu, embed)
    assert {s.name for s in seeds} == {"Holmes", "Watson"}
    assert next(s for s in seeds if s.name == "Watson").books == ("b1", "b2")


@pytest.mark.asyncio
async def test_an_alias_stub_resolves_to_its_canonical() -> None:
    # Spec 2.1: REL edges hang only on canonical nodes. A seed that lands
    # on an alias stub must hop ALIAS_OF or every traversal returns
    # nothing -- the exact failure that made Phase 2B's demo empty.
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    lance = FakeLance([{"chunk_id": "b:1:0", "entity_ids": ["alias1"],
                        "_distance": 0.1}])
    kuzu = FakeKuzu({"alias1": "canon1"},
                    {"canon1": ("canon1", "Sherlock Holmes", "Character", ["b1"])})
    seeds = await seed_entities("q", lance, kuzu, embed)
    assert [s.id for s in seeds] == ["canon1"]
    assert seeds[0].name == "Sherlock Holmes"


@pytest.mark.asyncio
async def test_no_chunks_gives_no_seeds_and_does_not_raise() -> None:
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    assert await seed_entities("q", FakeLance([]), FakeKuzu({}, {}), embed) == []


@pytest.mark.asyncio
async def test_seeds_are_capped_and_ordered_by_best_score() -> None:
    async def embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4]

    lance = FakeLance([
        {"chunk_id": "c0", "entity_ids": ["e3"], "_distance": 0.9},
        {"chunk_id": "c1", "entity_ids": ["e1"], "_distance": 0.1},
        {"chunk_id": "c2", "entity_ids": ["e2"], "_distance": 0.5},
    ])
    kuzu = FakeKuzu({}, {i: (i, i.upper(), "Character", ["b1"])
                         for i in ["e1", "e2", "e3"]})
    seeds = await seed_entities("q", lance, kuzu, embed, max_seeds=2)
    assert [s.id for s in seeds] == ["e1", "e2"]


def test_chunks_by_id_returns_text_for_known_ids_only(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    conn.execute(
        "INSERT INTO books (id, title, author, source, file_path, status) "
        "VALUES ('b1', 'T', NULL, 'upload', '/x', 'chunked')"
    )
    conn.execute(
        "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text) "
        "VALUES ('b1:1:0', 'b1', 1, 0, 0, 'the text')"
    )
    conn.commit()
    assert chunks_by_id(conn, ["b1:1:0", "missing"]) == {"b1:1:0": "the text"}
    assert chunks_by_id(conn, []) == {}
    conn.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_query_seed.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lore.query.seed'`.

If `pytest.mark.asyncio` errors as unknown, add `pytest-asyncio` via `uv add --dev pytest-asyncio` and set `asyncio_mode = "auto"` under `[tool.pytest.ini_options]` in `pyproject.toml`. Check first whether the repo already tests async code — match whatever it already does rather than introducing a second pattern.

- [ ] **Step 3: Add `chunks_by_id` to `lore/ingest/registry.py`**

```python
def chunks_by_id(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    """Chunk text for the given ids. Unknown ids are simply absent."""
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, text FROM chunks WHERE id IN ({marks})", ids
    ).fetchall()
    return {row[0]: row[1] for row in rows}
```

- [ ] **Step 4: Write `lore/query/seed.py`**

```python
"""Question -> the canonical entities a traversal should start from.

The vector index is the entry point, not the answer: LanceDB gives
chunks, each chunk carries the entity ids the loader bridged onto it,
and those ids become traversal seeds.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Protocol

from lore.extract.embed import QUERY_PREFIX
from lore.query.models import SeedEntity

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]


class _Lance(Protocol):
    def search_chunks(self, vector: list[float], limit: int = 5) -> list[dict]: ...


class _Kuzu(Protocol):
    def execute(self, cypher: str, params: dict | None = None): ...


def canonical_ids(kuzu: _Kuzu, entity_ids: list[str]) -> list[str]:
    """Replace alias stubs with the canonical they point at.

    Spec 2.1: REL edges attach only to canonical nodes. A seed left on an
    alias stub traverses to nothing, which is precisely why Phase 2B's
    multi-hop demo returned zero rows.
    """
    if not entity_ids:
        return []
    mapping = {
        row[0]: row[1]
        for row in kuzu.execute(
            "MATCH (a:Entity)-[:ALIAS_OF]->(c:Entity) WHERE a.id IN $ids "
            "RETURN a.id, c.id",
            {"ids": entity_ids},
        )
    }
    seen: list[str] = []
    for entity_id in entity_ids:
        resolved = mapping.get(entity_id, entity_id)
        if resolved not in seen:
            seen.append(resolved)
    return seen


async def seed_entities(
    question: str,
    lance: _Lance,
    kuzu: _Kuzu,
    embed: Embed,
    *,
    k: int = 8,
    max_seeds: int = 6,
) -> list[SeedEntity]:
    """The canonical entities the question's vector neighbourhood points at.

    `embed` must apply QUERY_PREFIX -- this function does it, so callers
    pass a raw embedder. Do NOT pass `make_ollama_pair`'s embed: that one
    applies DOCUMENT_PREFIX, and the mismatch degrades retrieval silently
    instead of failing.
    """
    vector = (await embed([QUERY_PREFIX + question]))[0]
    chunks = lance.search_chunks(vector, limit=k)

    best: dict[str, float] = {}
    for chunk in chunks:
        score = float(chunk.get("_distance", 1.0))
        for entity_id in chunk.get("entity_ids") or []:
            if entity_id not in best or score < best[entity_id]:
                best[entity_id] = score

    if not best:
        return []

    ordered = sorted(best, key=lambda i: (best[i], i))
    resolved = canonical_ids(kuzu, ordered)
    rows = {
        row[0]: row
        for row in kuzu.execute(
            "MATCH (e:Entity) WHERE e.id IN $ids "
            "RETURN e.id, e.name, e.kind, e.books",
            {"ids": resolved},
        )
    }
    seeds: list[SeedEntity] = []
    for entity_id in resolved:
        row = rows.get(entity_id)
        if row is None:
            continue
        seeds.append(
            SeedEntity(
                id=row[0], name=row[1], kind=row[2],
                books=tuple(row[3] or ()),
                score=best.get(entity_id, 1.0),
            )
        )
    return seeds[:max_seeds]
```

- [ ] **Step 5: Run tests, then commit**

```bash
uv run pytest tests/test_query_seed.py -v     # expect 6 passed
uv run pytest -m 'not live' -v                # expect 257 passed, 1 deselected
git config user.email   # HARD GATE
git add lore/query/seed.py lore/ingest/registry.py tests/test_query_seed.py
git commit -m "feat: seed canonical entities from the question's vector neighbourhood

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GDnBNbkNU43K9BTSeydsrf"
git push
```

---

## Task 5: Template selection

**Files:**
- Create: `lore/query/select.py`
- Test: `tests/test_query_select.py`

**Interfaces:**
- Consumes: `TEMPLATES`, `Template`, `TemplateCall`, `SeedEntity` (Tasks 3–4); `OllamaProvider.generate(prompt, schema) -> BaseModel`.
- Produces:
  - `class TemplateChoice(BaseModel)` — `template: str`, `entity: str`, `entity_b: str = ""`, `predicate: str = ""`, `chapter_lo: int = 0`, `chapter_hi: int = 0`
  - `async def select_template(question, seeds, generate, *, limit=40) -> TemplateCall | None`

Returns `None` when the model's choice cannot be honoured. The caller treats that exactly like an empty result — never as a crash.

**Refinement to this task, recorded deliberately (Task 5b supersedes the id-copying design below):** run live against the 3B on the frozen §8 set (8 questions × 3), the id-copying `TemplateChoice` honoured 12/24 choices. Copying ids was the minority failure (40/45 exact); the dominant one was *shape* — both ids packed into `entity` with `entity_b` empty, or `by_predicate` chosen with no predicate because the prompt named none — and selection was nondeterministic because `generate` set no temperature. So, as shipped: the model never sees an id; seeds are rendered `1) Name (Kind, in N book(s))` and `TemplateChoice` is `template: str`, `entities: list[int]` (1-based), `predicate: str = ""`, `chapter_lo: int = 0`, `chapter_hi: int = 0`. Each template line shows its entity count, derived from its `*_id` slots (`entity_count(template)`), and `by_predicate` lists `CURATED_PREDICATES` (not `UNCATEGORIZED`) and rejects anything else after strip + upper. Any number outside `1..len(seeds)` or an empty list → `None`; duplicates are dropped in first-seen order; a 1-entity template takes the first number, a 2-entity template the first two distinct, fewer → `None`. `select_template`'s signature and its `TemplateCall` output are unchanged. `OllamaProvider.generate` gained a backward-compatible `options: dict | None = None` merged over `{"num_predict": MAX_OUTPUT_TOKENS}`. **Binding on Task 8:** `make_query_pair` must build selection's `generate` with `options={"temperature": 0, "seed": 0}` (both are needed; see `lore/graph/adjudicate.py`), and its agent tests must construct `TemplateChoice(template=..., entities=[1])` rather than `entity="e1"`. The code blocks below are the original design, kept for the record.

- [ ] **Step 1: Write the failing test**

`tests/test_query_select.py`:

```python
import pytest

from lore.query.models import SeedEntity
from lore.query.select import TemplateChoice, select_template

SEEDS = [
    SeedEntity(id="e1", name="Holmes", kind="Character", books=("b1", "b2"), score=0.1),
    SeedEntity(id="e2", name="Watson", kind="Character", books=("b1",), score=0.2),
]


def _generate(choice: TemplateChoice):
    async def generate(prompt: str, schema):
        return choice
    return generate


@pytest.mark.asyncio
async def test_a_valid_choice_becomes_a_template_call() -> None:
    call = await select_template(
        "who does holmes deal with?", SEEDS,
        _generate(TemplateChoice(template="neighbours", entity="e1")),
    )
    assert call.template == "neighbours"
    assert call.slots == {"entity_id": "e1", "limit": 40}


@pytest.mark.asyncio
async def test_an_unknown_template_name_is_rejected() -> None:
    # A 3B model will invent names. Rejecting is correct; executing an
    # unknown key would KeyError deep in traversal.
    assert await select_template(
        "q", SEEDS, _generate(TemplateChoice(template="magic", entity="e1"))
    ) is None


@pytest.mark.asyncio
async def test_an_entity_id_outside_the_seeds_is_rejected() -> None:
    # The model may only choose among entities we actually surfaced.
    assert await select_template(
        "q", SEEDS, _generate(TemplateChoice(template="neighbours", entity="e99"))
    ) is None


@pytest.mark.asyncio
async def test_a_template_needing_two_entities_rejects_a_missing_second() -> None:
    assert await select_template(
        "q", SEEDS, _generate(TemplateChoice(template="path_between", entity="e1"))
    ) is None


@pytest.mark.asyncio
async def test_two_entity_template_fills_both_slots() -> None:
    call = await select_template(
        "how are holmes and watson connected?", SEEDS,
        _generate(TemplateChoice(template="path_between", entity="e1",
                                 entity_b="e2")),
    )
    assert call.slots == {"a_id": "e1", "b_id": "e2", "limit": 40}


@pytest.mark.asyncio
async def test_chapter_window_defaults_an_unset_range_to_everything() -> None:
    call = await select_template(
        "what happens to holmes early on?", SEEDS,
        _generate(TemplateChoice(template="chapter_window", entity="e1",
                                 chapter_lo=0, chapter_hi=0)),
    )
    assert call.slots["lo"] == 0
    assert call.slots["hi"] >= 10_000


@pytest.mark.asyncio
async def test_no_seeds_selects_nothing_without_calling_the_model() -> None:
    called = False

    async def generate(prompt: str, schema):
        nonlocal called
        called = True
        raise AssertionError("must not reach the model")

    assert await select_template("q", [], generate) is None
    assert called is False


@pytest.mark.asyncio
async def test_the_prompt_lists_every_template_and_every_seed() -> None:
    seen: dict[str, str] = {}

    async def generate(prompt: str, schema):
        seen["prompt"] = prompt
        return TemplateChoice(template="neighbours", entity="e1")

    await select_template("q", SEEDS, generate)
    from lore.query.templates import TEMPLATES
    for name in TEMPLATES:
        assert name in seen["prompt"]
    assert "Holmes" in seen["prompt"] and "e1" in seen["prompt"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_query_select.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lore.query.select'`.

- [ ] **Step 3: Write `lore/query/select.py`**

```python
"""The one LLM decision before synthesis: which template, filled how.

Picking among seven named options and naming an entity from a supplied
list is a constrained classification -- the kind of thing a 3B model does
reliably, and the reason this phase does not ask it to write Cypher.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from pydantic import BaseModel

from lore.query.models import SeedEntity, TemplateCall
from lore.query.templates import TEMPLATES

Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

MAX_CHAPTER = 1_000_000

_PROMPT = """You are choosing how to search a knowledge graph built from books.

Question: {question}

Entities found in passages related to the question:
{seeds}

Available searches:
{templates}

Choose exactly one search and the entity or entities it should use.
Use only the entity ids listed above. If the question compares two
things, name both. If it asks about a period of the story, give the
chapter range; otherwise leave the chapter numbers at 0.
"""


class TemplateChoice(BaseModel):
    template: str
    entity: str
    entity_b: str = ""
    predicate: str = ""
    chapter_lo: int = 0
    chapter_hi: int = 0


def _slots_for(choice: TemplateChoice, ids: set[str], limit: int) -> dict | None:
    name = choice.template
    a, b = choice.entity, choice.entity_b
    if a not in ids:
        return None
    if name in ("neighbours", "two_hop", "across_books"):
        return {"entity_id": a, "limit": limit}
    if name in ("path_between", "shared_neighbours"):
        if b not in ids or b == a:
            return None
        return ({"a_id": a, "b_id": b, "limit": limit})
    if name == "by_predicate":
        if not choice.predicate.strip():
            return None
        return {"entity_id": a, "predicate": choice.predicate.strip().upper(),
                "limit": limit}
    if name == "chapter_window":
        lo, hi = choice.chapter_lo, choice.chapter_hi
        # An unset range means "no constraint", not "chapter 0 to 0" --
        # which would match nothing and look like an empty graph.
        if hi <= lo:
            lo, hi = 0, MAX_CHAPTER
        return {"entity_id": a, "lo": lo, "hi": hi, "limit": limit}
    return None


async def select_template(
    question: str,
    seeds: list[SeedEntity],
    generate: Generate,
    *,
    limit: int = 40,
) -> TemplateCall | None:
    """Pick a template, or None if the model's choice cannot be honoured.

    None is not an error: the caller treats it exactly like a template
    that returned no rows, and falls back to answering from passages.
    """
    if not seeds:
        return None

    rendered_seeds = "\n".join(
        f"- {s.id}: {s.name} ({s.kind}, in {len(s.books)} book(s))" for s in seeds
    )
    rendered_templates = "\n".join(
        f"- {t.name}: {t.description}" for t in TEMPLATES.values()
    )
    choice = await generate(
        _PROMPT.format(question=question, seeds=rendered_seeds,
                       templates=rendered_templates),
        TemplateChoice,
    )
    if choice.template not in TEMPLATES:
        return None
    slots = _slots_for(choice, {s.id for s in seeds}, limit)
    if slots is None:
        return None
    return TemplateCall(template=choice.template, slots=slots)
```

- [ ] **Step 4: Run tests, then commit**

```bash
uv run pytest tests/test_query_select.py -v   # expect 8 passed
uv run pytest -m 'not live' -v                # expect 265 passed, 1 deselected
git config user.email   # HARD GATE
git add lore/query/select.py tests/test_query_select.py
git commit -m "feat: schema-constrained template selection

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GDnBNbkNU43K9BTSeydsrf"
git push
```

---

## Task 6: Traversal and evidence assembly

**Files:**
- Create: `lore/query/subgraph.py`
- Test: `tests/test_query_subgraph.py`

**Interfaces:**
- Consumes: `TEMPLATES`, `ROW_FIELDS`, `MAX_EVIDENCE_CHARS`, `EdgeRow`, `Passage`, `Evidence`, `TemplateCall` (Task 3); `chunks_by_id` (Task 4).
- Produces:
  - `def run_template(kuzu, call) -> list[EdgeRow]`
  - `def assemble(conn, rows, *, max_chars=MAX_EVIDENCE_CHARS) -> Evidence`
  - `def passages_from_chunks(chunk_rows) -> tuple[Passage, ...]` — for the baseline path, which has no edges

- [ ] **Step 1: Write the failing test**

`tests/test_query_subgraph.py`:

```python
from pathlib import Path

from lore.db.sqlite import open_db
from lore.query.models import EdgeRow, TemplateCall
from lore.query.subgraph import assemble, passages_from_chunks, run_template


class FakeKuzu:
    def __init__(self, rows: list[list]) -> None:
        self._rows = rows
        self.seen: tuple[str, dict] | None = None

    def execute(self, cypher: str, params: dict | None = None):
        self.seen = (cypher, params or {})
        return self._rows


def _row(chunk_id: str = "b1:1:0", subject: str = "Holmes") -> list:
    return [subject, "OPPOSES", "distrusts", "Moriarty", "ev", 0.9,
            "b1", 1, chunk_id]


def _seed(conn, chunk_id: str, text: str, book: str = "b1") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO books (id, title, author, source, file_path, status)"
        " VALUES (?, 'T', NULL, 'upload', '/x', 'chunked')", (book,)
    )
    conn.execute(
        "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
        " VALUES (?, ?, 1, 0, 0, ?)", (chunk_id, book, text)
    )
    conn.commit()


def test_run_template_binds_slots_as_parameters() -> None:
    kuzu = FakeKuzu([_row()])
    run_template(kuzu, TemplateCall(template="neighbours",
                                    slots={"entity_id": "e1", "limit": 40}))
    cypher, params = kuzu.seen
    assert params == {"entity_id": "e1", "limit": 40}
    assert "e1" not in cypher      # the value never reaches the query text


def test_run_template_maps_rows_onto_edge_row() -> None:
    rows = run_template(FakeKuzu([_row()]),
                        TemplateCall(template="neighbours",
                                     slots={"entity_id": "e1", "limit": 40}))
    assert rows == [EdgeRow("Holmes", "OPPOSES", "distrusts", "Moriarty", "ev",
                            0.9, "b1", 1, "b1:1:0")]


def test_an_unknown_template_raises_rather_than_guessing() -> None:
    try:
        run_template(FakeKuzu([]), TemplateCall(template="nope", slots={}))
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def test_assemble_numbers_passages_and_dedupes_chunks(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "first chunk")
    _seed(conn, "b1:2:0", "second chunk")
    rows = [EdgeRow("A", "P", "p", "B", "e", 0.5, "b1", 2, "b1:2:0"),
            EdgeRow("C", "P", "p", "D", "e", 0.5, "b1", 1, "b1:1:0"),
            EdgeRow("E", "P", "p", "F", "e", 0.5, "b1", 1, "b1:1:0")]
    evidence = assemble(conn, rows)
    assert [p.n for p in evidence.passages] == [1, 2]
    # ordered by (book, chapter), not by row order
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0", "b1:2:0"]
    assert evidence.truncated is False
    conn.close()


def test_assemble_skips_a_chunk_id_with_no_row_in_sqlite(tmp_path: Path) -> None:
    # An edge can carry a chunk_id the registry does not know; the edge
    # still counts, but there is no passage to cite.
    conn = open_db(tmp_path / "lore.db")
    _seed(conn, "b1:1:0", "known")
    rows = [EdgeRow("A", "P", "p", "B", "e", 0.5, "b1", 1, "b1:1:0"),
            EdgeRow("C", "P", "p", "D", "e", 0.5, "b1", 1, "ghost")]
    evidence = assemble(conn, rows)
    assert [p.chunk_id for p in evidence.passages] == ["b1:1:0"]
    assert len(evidence.edges) == 2
    conn.close()


def test_assemble_caps_total_characters_and_says_so(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    for i in range(5):
        _seed(conn, f"b1:{i}:0", "x" * 100)
    rows = [EdgeRow("A", "P", "p", "B", "e", 0.5, "b1", i, f"b1:{i}:0")
            for i in range(5)]
    evidence = assemble(conn, rows, max_chars=250)
    assert evidence.truncated is True
    assert sum(len(p.text) for p in evidence.passages) <= 250
    assert len(evidence.passages) >= 1     # never truncate to nothing
    conn.close()


def test_assemble_of_no_rows_is_empty_not_an_error(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    evidence = assemble(conn, [])
    assert evidence.edges == () and evidence.passages == ()
    assert evidence.truncated is False
    conn.close()


def test_passages_from_chunks_numbers_lance_rows() -> None:
    passages = passages_from_chunks([
        {"chunk_id": "b1:1:0", "book_id": "b1", "chapter_seq": 1, "text": "a"},
        {"chunk_id": "b1:2:0", "book_id": "b1", "chapter_seq": 2, "text": "b"},
    ])
    assert [(p.n, p.chunk_id) for p in passages] == [(1, "b1:1:0"), (2, "b1:2:0")]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_query_subgraph.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lore.query.subgraph'`.

- [ ] **Step 3: Write `lore/query/subgraph.py`**

```python
"""Run one template and turn its rows into citable evidence."""
from __future__ import annotations

import sqlite3

from lore.ingest.registry import chunks_by_id
from lore.query.models import EdgeRow, Evidence, Passage, TemplateCall
from lore.query.templates import MAX_EVIDENCE_CHARS, TEMPLATES


def run_template(kuzu, call: TemplateCall) -> list[EdgeRow]:
    """Execute a template. Slot values bind as parameters, never as text."""
    template = TEMPLATES[call.template]
    rows = kuzu.execute(template.cypher, dict(call.slots))
    return [
        EdgeRow(
            subject=str(row[0]), predicate=str(row[1]), predicate_raw=str(row[2]),
            object=str(row[3]), evidence=str(row[4]), confidence=float(row[5]),
            book_id=str(row[6]), chapter_seq=int(row[7]), chunk_id=str(row[8]),
        )
        for row in rows
    ]


def _numbered(items: list[tuple[str, str, int, str]], max_chars: int
              ) -> tuple[tuple[Passage, ...], bool]:
    passages: list[Passage] = []
    used = 0
    truncated = False
    for chunk_id, book_id, chapter_seq, text in items:
        # Always keep the first passage: an answer with no evidence at all
        # is worse than one over budget by a little.
        if passages and used + len(text) > max_chars:
            truncated = True
            break
        passages.append(Passage(n=len(passages) + 1, chunk_id=chunk_id,
                                book_id=book_id, chapter_seq=chapter_seq,
                                text=text))
        used += len(text)
    return tuple(passages), truncated


def assemble(conn: sqlite3.Connection, rows: list[EdgeRow], *,
             max_chars: int = MAX_EVIDENCE_CHARS) -> Evidence:
    """Deduped, ordered, capped evidence for the rows a template returned.

    Chunk text lives in SQLite, not in Kuzu's Chunk node -- the node
    carries only ids and positions.
    """
    if not rows:
        return Evidence(edges=(), passages=(), truncated=False)

    ordered: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda r: (r.book_id, r.chapter_seq, r.chunk_id)):
        if row.chunk_id not in seen:
            seen.add(row.chunk_id)
            ordered.append((row.chunk_id, row.book_id, row.chapter_seq))

    texts = chunks_by_id(conn, [c for c, _, _ in ordered])
    items = [(c, b, s, texts[c]) for c, b, s in ordered if c in texts]
    passages, truncated = _numbered(items, max_chars)
    return Evidence(edges=tuple(rows), passages=passages, truncated=truncated)


def passages_from_chunks(chunk_rows: list[dict]) -> tuple[Passage, ...]:
    """Passages straight from LanceDB rows, for the no-graph paths."""
    return tuple(
        Passage(n=index + 1, chunk_id=str(row["chunk_id"]),
                book_id=str(row.get("book_id", "")),
                chapter_seq=int(row.get("chapter_seq", 0)),
                text=str(row.get("text", "")))
        for index, row in enumerate(chunk_rows)
    )
```

- [ ] **Step 4: Run tests, then commit**

```bash
uv run pytest tests/test_query_subgraph.py -v   # expect 9 passed
uv run pytest -m 'not live' -v                  # expect 274 passed, 1 deselected
git config user.email   # HARD GATE
git add lore/query/subgraph.py tests/test_query_subgraph.py
git commit -m "feat: run a template and assemble capped, citable evidence

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GDnBNbkNU43K9BTSeydsrf"
git push
```

---

## Task 7: Cited synthesis

**Files:**
- Create: `lore/query/synthesize.py`
- Test: `tests/test_query_synthesize.py`

**Interfaces:**
- Consumes: `Evidence`, `Passage`, `Answer` (Task 3); `generate(prompt, schema)`.
- Produces:
  - `class Synthesis(BaseModel)` — `answer: str`, `can_answer: bool`
  - `async def synthesize(question, evidence, generate, *, mode, template=None, note=None) -> Answer`
  - `REFUSAL = "The loaded books do not contain enough to answer that."`

**Refinement to this task, recorded deliberately (Task 7 as shipped; the code blocks below are the original design, kept for the record):** four changes. (1) `synthesize(..., book_titles: dict[str, str] | None = None)` renders passage headers and lead lines as `(Title, chapter N)`, falling back to the book id. (2) Relationship lines are *leads*: `- subject —predicate_raw→ object [n] (Title, chapter N)`, where `[n]` is the kept passage the edge's `chunk_id` maps to; an edge whose chunk was cut by the evidence cap is omitted, identical rendered lines are shown once, and the prompt says leads are extracted automatically, can be wrong, and a claim counts only if the cited passage says it. (3) After generation `cited_numbers` parses `[n]`, `[n, m]`/`[n,m]` and adjacent `[n][m]` (nothing else, e.g. `[1-3]`, counts); `can_answer` false, no citation, or any number not a shown passage → `REFUSAL`. (4) Context size is the caller's: measured on Ollama 0.32.15 CPU, the default context is 4096 and a prompt of ≥ `num_ctx` tokens is cut to `num_ctx/2 + 2` (limit 2050 at the default, 4098 at 8192) keeping only the first 4 tokens — a WARN in the server log, no API error, independent of `num_predict`. Real §8 synthesis prompts measured 551–3422 tokens; a structural worst case (12000 chars of passages + 40 lead lines) is ~6.5k. **Binding on Task 8:** `make_query_pair` must build synthesis's `generate` with `options={"temperature": 0, "seed": 0, "num_ctx": 12288}` (largest prompt + the provider's `num_predict` 4096 + margin covering the worst case; KV cache ≈ 36 KB/token, ~430 MB at 12288 on qwen2.5 3B) — selection's `generate` keeps `{"temperature": 0, "seed": 0}` — and must pass `book_titles={b["id"]: b["title"] for b in list_books(conn)}` into every `synthesize` call, including the vector paths. Agent tests' fake `Synthesis` answers must carry an in-range citation or they come back as `REFUSAL`.

**Refinement 7b, recorded deliberately (quote-checked claims; supersedes change (3) and the last sentence above, everything else in the Task 7 note stands):** Task 7's live run found every honoured answer carried a claim its cited passage does not support, which a bracket check cannot see. The model no longer writes the answer. `Synthesis` is now `claims: list[Claim]`, `can_answer: bool`, with `Claim(text: str, passage: int, quote: str)`. Public `verify_claims(synthesis, passages) -> (kept, [(claim, reason)])` keeps a claim only if `passage` is a shown number (else `"bad passage number"`), the quote has ≥ 4 words (else `"quote too short"`), and the quote appears in *that* passage after normalising both sides: lowercase, curly quotes → straight, en/em dash → `-`, whitespace runs → one space, trim. Nothing looser. The match must also start and end on word boundaries, so a clipped word such as `olmes` fails (else `"quote not in passage"`). `synthesize` composes the answer in code as `"{text} [n]"` per kept claim, joined by spaces, with identical rendered claims shown once. `can_answer` false, or zero kept claims, → `REFUSAL`. `cited_numbers` is removed; only the Task 7 eval script used it. The prompt says quotes are copied word for word, checked by code and discarded if not found, and that a claim may say only what its quote says without combining passages. The Task 7 rules on titles, leads and `can_answer` stay. The check proves a quote exists where it is cited. It does **not** prove the claim reads the quote correctly. **Binding on Task 8:** agent tests' fake `Synthesis` must carry a `Claim` whose quote is at least 4 words copied from a shown passage, or it comes back as `REFUSAL`. The options and `book_titles` wiring above are unchanged.

- [ ] **Step 1: Write the failing test**

`tests/test_query_synthesize.py`:

```python
import pytest

from lore.query.models import Evidence, Passage
from lore.query.synthesize import REFUSAL, Synthesis, synthesize

P1 = Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=1, text="Holmes spoke.")
P2 = Passage(n=2, chunk_id="b1:2:0", book_id="b1", chapter_seq=2, text="Watson wrote.")
EV = Evidence(edges=(), passages=(P1, P2), truncated=False)


def _generate(result: Synthesis, sink: dict | None = None):
    async def generate(prompt: str, schema):
        if sink is not None:
            sink["prompt"] = prompt
        return result
    return generate


@pytest.mark.asyncio
async def test_a_grounded_answer_keeps_its_passages() -> None:
    answer = await synthesize(
        "q", EV, _generate(Synthesis(answer="Holmes spoke [1].", can_answer=True)),
        mode="graph", template="neighbours",
    )
    assert answer.text == "Holmes spoke [1]."
    assert answer.mode == "graph" and answer.template == "neighbours"
    assert answer.passages == (P1, P2)


@pytest.mark.asyncio
async def test_the_model_refusing_produces_the_refusal_text() -> None:
    answer = await synthesize(
        "q", EV, _generate(Synthesis(answer="I think maybe...", can_answer=False)),
        mode="graph",
    )
    assert answer.text == REFUSAL


@pytest.mark.asyncio
async def test_no_passages_refuses_without_calling_the_model() -> None:
    called = False

    async def generate(prompt: str, schema):
        nonlocal called
        called = True
        raise AssertionError("must not reach the model")

    answer = await synthesize(
        "q", Evidence(edges=(), passages=(), truncated=False), generate, mode="empty"
    )
    assert answer.text == REFUSAL
    assert answer.mode == "empty"
    assert called is False


@pytest.mark.asyncio
async def test_the_prompt_numbers_every_passage_and_forbids_outside_knowledge() -> None:
    sink: dict = {}
    await synthesize("q", EV, _generate(
        Synthesis(answer="a [1]", can_answer=True), sink), mode="graph")
    prompt = sink["prompt"]
    assert "[1]" in prompt and "[2]" in prompt
    assert "Holmes spoke." in prompt and "Watson wrote." in prompt
    assert "only" in prompt.lower()


@pytest.mark.asyncio
async def test_edges_are_shown_to_the_model_when_present() -> None:
    from lore.query.models import EdgeRow
    sink: dict = {}
    edge = EdgeRow("Holmes", "OPPOSES", "distrusts", "Moriarty", "ev", 0.9,
                   "b1", 1, "b1:1:0")
    await synthesize(
        "q", Evidence(edges=(edge,), passages=(P1,), truncated=False),
        _generate(Synthesis(answer="a [1]", can_answer=True), sink), mode="graph",
    )
    assert "Holmes" in sink["prompt"] and "OPPOSES" in sink["prompt"]


@pytest.mark.asyncio
async def test_the_note_travels_onto_the_answer() -> None:
    answer = await synthesize(
        "q", EV, _generate(Synthesis(answer="a [1]", can_answer=True)),
        mode="vector-fallback", note="the graph contributed nothing",
    )
    assert answer.note == "the graph contributed nothing"
    assert answer.mode == "vector-fallback"


@pytest.mark.asyncio
async def test_truncated_evidence_is_disclosed_in_the_prompt() -> None:
    sink: dict = {}
    await synthesize(
        "q", Evidence(edges=(), passages=(P1,), truncated=True),
        _generate(Synthesis(answer="a [1]", can_answer=True), sink), mode="graph",
    )
    assert "truncated" in sink["prompt"].lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_query_synthesize.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lore.query.synthesize'`.

- [ ] **Step 3: Write `lore/query/synthesize.py`**

```python
"""Turn assembled evidence into a cited answer, or refuse.

Refusing is a first-class outcome. An answer the evidence does not
support is worse than no answer, because the citation makes it look
checked.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from pydantic import BaseModel

from lore.query.models import Answer, Evidence

Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

REFUSAL = "The loaded books do not contain enough to answer that."

_PROMPT = """Answer the question using only the numbered passages below.

Cite every claim with the passage number in square brackets, like [1].
Use only what the passages say. If they do not contain enough to answer,
set can_answer to false rather than guessing.

Question: {question}
{relations}
Passages:
{passages}
{truncation}"""


class Synthesis(BaseModel):
    answer: str
    can_answer: bool


async def synthesize(
    question: str,
    evidence: Evidence,
    generate: Generate,
    *,
    mode: str,
    template: str | None = None,
    note: str | None = None,
) -> Answer:
    if not evidence.passages:
        return Answer(text=REFUSAL, passages=(), mode=mode, template=template,
                      note=note)

    relations = ""
    if evidence.edges:
        lines = "\n".join(
            f"- {e.subject} {e.predicate} {e.object} (book {e.book_id}, "
            f"chapter {e.chapter_seq})"
            for e in evidence.edges
        )
        relations = f"\nRelationships found in the knowledge graph:\n{lines}\n"

    passages = "\n\n".join(
        f"[{p.n}] (book {p.book_id}, chapter {p.chapter_seq})\n{p.text}"
        for p in evidence.passages
    )
    truncation = (
        "\nSome evidence was truncated to fit; answer from what is shown.\n"
        if evidence.truncated else ""
    )
    result = await generate(
        _PROMPT.format(question=question, relations=relations,
                       passages=passages, truncation=truncation),
        Synthesis,
    )
    text = result.answer if result.can_answer else REFUSAL
    return Answer(text=text, passages=evidence.passages, mode=mode,
                  template=template, note=note)
```

- [ ] **Step 4: Run tests, then commit**

```bash
uv run pytest tests/test_query_synthesize.py -v   # expect 7 passed
uv run pytest -m 'not live' -v                    # expect 281 passed, 1 deselected
git config user.email   # HARD GATE
git add lore/query/synthesize.py tests/test_query_synthesize.py
git commit -m "feat: cited synthesis that refuses rather than guesses

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GDnBNbkNU43K9BTSeydsrf"
git push
```

---

## Task 8: The agent, `lore ask`, and two carried fixes

**Files:**
- Create: `lore/query/agent.py`
- Modify: `lore/cli.py` (add `ask`; `process` sets `books.status = 'graphed'`)
- Modify: `lore/graph/loader.py` (`_entities_where` f-string → bound parameter)
- Test: `tests/test_query_agent.py`, `tests/test_cli_ask.py`

**Interfaces:**
- Consumes: everything from Tasks 3–7.
- Produces:
  - `def make_query_pair(config) -> tuple[Embed, Generate, AClose]` — a query-side embedder applying `QUERY_PREFIX`, plus schema-constrained generate, on one client
  - `async def answer_question(config, conn, kuzu, lance, question, embed, generate, *, baseline=False) -> Answer`

**Two Phase 2B carry-ins land here.** Spec §2.2: `loader._entities_where` interpolates a predicate via f-string — change it to a bound parameter, keeping every loader test green. Spec §5: `lore process` sets `books.status = 'graphed'` on a successful load, so `ask` can tell which books are queryable.

**Refinement to this task, recorded deliberately (Task 8 as shipped, per `task-8-brief.md`. The code blocks below are the original design, kept for the record).** Each item below is a deviation from the plan's steps.
1. **Two generate callables, held in one object.** Selection and synthesis need different options, so the single `generate` is gone. `make_query_pair` is replaced by `make_query_models(config, api_key=None, *, ollama=None, gemini=None) -> QueryModels(embed, select, synthesize, synthesis_model, aclose)`.
   - `select` always runs on Ollama with `SELECTION_OPTIONS = {"temperature": 0, "seed": 0}`.
   - `synthesize` runs on Ollama with `SYNTHESIS_OPTIONS = {"temperature": 0, "seed": 0, "num_ctx": 12288}`, or on Gemini when opted in.
   - `synthesis_model()` names the model that answered.
   - `aclose` closes every client the object opened.
   - The `ollama`/`gemini` parameters are test seams.
2. **`answer_question(conn, kuzu, lance, question, models, *, baseline=False)`.**
   - The plan's `config` parameter was unused, so it is dropped.
   - `book_titles` is built once from `list_books` and passed on every path.
3. **No prefix in the builder's embedder.** `seed_entities` already applies `QUERY_PREFIX` itself (Task 4), so a prefixing embedder would double it. `QUERY_PREFIX` is applied exactly once where a question is embedded: inside `seed_entities`, and in the agent's vector path. A test asserts every embedded text is `QUERY_PREFIX + question`. The embedder is still not `make_ollama_pair`'s, which applies `DOCUMENT_PREFIX`.
4. **Opt-in `[lore.synthesis]`.**
   - Config: `provider: Literal["ollama", "gemini"] = "ollama"`, `models = LITE_MODELS` (`gemini-3.1-flash-lite`, `gemini-3.5-flash-lite`), `extra="forbid"`. The committed `lore.toml` is unchanged, and a test pins it to `ollama`.
   - Structured generation: `GeminiClient.generate_json(prompt, schema)` uses Task 7c's measured shape. It sends `generationConfig {"temperature": 0, "responseMimeType": "application/json"}`, appends the schema instruction, strips a code fence, and validates strictly. Invalid output raises `GeminiError` naming the model and schema; it is not cascaded.
   - The cascade is shared with `generate`, and 401/403 stays fatal. The "every model refused" hint is now per call, so `ask` is not told to re-run `lore disambiguate`.
   - Gemini with no key makes `ask` exit 2 without falling back.
5. **Key file.** `resolve_api_key(env=None, *, key_file=None)` reads the environment, then `key_file`, and never `lore.toml`.
   - A file with any mode bit beyond `0o600` raises `GeminiKeyFileError`, which names the path and mode and says `chmod 600`. The error never carries the key.
   - The file is opt-in per caller. `ask` passes `default_key_file()` (`~/.config/lore/gemini.env`). `disambiguate` still reads the environment only: out of this task's scope, and its no-key test assumes it.
   - Tests isolate `HOME`.
6. **`Answer.model: str | None = None`.** It holds the Ollama model name, or the Gemini id from `last_model`. It stays `None` when no model was asked (the `empty` path, or no passages). A refusal returned by a model still names that model.
7. **Queryable-book gate on ground truth.** A book is queryable if it has a `Book` node in Kuzu, not if `books.status == 'graphed'`. All five real books read `chunked`, including the two in the graph, so the plan's gate would refuse the real corpus.
8. **`ask` is read-only by construction**, which the plan did not require.
   - Kuzu opens with a new `KuzuStore(path, *, read_only=True)` mode: no mkdir, no DDL, and Kuzu refuses writes. `lore/db/kuzu_store.py` was not in the plan's file list.
   - SQLite opens with `file:...?mode=ro`. There is no `open_db`, which migrates, and no `ensure_dirs`.
   - A missing graph, `lore.db` or `lancedb/` gets the same "run `lore process`" refusal, and nothing is created.
   - SQLite may still create its `-wal`/`-shm` side files on a read-only open.
9. **Output.**
   - Sources print `[n] <book title>, chapter <chapter_seq>`, falling back to the Kuzu title, then the book id.
   - The last line is `mode: <mode>[ via <template>][ · answered by <model>]`.
   - Errors also catch `httpx.HTTPError` (a `ConnectError` names the Ollama host), `sqlite3.Error` and `ProviderOutputError`, so a down model is one line with no traceback (spec §7).
10. **`process` sets `graphed` only when it exits 0.** The update runs after the extraction-incomplete check. Step 5 put it before that block, which would have marked a book `graphed` while `process` exits 1.
11. **Loader.** `_entities_where` had three callers, not two. It becomes three named literal queries (`_ENTITIES_BY_ID`, `_ENTITIES_BY_KIND`, `_ENTITIES_BY_BOOK`) plus a shared row unpacker, `_entities_from`. A new AST test asserts no loader Cypher is built by f-string or `.format`. Every existing loader test is untouched and green.
12. **Tests.**
   - The agent's `FakeKuzu` needs a `count(r)` branch for `live_entity_ids` (Task 4b).
   - Fake `Synthesis` objects carry a real ≥ 4-word quote (Task 7b).
   - `test_every_existing_subcommand_still_resolves` reads the command list from `cli.py`.
13. **Name clash.** `cli.py` already imported `ask` from `lore.resolve.disambiguation`. A `def ask` command silently shadowed it, and `lore disambiguate` broke; five of its tests caught this. The import is now `ask as ask_disambiguation`.
14. **Ollama timeout.** `OllamaProvider(config, *, timeout=120.0)` gained a backward-compatible `timeout`, and the query side builds it with `OLLAMA_TIMEOUT_S = 600`. On CPU at `num_ctx` 12288, Task 7b's real synthesis calls took 23–170 s. Q1 (153 s) and Q8 (170 s) exceed the old 120 s default, which would turn a slow answer into `ask failed: ReadTimeout`. `lore/inference/ollama.py` was not in the plan's file list.

- [ ] **Step 1: Write the failing agent test**

`tests/test_query_agent.py`:

```python
from pathlib import Path

import pytest

from lore.db.sqlite import open_db
from lore.query.agent import answer_question
from lore.query.models import SeedEntity
from lore.query.select import TemplateChoice
from lore.query.synthesize import REFUSAL, Synthesis

SEED_ROW = {"chunk_id": "b1:1:0", "entity_ids": ["e1"], "_distance": 0.1,
            "book_id": "b1", "chapter_seq": 1, "text": "Holmes spoke."}


class FakeLance:
    def __init__(self, rows): self._rows = rows
    def search_chunks(self, vector, limit=5): return self._rows[:limit]


class FakeKuzu:
    def __init__(self, edge_rows): self._edges = edge_rows
    def execute(self, cypher, params=None):
        if "ALIAS_OF" in cypher:
            return []
        if "RETURN e.id, e.name" in cypher:
            return [["e1", "Holmes", "Character", ["b1"]]]
        return self._edges


def _conn(tmp_path: Path):
    conn = open_db(tmp_path / "lore.db")
    conn.execute("INSERT INTO books (id, title, author, source, file_path, status)"
                 " VALUES ('b1', 'T', NULL, 'upload', '/x', 'graphed')")
    conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                 " VALUES ('b1:1:0', 'b1', 1, 0, 0, 'Holmes spoke.')")
    conn.commit()
    return conn


async def _embed(texts): return [[0.1] * 4 for _ in texts]


def _generate(choice, synthesis):
    async def generate(prompt, schema):
        return choice if schema is TemplateChoice else synthesis
    return generate


EDGE = [["Holmes", "OPPOSES", "distrusts", "Moriarty", "ev", 0.9, "b1", 1, "b1:1:0"]]


@pytest.mark.asyncio
async def test_a_full_graph_answer_reports_mode_graph(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    answer = await answer_question(
        None, conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", _embed,
        _generate(TemplateChoice(template="neighbours", entity="e1"),
                  Synthesis(answer="Holmes opposes Moriarty [1].", can_answer=True)),
    )
    assert answer.mode == "graph"
    assert answer.template == "neighbours"
    assert "[1]" in answer.text
    conn.close()


@pytest.mark.asyncio
async def test_no_seeds_is_an_empty_answer_not_an_error(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    answer = await answer_question(
        None, conn, FakeKuzu([]), FakeLance([]), "q", _embed,
        _generate(TemplateChoice(template="neighbours", entity="e1"),
                  Synthesis(answer="x", can_answer=True)),
    )
    assert answer.mode == "empty"
    assert answer.text == REFUSAL
    conn.close()


@pytest.mark.asyncio
async def test_an_empty_traversal_falls_back_and_says_so(tmp_path: Path) -> None:
    # The important one: degrading to vector search silently, and
    # presenting it as a graph answer, would make the phase's whole
    # claim unfalsifiable.
    conn = _conn(tmp_path)
    answer = await answer_question(
        None, conn, FakeKuzu([]), FakeLance([SEED_ROW]), "q", _embed,
        _generate(TemplateChoice(template="neighbours", entity="e1"),
                  Synthesis(answer="Holmes spoke [1].", can_answer=True)),
    )
    assert answer.mode == "vector-fallback"
    assert answer.note and "graph" in answer.note.lower()
    conn.close()


@pytest.mark.asyncio
async def test_baseline_never_touches_the_graph(tmp_path: Path) -> None:
    class ExplodingKuzu:
        def execute(self, cypher, params=None):
            raise AssertionError("baseline must not query the graph")

    conn = _conn(tmp_path)
    answer = await answer_question(
        None, conn, ExplodingKuzu(), FakeLance([SEED_ROW]), "q", _embed,
        _generate(TemplateChoice(template="neighbours", entity="e1"),
                  Synthesis(answer="Holmes spoke [1].", can_answer=True)),
        baseline=True,
    )
    assert answer.mode == "vector-baseline"
    assert answer.template is None
    conn.close()


@pytest.mark.asyncio
async def test_a_rejected_template_choice_falls_back(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    answer = await answer_question(
        None, conn, FakeKuzu(EDGE), FakeLance([SEED_ROW]), "q", _embed,
        _generate(TemplateChoice(template="invented", entity="e1"),
                  Synthesis(answer="Holmes spoke [1].", can_answer=True)),
    )
    assert answer.mode == "vector-fallback"
    conn.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_query_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lore.query.agent'`.

- [ ] **Step 3: Write `lore/query/agent.py`**

```python
"""Orchestration only. Every stage lives in its own module; this file
decides which of the spec's four outcomes a question landed in.
"""
from __future__ import annotations

import sqlite3
from typing import Awaitable, Callable

from pydantic import BaseModel

from lore.extract.embed import QUERY_PREFIX
from lore.inference.ollama import OllamaProvider
from lore.query.models import Answer, Evidence
from lore.query.seed import seed_entities
from lore.query.select import select_template
from lore.query.subgraph import assemble, passages_from_chunks, run_template
from lore.query.synthesize import synthesize

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]
Generate = Callable[[str, type[BaseModel]], Awaitable[BaseModel]]

GRAPH_EMPTY_NOTE = (
    "the graph had no relationships for this question — "
    "answered from passages alone"
)


def make_query_pair(config):
    """(embed, generate, aclose) for the query side, on one client.

    The embedder here is deliberately NOT the one from
    `lore.graph.adjudicate.make_ollama_pair`: that applies
    DOCUMENT_PREFIX, and a query embedded with the document prefix
    retrieves worse without ever erroring.
    """
    provider = OllamaProvider(config.ollama)

    async def embed(texts: list[str]) -> list[list[float]]:
        return await provider.embed(
            texts, model=config.extraction.embed_model_ollama
        )

    async def generate(prompt: str, schema: type[BaseModel]) -> BaseModel:
        return await provider.generate(prompt, schema)

    return embed, generate, provider.aclose


async def _vector_answer(conn, lance, question, embed, generate, *, mode, note):
    vector = (await embed([QUERY_PREFIX + question]))[0]
    passages = passages_from_chunks(lance.search_chunks(vector, limit=5))
    evidence = Evidence(edges=(), passages=passages, truncated=False)
    return await synthesize(question, evidence, generate, mode=mode, note=note)


async def answer_question(
    config,
    conn: sqlite3.Connection,
    kuzu,
    lance,
    question: str,
    embed: Embed,
    generate: Generate,
    *,
    baseline: bool = False,
) -> Answer:
    if baseline:
        return await _vector_answer(conn, lance, question, embed, generate,
                                    mode="vector-baseline", note=None)

    seeds = await seed_entities(question, lance, kuzu, embed)
    if not seeds:
        # A legitimate empty answer, not a failure: nothing in the library
        # is close enough to the question to start a traversal from.
        return await synthesize(
            question, Evidence(edges=(), passages=(), truncated=False),
            generate, mode="empty",
        )

    call = await select_template(question, seeds, generate)
    rows = run_template(kuzu, call) if call is not None else []
    if not rows:
        return await _vector_answer(conn, lance, question, embed, generate,
                                    mode="vector-fallback", note=GRAPH_EMPTY_NOTE)

    evidence = assemble(conn, rows)
    return await synthesize(question, evidence, generate, mode="graph",
                            template=call.template)
```

- [ ] **Step 4: Add `ask` to `lore/cli.py`**

Follow the file's existing command style. `ask` opens SQLite, Kuzu and LanceDB, runs one event loop for the whole call so the client is closed on the loop that made the requests (the `_load_book_sync` pattern — a second `asyncio.run()` around `aclose()` raises "Event loop is closed" after a successful answer), and closes Kuzu in a `finally`.

```python
@app.command()
def ask(
    question: str,
    baseline: bool = typer.Option(
        False, "--baseline", help="answer from vector search alone, no graph"
    ),
) -> None:
    """Answer a question from the knowledge graph, with citations.

    Note: every question makes at least two LLM calls (choosing how to
    search, then writing the answer), so this is not instantaneous.
    """
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    graphed = [r for r in list_books(conn) if r["status"] == "graphed"]
    if not graphed:
        typer.echo("no books are loaded into the graph — run `lore process <book_id>`")
        conn.close()
        raise typer.Exit(1)

    kuzu = KuzuStore(config.kuzu_dir / "graph")
    lance = LanceStore(config.lancedb_dir)
    embed, generate, aclose = make_query_pair(config)

    async def _run():
        try:
            return await answer_question(config, conn, kuzu, lance, question,
                                         embed, generate, baseline=baseline)
        finally:
            await aclose()

    try:
        answer = asyncio.run(_run())
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"ask failed: {exc}")
        raise typer.Exit(1)
    finally:
        kuzu.close()
        conn.close()

    typer.echo(answer.text)
    if answer.note:
        typer.echo(f"\nnote: {answer.note}")
    if answer.passages:
        typer.echo("\nsources:")
        for passage in answer.passages:
            typer.echo(f"  [{passage.n}] {passage.chunk_id} "
                       f"(chapter {passage.chapter_seq})")
    typer.echo(f"\nmode: {answer.mode}"
               + (f" via {answer.template}" if answer.template else ""))
```

- [ ] **Step 5: Set `books.status = 'graphed'` in `process`**

In `lore/cli.py`'s `process`, after the success `typer.echo` and before the extraction-report block, mark the book graphed:

```python
    conn = open_db(config.sqlite_path)
    conn.execute("UPDATE books SET status = 'graphed' WHERE id = ?", (book_id,))
    conn.commit()
    conn.close()
```

`process` closes `conn` in its `finally`, so this opens a fresh one. It runs only on the success path — never after `PartialLoadError`, which is why that branch exits before reaching it.

- [ ] **Step 6: Fix the loader f-string (spec §2.2)**

In `lore/graph/loader.py`, `_entities_where` interpolates its predicate into Cypher. Rewrite it so the value binds as a parameter. Every existing loader test must stay green untouched; if the predicate genuinely must vary the query *structure* rather than a value, split it into two named functions instead of interpolating.

- [ ] **Step 7: Write `tests/test_cli_ask.py`**

```python
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from lore.cli import app
from lore.db.sqlite import open_db
from lore.query.models import Answer, Passage

runner = CliRunner()


def _library(tmp_path: Path, status: str) -> None:
    conn = open_db(tmp_path / "data" / "lore.db")
    conn.execute("INSERT INTO books (id, title, author, source, file_path, status)"
                 " VALUES ('b1', 'T', NULL, 'upload', '/x', ?)", (status,))
    conn.commit()
    conn.close()


def test_ask_refuses_when_no_book_is_graphed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    _library(tmp_path, "chunked")
    result = runner.invoke(app, ["ask", "who is holmes?"])
    assert result.exit_code == 1
    assert "lore process" in result.output


def test_ask_prints_the_answer_its_sources_and_its_mode(tmp_path: Path,
                                                        monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    _library(tmp_path, "graphed")
    answer = Answer(
        text="Holmes opposes Moriarty [1].",
        passages=(Passage(n=1, chunk_id="b1:1:0", book_id="b1",
                          chapter_seq=1, text="x"),),
        mode="graph", template="neighbours", note=None,
    )

    async def fake_answer(*args, **kwargs):
        return answer

    with patch("lore.cli.answer_question", fake_answer):
        result = runner.invoke(app, ["ask", "q"])
    assert result.exit_code == 0
    assert "Holmes opposes Moriarty [1]." in result.output
    assert "b1:1:0" in result.output
    assert "mode: graph via neighbours" in result.output


def test_ask_surfaces_the_fallback_note(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    _library(tmp_path, "graphed")
    answer = Answer(text="a", passages=(), mode="vector-fallback",
                    template=None, note="the graph contributed nothing")

    async def fake_answer(*args, **kwargs):
        return answer

    with patch("lore.cli.answer_question", fake_answer):
        result = runner.invoke(app, ["ask", "q"])
    assert result.exit_code == 0
    assert "the graph contributed nothing" in result.output


def test_every_existing_subcommand_still_resolves() -> None:
    # A single @app.command() would collapse Typer into single-command
    # mode and silently break the whole CLI.
    for name in ["doctor", "ingest", "books", "extract", "process", "graph",
                 "quota", "ask"]:
        assert runner.invoke(app, [name, "--help"]).exit_code == 0
```

- [ ] **Step 8: Run everything, then commit**

```bash
uv run pytest tests/test_query_agent.py tests/test_cli_ask.py -v   # 9 passed
uv run pytest -m 'not live' -v                  # expect 290 passed, 1 deselected
git config user.email   # HARD GATE
git add lore/query/agent.py lore/cli.py lore/graph/loader.py \
        tests/test_query_agent.py tests/test_cli_ask.py
git commit -m "feat: lore ask, the query agent, and two Phase 2B carry-ins

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GDnBNbkNU43K9BTSeydsrf"
git push
```

---

## Task 9: Acceptance — the frozen set, agent versus baseline

**Files:**
- Modify: `README.md` (quickstart gains `ask`)
- Report: `.superpowers/sdd/2026-09-04-phase-3-query-agent/task-9-report.md`

**Interfaces:**
- Consumes: the finished CLI, the graph from Task 1, spec §8 from Task 2.
- Produces: recorded evidence against every exit criterion in spec §9.

No new production code beyond the README. **Do not edit spec §8** — a question the agent cannot answer is a finding, recorded here.

- [ ] **Step 1: Confirm prerequisites**

```bash
uv run lore doctor
uv run lore books        # both Holmes books must read [graphed]
uv run lore graph
```

If Ollama is down: `~/.local/ollama/bin/ollama serve &`. Models needed: `qwen2.5:3b-instruct-q4_K_M` and `nomic-embed-text`.

- [ ] **Step 2: Run every frozen question both ways**

For each question in spec §8, run and record verbatim:

```bash
uv run lore ask "<question>"
uv run lore ask "<question>" --baseline
```

Record for each: the full answer text, the sources, the `mode` line, and the wall-clock. Then judge against §8's verified answer — **correct / partly correct / wrong / refused** — for both the agent and the baseline. Judge against the recorded answer from the book, not against whichever output reads more fluently.

- [ ] **Step 3: Verify every citation resolves**

Exit criterion 5. For each cited chunk id, confirm the chunk exists and its text supports the claim:

```bash
uv run python - <<'PY'
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.ingest.registry import chunks_by_id

conn = open_db(LoreConfig.load().sqlite_path)
for chunk_id in ["<paste the cited ids>"]:
    text = chunks_by_id(conn, [chunk_id]).get(chunk_id)
    print(chunk_id, "->", (text or "MISSING")[:200])
conn.close()
PY
```

A citation naming a chunk that does not exist, or whose text does not support the claim it is attached to, is a **Critical** finding. Report it; do not quietly drop the question.

- [ ] **Step 4: Exercise every error state**

Spec §7 and exit criterion 6, each by invocation, recording output and exit code:

```bash
uv run lore ask "zqxjkv nonsense that matches nothing"   # expect a clear empty answer, exit 0
uv run lore ask ""                                       # empty question
# stop Ollama, then:
uv run lore ask "who is holmes?"                         # expect a clear message, non-zero, no traceback
```

- [ ] **Step 5: Update the README quickstart**

Add to the quickstart block, after `lore graph`:

```
    uv run lore ask "how are Holmes and Watson connected?"   # cited answer from the graph
    uv run lore ask "..." --baseline                         # vector search alone, for comparison
```

- [ ] **Step 6: Write the report and commit**

The report must state, per exit criterion in spec §9, met or not, with the evidence. It must include a table of every frozen question with the agent verdict and the baseline verdict side by side, and the count of questions where the agent succeeded and the baseline failed — exit criterion 4 needs at least three.

If the agent does not clear the bar, say so plainly with the numbers. Phase 2B closed with two honestly recorded gaps and that was the right outcome; a tuned demo would not have been.

```bash
uv run pytest -m 'not live' -v          # still green, zero warnings
git config user.email   # HARD GATE
git add README.md
git commit -m "docs: quickstart covers lore ask and --baseline

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GDnBNbkNU43K9BTSeydsrf"
git push
```

---

## Self-Review

**Spec coverage.** §1 stores/APIs → Tasks 3–6. §2.1 alias traversal → Task 4 (`canonical_ids`, with its own test). §2.2 bound parameters → Task 3 (template guard tests) and Task 8 Step 6 (the loader carry-in). §2.3 not-free → Task 8's `ask` docstring. §3 staging → Tasks 1, 2, 9 in order. §4 architecture → Tasks 4–8. §5 modules → one task each, plus `chunks_by_id` in Task 4 and `books.status` in Task 8. §6 templates → Task 3, with the seven-template refinement recorded. §7 four error states → `Answer.mode`, tested in Tasks 7, 8, and exercised in Task 9 Step 4. §8 frozen questions → Task 2 produces, Task 9 consumes, no task edits. §9 exit criteria → Task 1 (1, 2), Task 9 (3, 4, 5, 6), Global Constraints (7).

**Placeholder scan.** No TBDs. Task 2's questions are genuinely unknowable before Task 1 runs, so that task specifies the survey commands, the selection rules, and the exact output format rather than the content — the deliverable is defined even though its text is not. Task 1's book ids are runtime values, marked as `<...>` placeholders in shell commands only.

**Type consistency.** `SeedEntity`, `Template`, `TemplateCall`, `EdgeRow`, `Passage`, `Evidence`, `Answer` are defined once in Task 3 and used unchanged after. `ROW_FIELDS` order matches `EdgeRow`'s field order, and Task 3 has a test pinning exactly that. `seed_entities`, `select_template`, `run_template`, `assemble`, `synthesize`, `answer_question` keep one signature throughout. `Answer.mode` takes exactly the four values §7 names.

**One known rough edge, deliberately left.** Task 3's `path_between` and `two_hop` use fixed two-segment patterns rather than Kuzu variable-length paths, because variable-length property extraction is version-sensitive and Phase 2B was bitten by exactly that class of assumption. Step 4b makes the implementer verify every template against the live database and report what it had to change. If richer traversal is wanted later, it is a template edit, not an architecture change.
