# LORE — Local Orchestration & Relational Extraction

**A local-first literary knowledge graph, powered by free-tier cloud burst compute.**

LORE ingests your e-book library (EPUB, PDF), extracts entities, relationships, and narrative arcs across books using open-source LLMs running on **free Kaggle GPUs**, and compiles them into an **embedded graph + vector store on your laptop**. You then ask it questions no ordinary RAG system can answer — multi-hop, cross-book, structural questions — through an agentic query loop, a custom MCP server, and a graph UI. Serving is 100% local; heavy compute is batched to the cloud.

---

## 1. Why This Exists

Standard vector RAG can find a quote. It cannot connect a thematic pattern on page 50 of Book 1 to a character's behavioral shift on page 400 of Book 3, because semantic similarity is not a relationship. LORE turns passive text into a traversable database of literary intelligence:

- **Cross-reference queries:** "Find every instance where a character's internal monologue contradicts their spoken dialogue, grouped by faction."
- **Continuity / lore checking:** "Does the artifact described in Book 1, Chapter 2 violate the rules of magic established in the prequel?"
- **Character arc navigation:** "Show me every event that changed X's allegiance, in chronological order across all five books."
- **Macroscopic theme tracking:** "Trace how the concept of 'honor' shifts across the series and which archetypes champion it."

The engineering value is not the final answer text — it's the **orchestration pipeline**: deterministic chunking, packaging work into resumable cloud batch runs against a weekly GPU quota, entity resolution ("The Dark Lord" ≡ "Voldemort"), and compiling results into embedded, single-file databases that serve multi-hop queries on a modest laptop.

## 2. Goals & Non-Goals

### Goals
1. Ingest EPUB and PDF into clean, deterministic, provenance-tracked chunks — locally.
2. Extract (Subject)-[Predicate]->(Object) triplets + entity metadata with **Llama-3-8B-class models on free Kaggle T4 GPUs**, orchestrated automatically via the Kaggle API (no manual notebook babysitting).
3. Resolve entity aliases into canonical graph nodes.
4. Serve hybrid retrieval — vector search + Cypher graph traversal, fused by an agentic loop — entirely from **embedded local databases** (no Docker, no DB servers).
5. Expose the system as a custom **MCP server** so any MCP client (Claude Code, etc.) can query the graph.
6. Ship a web UI: search box + interactive graph explorer + book library manager.
7. Let users add books by **upload** or by **searching legal, free sources** (public-domain catalogs only).
8. Keep total local footprint under ~5GB (serving layer + data), with an optional fully-offline mode.
9. **Backend portability as a hard requirement:** switching extraction from Kaggle to a local model (or any OpenAI-compatible endpoint) is a **one-line config change** — zero code changes — so that when better local hardware/storage is available, LORE becomes truly zero-egress by flipping a switch. The local path is a maintained, tested, first-class mode, not a degraded afterthought.

### Non-Goals
- No paid APIs anywhere. Cloud compute = Kaggle free tier only; models = open-source only.
- No piracy. Web book discovery is restricted to verifiably free/legal sources (Project Gutenberg, Standard Ebooks, Open Library public-domain items). No shadow-library integrations, ever.
- No multi-user / auth system in v1. Single user, single machine.
- No fine-tuning in v1. Prompt-engineered extraction only.
- Not a general document platform — books and long-form narrative text only.
- No live serving from Kaggle (no tunnels/ngrok — against ToS and fragile by design).

## 3. Compute & Storage Strategy

**Local machine:** laptop, RTX 3050 (4GB VRAM), WSL2, ~15GB usable disk.
**Cloud:** Kaggle free tier — dual T4 (2×16GB), ~30 GPU-hours/week, sessions ≤ 9–12h, no inbound networking, programmatic control via the Kaggle API (datasets + kernel push/status/pull).

The split follows one observation: **extraction is a batch job, serving is not.** Bulk inference needs big GPUs but no interactivity; queries need interactivity but little compute. So:

### Tier 1 — Kaggle Burst Compute (default for ingestion)
- All embedding generation and triplet extraction runs in a parameterized Kaggle notebook on T4s using **Llama-3.1-8B-Instruct (AWQ/GPTQ quant) via vLLM** — meaningfully better extraction quality than anything a 4GB laptop GPU could run.
- Work travels as data, not as API calls: local pipeline **pushes a work-unit dataset** (chunk JSONL + current entity registry snapshot) → **triggers the notebook** via `kaggle kernels push` → **polls status** → **pulls output** (triplets + embeddings as Parquet). ToS-clean: no serving, no tunnels.
- **Run Orchestrator** (local) owns this loop: packaging, quota budgeting against the 30h/week allowance, resumable/idempotent work units, retry on session death, output validation before ingest.

### Tier 2 — Fully-Local Mode (zero-egress, the intended end-state)
- Same pipeline, same stages, same prompts — pointed at local **Ollama** instead of Kaggle. Today that means a 3B Q4 model (Qwen2.5-3B / Llama-3.2-3B fits in 4GB VRAM) and CPU embeddings: slow (a novel overnight) but nothing ever leaves the machine.
- Tier 2 is not a fallback — it is the **designed destination**. Kaggle is the pragmatic default while local hardware/storage is constrained; when that changes (more disk, bigger GPU), the user edits one config line (`extraction.provider: ollama`, plus the model name) and LORE is truly zero-egress with no other change. A bigger local GPU can run the *same* 8B model the Kaggle notebook uses, closing the quality gap entirely.
- It also de-risks Kaggle quota/ToS/connectivity changes and serves as the privacy mode for books you don't want in Google's cloud.

### Query-time compute (always local)
- **Embeddings:** `nomic-embed-text-v1.5` via sentence-transformers on CPU — the *same model* the Kaggle notebook uses for indexing, so query and index vectors live in one space.
- **Agent + synthesis LLM:** local Ollama 3B by default. A handful of short LLM calls per query is well within the 3050. (Configurable to any OpenAI-compatible endpoint if the user has one.)

### The `InferenceProvider` abstraction
Every LLM call site goes through one interface:

```python
class InferenceProvider(Protocol):
    async def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel: ...
```

Implementations: `KaggleBatchProvider` (work-unit dataset round-trip), `OllamaProvider` (local), `OpenAICompatProvider` (any future endpoint — a bigger local GPU serving vLLM, a LAN box, etc.). Tiers are config, not code.

Three rules keep the switch honest:
1. **Single source of truth for extraction logic:** prompts, pydantic schemas, and validation live once in the `lore` package. The Kaggle notebook does not duplicate them — the work-unit dataset ships the relevant `lore` modules, and the notebook imports them. A prompt improvement automatically applies to every provider.
2. **Provider parity is tested:** the test suite runs a small fixture work unit through `OllamaProvider` and through the notebook code path (locally, with a stub model) and asserts schema-identical output shapes. The local path cannot silently rot while Kaggle is the daily driver.
3. **Provenance per run, not per install:** every edge already records `extractor_model` and `run_id`, so a graph built partly on Kaggle-8B and partly on local models stays auditable, and books can be selectively re-extracted after an upgrade.

### Storage: embedded everything
No database servers, no Docker required for serving:

| Store | Engine | Why |
|---|---|---|
| Graph | **Kuzu** (embedded, speaks Cypher) | Single-directory columnar graph DB; full Cypher for the agentic traversal loop; zero-ops |
| Vectors | **LanceDB** (embedded, file-based) | Columnar vector store with payload filtering; no server |
| Catalog & jobs | **SQLite** | Book registry, chunk manifest, pipeline/job state, quota ledger |

Expected data size for a personal library (dozens of books): low single-digit GB.

## 4. Technology Stack

| Layer | Choice | Rationale |
|---|---|---|
| Core language | **Python 3.12** | Best AI ecosystem; local work is I/O-light orchestration |
| Packaging/env | **uv** | Fast, reproducible, single-tool workflow |
| API framework | **FastAPI** | Async-native, pydantic-integrated, serves UI + REST + SSE progress |
| Job state | **SQLite + asyncio** | Pipeline is orchestration + polling, not high-throughput queuing — no Redis needed |
| Cloud compute | **Kaggle API** (`kaggle` CLI/SDK) | Dataset versioning as the work-unit transport; kernel push/status/output as the job API |
| Extraction inference | **vLLM + Llama-3.1-8B-Instruct (quantized)** in the Kaggle notebook | Continuous batching on T4s; strong JSON-schema extraction |
| Local inference | **Ollama** (3B Q4) | Query-time synthesis + zero-egress fallback |
| Embeddings | **nomic-embed-text-v1.5** (sentence-transformers) | Same weights on Kaggle (indexing) and laptop CPU (querying) |
| Graph DB | **Kuzu** | Embedded Cypher engine |
| Vector DB | **LanceDB** | Embedded, file-based |
| Structured output | **pydantic** schemas + validation-with-retry | Extraction correctness gate |
| E-book parsing | **ebooklib** (EPUB), **PyMuPDF** (PDF) | Reliable structure extraction |
| MCP server | **Python MCP SDK** (stdio + Streamable HTTP) | The custom protocol surface |
| UI | **React + TypeScript + Vite**, **Sigma.js** | Sigma renders thousands of nodes; search + panels |
| Observability | **SQLite metrics + UI dashboard page** | Quota ledger, run history, throughput — no Prometheus stack needed at this footprint |

## 5. System Architecture

```
 LOCAL (laptop, ~2–5GB total)                          KAGGLE (free T4s)
┌─────────────────────────────────────────────┐
│              Web UI (React)                 │
│  search box · graph explorer · library mgr  │
│         · pipeline/quota dashboard          │
└───────────────┬─────────────────────────────┘
                │ REST/SSE
┌──────────────┐│                                 ┌──────────────────────────┐
│  MCP Server  ││                                 │  extract.ipynb           │
│ (stdio/http) ▼▼                                 │  (parameterized kernel)  │
│      ┌────────────────────────┐   push dataset  │                          │
│      │   LORE API (FastAPI)   │  (chunks+state) │  vLLM + Llama-3.1-8B     │
│      └──┬──────────────┬──────┘ ────────────▶   │  · embed chunks          │
│         │              │        kernel push /   │  · extract triplets      │
│  ingest jobs     query agent        poll        │  · validate JSON         │
│         │              │        ◀────────────   │                          │
│ ┌───────▼─────────┐    │         pull output    │  outputs: triplets +     │
│ │ Local Pipeline  │    │      (parquet artifacts)│  embeddings (Parquet)   │
│ │ parse → chunk → │    │                         └──────────────────────────┘
│ │ package work    │    │
│ └───────┬─────────┘    │
│ ┌───────▼─────────┐    │
│ │ Run Orchestrator│    │      Tier 2 fallback: same stages
│ │ quota · retries │    │      via local Ollama (slow, zero-egress)
│ │ resume · verify │    │
│ └───────┬─────────┘    │
│ ┌───────▼─────────────┐│
│ │ Ingest & Resolution ││
│ │ entity merge → load ││
│ └──┬────────┬─────────┘│
│    ▼        ▼          ▼
│ ┌──────┐ ┌───────┐ ┌────────┐
│ │ Kuzu │ │LanceDB│ │ SQLite │   + Ollama (3B, query-time)
│ └──────┘ └───────┘ └────────┘
└─────────────────────────────────────────────┘
```

### Components

1. **Ingestion Service (local)** — accepts uploads or discovery downloads; parses EPUB/PDF into a normalized `Book → Chapter → Chunk` structure with stable, deterministic chunk IDs (`{book_hash}:{chapter}:{seq}`). Chunking is structure-aware (chapter/scene boundaries) with sentence-window overlap; re-ingesting the same file yields identical chunks (idempotent pipeline).

2. **Run Orchestrator (local — the infra centerpiece)** — turns Kaggle into a reliable batch backend:
   - *Work units:* pending chunks are packaged into sized batches (calibrated to fit one Kaggle session comfortably, e.g. ~2–4 GPU-hours each) and versioned as a Kaggle Dataset together with a snapshot of the canonical entity registry.
   - *Dispatch:* pushes the parameterized notebook via the Kaggle API, records the run in SQLite, polls status with backoff.
   - *Quota ledger:* tracks estimated vs. actual GPU-hours against the 30h/week allowance; refuses dispatch when the budget would be blown; surfaces the ledger in the UI.
   - *Resilience:* Kaggle sessions can die at any time — every work unit is idempotent, outputs are validated (schema + row counts + checksums) before ingest, failed/partial units are re-queued, and a `--resume` picks up exactly where things stopped.
   - *Fallback routing:* any work unit can be re-routed to the local Tier 2 provider.

3. **Kaggle Extraction Notebook** (`extract.ipynb`, versioned in-repo) — loads the work-unit dataset, spins up vLLM with quantized Llama-3.1-8B, then per chunk: embeds (nomic-embed) and extracts a strict JSON payload — entities (name, type: Character/Place/Faction/Object/Concept/Event), triplets (subject, predicate, object, evidence quote, confidence) — with schema validation and per-chunk retry. Writes Parquet artifacts + a run manifest. Malformed chunks are emitted to a dead-letter file with raw model output for local debugging.

4. **Entity Resolution (local, on ingest of results)** — the "Voldemort ≡ The Dark Lord" problem, three passes:
   - *Exact/alias match:* normalized-name lookup against canonical nodes and alias lists.
   - *Embedding similarity:* candidate name+context vectors vs. canonical entity centroids (threshold-gated), using the embeddings that came back in the same Parquet.
   - *LLM adjudication:* gray-zone candidates get a cheap yes/no call to local Ollama with both entities' evidence quotes. Merges are recorded as `ALIAS_OF` provenance — auditable and reversible.

5. **Graph & Vector Load (local)** — batched `MERGE`-style upserts into Kuzu; embeddings + payloads upserted into LanceDB. Every edge carries provenance: `book_id, chapter, chunk_id, evidence, confidence, extractor_model, run_id`. Loads are transactional per work unit: a unit is either fully queryable or not present.

6. **Query Agent (Hybrid Graph-Vector Loop, local)**:
   1. *Route:* local LLM classifies the question (semantic lookup / relational traversal / hybrid).
   2. *Vector seed:* LanceDB search finds entry chunks → their linked entity IDs are the graph entry points.
   3. *Graph hops:* templated + LLM-generated Cypher (validated, read-only, result-size capped) expands a bounded subgraph in Kuzu.
   4. *Merge & synthesize:* subgraph (as structured text) + top chunks go into the final LLM call, which must cite node/edge provenance.
   5. *Iterate:* if evidence is judged insufficient, loop with refined seeds (max N=3).

7. **MCP Server** — stdio + Streamable HTTP tools:
   - `lore_search(query)` — full hybrid agentic answer with citations
   - `vector_search(query, filters)` — raw chunk retrieval
   - `cypher_query(query)` — read-only, validated Cypher against Kuzu
   - `entity(name)` — canonical entity card: aliases, edges, arc timeline
   - `subgraph(entity, depth)` — bounded subgraph as JSON
   - `library()` / `ingest_status(book_id)` — library and pipeline introspection

8. **Web UI** — single-page React app served by FastAPI:
   - **Search box** → answer panel with citations; cited entities/edges are clickable.
   - **Graph explorer** (Sigma.js): renders the answer's subgraph; click-to-expand neighbors, filter by book/edge type/entity type, timeline slider by chapter order.
   - **Library manager:** upload EPUB/PDF; search free-and-legal catalogs; per-book status.
   - **Pipeline dashboard:** live run states, quota ledger (GPU-hours used/remaining this week), throughput history, dead-letter browser.

9. **Book Discovery Service** — thin adapters over public-domain catalogs (Project Gutenberg via Gutendex API, Standard Ebooks OPDS, Open Library with public-domain filter). Only sources with explicit free/legal licensing; results show license/source; downloads land in the ingestion inbox. Sends only search terms, never book content.

## 6. Data Model

### Graph (Kuzu)
**Node tables:** `Book`, `Chapter`, `Chunk`, `Entity` (with `kind ∈ {Character, Place, Faction, Object, Concept, Event}`, canonical name, alias list)
**Rel tables:**
- Structural: `(Book)-[HAS_CHAPTER]->(Chapter)-[HAS_CHUNK]->(Chunk)`
- Mention: `(Chunk)-[MENTIONS]->(Entity)`
- Extracted: a curated set of first-class typed rels the extractor is prompted toward — `ALLEGIANCE_TO`, `MEMBER_OF`, `LOCATED_IN`, `POSSESSES`, `INTERACTS_WITH`, `OPPOSES`, `LOVES`, `KILLS`, `BECOMES`, `EXPERIENCES` — plus a generic `REL {predicate}` catch-all. All extracted rels carry `{evidence, confidence, chunk_id, book_id, chapter_seq, run_id}`.
- Resolution: `(Entity)-[ALIAS_OF]->(Entity)` provenance for merges.

Chapter sequence numbers give every edge a *narrative timestamp* — this is what makes arc/timeline queries possible.

### Vectors (LanceDB)
Table `chunks`: vector = chunk embedding; columns = `{book_id, chapter_seq, chunk_id, text, entity_ids[]}` — `entity_ids` is the vector→graph bridge. Table `entities`: entity name+context centroids for resolution.

### Interchange formats (local ⇄ Kaggle)
- **Up:** `work_unit.jsonl` (chunk_id, text, context) + `entity_registry.json` snapshot, versioned as a Kaggle Dataset.
- **Down:** `triplets.parquet`, `embeddings.parquet`, `dead_letter.jsonl`, `manifest.json` (counts, model, quant, timings, checksums).

## 7. End-to-End Flows

### Ingestion
1. User uploads an EPUB (or picks a Gutenberg result) in the UI → file lands in the inbox, `Book` record created.
2. Local pipeline parses → deterministic structure-aware chunks → chunk manifest in SQLite.
3. Run Orchestrator packages pending chunks into work units, checks the quota ledger, pushes the dataset + kernel to Kaggle, and polls.
4. Notebook embeds + extracts on T4s → Parquet artifacts → orchestrator pulls and validates them.
5. Local ingest: entity resolution merges into canonical nodes → transactional load into Kuzu + LanceDB.
6. UI dashboard shows the whole journey live; the book flips to "queryable" when all its work units land. *(Tier 2 variant: steps 3–4 run against local Ollama instead.)*

### Query
1. User asks: *"How does Faction A's economic decline affect the loyalty of secondary characters in later books?"*
2. Agent routes → hybrid. CPU embedding → LanceDB seeds chunks about "economic decline" + "Faction A" → entry entities.
3. Cypher hops in Kuzu: characters with `ALLEGIANCE_TO` Faction A, filtered to later `chapter_seq`, expanded along `INTERACTS_WITH`/`OPPOSES`/`BECOMES` → bounded subgraph.
4. Subgraph + evidence chunks → local synthesis LLM → cited answer.
5. UI renders the answer beside the actual subgraph it was derived from — the user sees *why* the answer is what it is.

## 8. Repository Layout

```
LORE/
├── SPEC.md
├── pyproject.toml                  # uv-managed
├── kaggle/
│   ├── extract.ipynb               # parameterized extraction kernel
│   └── kernel-metadata.json        # Kaggle API push config
├── lore/                           # Python package
│   ├── ingest/                     # parsers, chunker, book registry
│   ├── pipeline/                   # stages, schemas, dead-letter, SQLite job state
│   ├── orchestrator/               # Kaggle run orchestrator, quota ledger
│   ├── inference/                  # InferenceProvider impls (kaggle-batch, ollama, openai-compat)
│   ├── resolution/                 # entity resolver
│   ├── graph/                      # kuzu repo, cypher templates, guards
│   ├── vectors/                    # lancedb repo, embedder
│   ├── agent/                      # query agent loop
│   ├── mcp/                        # MCP server
│   └── api/                        # FastAPI app
├── ui/                             # React + TS + Vite + Sigma.js
├── data/                           # kuzu/, lancedb/, lore.db, inbox/  (gitignored)
└── tests/
```

## 9. Build Phases

Each phase ends with something demonstrably working. Later phases never block earlier value.

### Phase 0 — Foundation
uv project scaffold, config system, SQLite schema; Kuzu + LanceDB + Ollama installed and smoke-tested; Kaggle API authenticated; **hello-world round trip**: push a toy dataset, run a trivial GPU kernel, poll, pull output.
**Done when:** one command proves every store opens, local Ollama answers a prompt, and the Kaggle round trip completes programmatically.

### Phase 1 — Ingestion & Chunking
EPUB/PDF parsers, deterministic structure-aware chunker, book registry + chunk manifest. CLI: `lore ingest book.epub`.
**Done when:** ingesting the same book twice is a no-op and chunk boundaries respect chapters/scenes on real novels.

### Phase 2 — Extraction via Kaggle + Graph Build
Extraction prompt + pydantic schemas; `extract.ipynb` with vLLM/8B (embed + extract + validate + dead-letter); **Run Orchestrator v1** (work units, dispatch, poll, pull, verify, resume, quota ledger); entity resolution (all three passes); transactional Kuzu/LanceDB load. Also `OllamaProvider` wiring so Tier 2 runs the same stages.
**Done when:** `lore process` takes a full novel from chunks to a browsable graph using only free Kaggle quota, survives a killed session with `--resume`, "Voldemort"/"The Dark Lord" end up as one node — **and** rerunning the same pipeline with `extraction.provider: ollama` (no other change) completes end-to-end on a short fixture book.

### Phase 3 — Hybrid Query Agent
Router, vector-seed → Cypher-hop loop, Cypher validation/guards, subgraph assembly, cited synthesis via local Ollama. CLI: `lore ask "..."`.
**Done when:** a genuine multi-hop question over 2+ ingested books gets a correct, cited answer that a pure vector-search baseline demonstrably fails.

### Phase 4 — MCP Server
All MCP tools from §5.7, stdio + Streamable HTTP transports, registered in Claude Code and exercised end-to-end.
**Done when:** an external MCP client answers a lore question through the running stack.

### Phase 5 — Web UI
FastAPI REST/SSE endpoints; React app: search+answer panel, Sigma.js graph explorer with expand/filter/timeline, library manager with upload + live progress, pipeline/quota dashboard.
**Done when:** the full loop — upload book → watch Kaggle runs on the dashboard → ask a question → explore the answer subgraph — happens in the browser.

### Phase 6 — Book Discovery
Gutendex / Standard Ebooks / Open Library adapters with license labeling, wired into the library manager.
**Done when:** a public-domain book found in-app is one click away from queryable.

### Phase 7 — Hardening & Distribution
Extraction quality eval set (hand-labeled chunks → precision/recall per model/prompt version); scheduled/queued multi-book processing within weekly quota; backup/restore (the whole state is `data/` — snapshot + verify); one-command install (`uvx lore` / pipx) and a quickstart doc; optional docker-compose profile for the serving layer for users who prefer containers.
**Done when:** a stranger with a Kaggle account can go from `git clone` to a queryable book in under 30 minutes using only the README.

## 10. Key Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Kaggle ToS/quota/API changes | Tier 2 local fallback runs the identical pipeline; `OpenAICompatProvider` escape hatch; work-unit design is backend-agnostic |
| Kaggle sessions die mid-run | Idempotent work units, output validation before ingest, automatic re-queue, `--resume` |
| Books leave the machine (privacy) | Explicit, documented tradeoff in UI/docs; Tier 2 zero-egress mode for sensitive material |
| 30h/week quota too tight for big libraries | Quota ledger + scheduler spreads work across weeks; batching calibrated from measured tokens/sec; quality-vs-cost knob (8B on Kaggle vs 3B local) |
| Extraction quality issues | Strict schema + few-shot prompts, per-chunk retry, dead-letter with raw output, Phase 7 eval set gating prompt/model changes |
| Entity resolution over/under-merging | Gray-zone LLM adjudication; reversible `ALIAS_OF` provenance; per-book review view in UI |
| LLM-generated Cypher unsafe/broken | Template-first, generation-second; read-only execution path; syntax validation; result-size caps |
| Kuzu/LanceDB maturity vs. Neo4j/Qdrant | Both are actively maintained with stable Python APIs; repositories are thin adapters, so swapping engines later touches one module each |
| Embedding drift between index & query | Same pinned model+version (nomic-embed-text-v1.5) in the notebook and locally; manifest records the model per run |

## 11. Success Criteria

1. **Free-tier only:** a full novel goes from EPUB to queryable graph using zero paid services, orchestrated hands-off by `lore process`.
2. **The killer demo:** a multi-hop cross-book question answered correctly with provenance, alongside the visible subgraph, where a plain vector-RAG baseline fails — recorded and reproducible.
3. **Resilience:** killing a Kaggle session mid-extraction loses no committed work; `--resume` completes the book.
4. **Slim edge:** the serving layer (UI + agent + MCP + all data for a dozen books) fits in ~5GB and needs no Docker or database servers.
5. **Interop:** any MCP client can use LORE as a tool server.
6. **Offline mode:** with Tier 2 selected, the entire pipeline and all queries run with the network cable figuratively cut.
7. **The switch is one line:** migrating extraction from Kaggle to a local (or any OpenAI-compatible) backend requires editing only the provider config — verified by the Phase 2 parity test and re-verified whenever prompts or schemas change.
