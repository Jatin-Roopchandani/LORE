# Phase 3 — Hybrid Query Agent

**Status:** approved in chat 2026-09-04. Implements SPEC.md §9 Phase 3.

**Goal:** `lore ask "..."` answers a multi-hop, cross-book question with a cited
answer derived from the knowledge graph, and demonstrably beats a pure
vector-search baseline on questions it was not tuned against.

---

## 1. What exists already

Phase 2B closed with a working loader and these stores:

**Kuzu** (`lore/db/kuzu_store.py`)
```
Book(id, title, author)
Chapter(id, book_id, seq, title)
Chunk(id, book_id, chapter_seq)              -- NOTE: no text
Entity(id, name, kind, aliases[], books[])
HAS_CHAPTER(Book -> Chapter)   HAS_CHUNK(Chapter -> Chunk)
MENTIONS(Chunk -> Entity)      ALIAS_OF(Entity -> Entity)
REL(Entity -> Entity, predicate, predicate_raw, evidence,
    confidence, chunk_id, book_id, chapter_seq, run_id)
```

**LanceDB** (`lore/db/lance_store.py`) — `chunks` (vector, text, `entity_ids[]`)
and `entities` (centroid vector, name, kind). `search_chunks(vector, limit)`
is the vector entry point.

**SQLite** — `chunks(id, book_id, chapter_seq, scene_seq, seq, text)` is the
canonical home of chunk text. Kuzu's `Chunk` node deliberately carries no text,
so evidence assembly reads text from SQLite by `chunk_id`.

**Embedding** — `nomic-embed-text`, 768-dim. `lore/extract/embed.py` defines
`DOCUMENT_PREFIX = "search_document: "` (applied at index time) and
`QUERY_PREFIX = "search_query: "` (applied here, at query time). Using the
wrong prefix silently degrades retrieval rather than erroring, so the query
path must apply `QUERY_PREFIX` and a test must pin it.

**Inference** — `OllamaProvider.generate(prompt, schema)` returns a
schema-validated Pydantic model; `embed(texts, model)`; `healthy()`; `aclose()`.
`generate` is the only text path and it is schema-constrained.

## 2. Inherited constraints from Phase 2B

Three findings from Phase 2B bind this phase. They are requirements, not advice.

**2.1 Never match entities by name substring.** `REL` edges attach only to
canonical entity nodes, and `display_name` picks the longest raw surface form.
A character introduced as "Captain Vane" but canonicalised to "Governor Hale"
is unreachable by `WHERE e.name CONTAINS 'Vane'` — this is exactly why Phase
2B's own multi-hop demo returned zero rows. Entity lookup MUST match
`Entity.aliases` or traverse `ALIAS_OF`, and a regression test must cover an
entity whose display name differs from the queried surface form.

**2.2 Every Cypher slot is a bound parameter.** Phase 2B deferred a finding
that `loader._entities_where` interpolates a predicate via f-string. No query
in this phase may interpolate a value into Cypher; all slots bind as `$name`.
The deferred loader finding is fixed in this phase.

**2.3 A re-load is not LLM-free and neither is a query.** Resolver-internal
adjudication runs on every load; likewise every `ask` makes at least two LLM
calls (template selection, synthesis). Nothing in the CLI or docs may describe
querying as free or instant.

## 3. Staging — data before code

Phase 3 carries a data dependency in its middle. The order is fixed:

| Stage | Deliverable |
|---|---|
| **0. Data** | Two Sherlock Holmes books ingested, extracted on Kaggle, loaded |
| **1. Freeze** | The acceptance question set, appended to this spec |
| **2. Build** | The query agent, TDD against fixture graphs |
| **3. Accept** | The frozen set run agent-vs-baseline |

**Corpus:** *A Study in Scarlet* and *The Sign of the Four*. Holmes, Watson and
Lestrade appear in both, so this is the first real-data exercise of the
cross-book adjudication gate Phase 2B built and proved only by unit test —
where the same character across two books *should* merge. Both are short
novels, so the run is roughly half the cost of Pride and Prejudice alone.

**Pride and Prejudice is not used.** Its existing artifacts are 5 units of ~74
and predate the current schema: they carry `entity_names` but no
`entity_kinds`, and `loader._named_kinds` defaults a missing kind to
`"Concept"`. Loading them would silently produce a graph in which every entity
is a Concept, with no error. Re-extracting it is a larger run for a corpus that
barely exercises cross-book merging.

**The question set is frozen, not discovered.** Stage 1 inspects the real
loaded graph and writes 5–8 questions into §8 of this spec, each with a
human-verified answer from the text and the vector-only baseline result. Once
appended, **no later task may modify a question**. This is the safeguard
against Phase 2B's ending, where an optimistically pre-written demo query
returned nothing; choosing questions from the real graph makes them
answerable, and freezing them before implementation stops them being retuned
to whatever the agent happens to do well. A question the agent cannot answer
is a finding, not a reason to edit §8.

## 4. Architecture

```
lore ask "How does Watson's role differ between the two books?"
   │
   ├─1 SEED     embed(QUERY_PREFIX + question) -> LanceDB search_chunks(k)
   │            -> union of entity_ids on those chunks
   │            -> canonicalise (hop ALIAS_OF) -> seed entities
   │
   ├─2 SELECT   LLM picks ONE template + fills slots  [schema-constrained]
   │
   ├─3 TRAVERSE run the parameterized template in Kuzu (read-only, LIMITed)
   │            -> bounded subgraph of REL rows
   │
   ├─4 ASSEMBLE per row: evidence string + chunk text (SQLite, by chunk_id)
   │            dedupe, order by (book_id, chapter_seq), cap at 12000 chars
   │
   └─5 SYNTH    Ollama -> answer with inline numbered citations, or refusal
```

`--baseline` runs **stages 1 and 5 only** — retrieved chunks straight to
synthesis, same model, same prompt, no graph. It is a real flag rather than an
acceptance-only script so the comparison can be re-run after any prompt or
model change.

### Why the LLM never writes Cypher

The one LLM decision before synthesis is *which template, with which slots* —
a constrained classification that `generate(prompt, schema)` already enforces
structurally. Phase 2B's evidence is that the local 3B model produced three
self-loops and two false entity merges on a six-sentence passage; asking it to
emit correct Cypher would add a syntax-validation layer, a read-only
enforcement layer, and a retry loop, to buy expressiveness this phase does not
need. Templates are hand-written constants, so read-only holds by construction.

The seam is left clean — `select_template` returns a `TemplateCall`, and
`run_template` consumes one — so a generator could be added behind that
interface later. Nothing is built for it now.

## 5. Modules

All new code lives in `lore/query/`. Each file has one job and is testable
alone.

| File | Responsibility |
|---|---|
| `seed.py` | `seed_entities(question, lance, kuzu, embed, *, k) -> list[SeedEntity]` — embed, vector search, entity bridge, canonicalisation |
| `templates.py` | `TEMPLATES: dict[str, Template]` — the Cypher library; `Template(name, cypher, slots, description)` |
| `select.py` | `select_template(question, seeds, provider) -> TemplateCall` — the one pre-synthesis LLM call |
| `subgraph.py` | `run_template(kuzu, call) -> list[EdgeRow]`; `assemble(conn, rows, *, max_chars) -> Evidence` |
| `synthesize.py` | `synthesize(question, evidence, provider) -> Answer` — cited answer or refusal |
| `agent.py` | `answer(question, *, baseline=False) -> Answer` — orchestration only |

`lore/cli.py` gains `ask`. `lore/ingest/registry.py` gains
`chunks_by_id(conn, ids) -> dict[str, str]` for evidence assembly.

`books.status` finally advances past `chunked` — `lore process` sets it to
`graphed` on a successful load, and `ask` reports which books are queryable.
This was deferred from Phase 2B with Phase 3 named as the consumer.

## 6. The template library

Six templates. Every one returns the identical row shape, so assembly never
branches on which template ran:

```
(subject, predicate, predicate_raw, object, evidence,
 confidence, book_id, chapter_seq, chunk_id)
```

| Template | Slots | Question it answers |
|---|---|---|
| `neighbours` | `entity_id`, `depth` (1–2) | "Who does Holmes deal with?" |
| `path_between` | `a_id`, `b_id`, `max_hops` (≤3) | "How are Watson and Lestrade connected?" |
| `by_predicate` | `entity_id`, `predicate` | "Who opposes Holmes?" |
| `shared_neighbours` | `a_id`, `b_id` | "What do these two have in common?" |
| `across_books` | `entity_id` | "How does Watson's role differ between the books?" |
| `chapter_window` | `entity_id`, `lo`, `hi` | "How does X change over the story?" |

`across_books` is the one that makes the phase's premise concrete: it returns a
single entity's edges partitioned by `book_id`, which is only meaningful
because the cross-book merge gate put one node behind both books.

### Guards

- **Read-only by construction.** Templates are module constants. A test asserts
  every template matches a read-only whitelist and contains no `CREATE`,
  `MERGE`, `SET`, `DELETE` or `DROP`.
- **Bound parameters only.** Slot values pass as Cypher parameters, never
  interpolated (§2.2). A test asserts no template string contains an f-string
  brace or `%s`.
- **Bounded results.** `LIMIT` is baked into every template; `depth ≤ 2`,
  `max_hops ≤ 3`; assembled evidence capped at 12000 characters before synthesis
  (roughly 3k tokens, leaving the 3B model's context to the prompt and
  answer); the cap is a module constant, not a magic number at the call site.
- **Slot validation.** `select_template` output is schema-constrained, but the
  model can still name a template that does not exist or an entity id that is
  not among the seeds. Both are rejected before execution and fall back to §7's
  empty-result path.

## 7. Error states — kept distinct

Phase 2B's recurring defect was a success line printed over incomplete data.
These four outcomes are deliberately different and each has a test:

| Condition | Behaviour | Exit |
|---|---|---|
| No seed entities found | Plain "nothing in the library matches" — a legitimate empty answer, not an error | 0 |
| Template ran, returned no rows | Fall back to vector-only synthesis **with an explicit note that the graph contributed nothing** — never presented as graph-derived | 0 |
| Synthesis cannot ground a claim | Refuse that claim rather than fill it in | 0 |
| Store unreachable, model down, invalid book | Clear one-line message, no traceback | non-zero |

The second is the important one: silently degrading to vector search and
presenting the result as a graph answer would make the phase's central claim
unfalsifiable.

## 8. Acceptance question set

> **This section is empty by design.** It is the deliverable of Stage 1
> (implementation Task 1), which inspects the real loaded graph and appends
> 5–8 questions here, each with: the question, the human-verified answer from
> the text, the entities and edges that support it, and the vector-only
> baseline result. Once appended, no later task may modify an entry. See §3.

## 9. Exit criteria

1. Both Holmes books ingested, extracted and loaded; `lore graph` counts
   cross-checked against Kuzu and LanceDB directly.
2. At least one entity correctly merged across both books through the
   adjudication gate, with `books` holding both ids — the first real-data
   proof of Phase 2B's cross-book guarantee.
3. `lore ask` answers every question in §8 with citations that resolve to real
   chunk ids in the loaded books.
4. `lore ask --baseline` demonstrably fails on at least three of those
   questions where the graph agent succeeds, with both outputs recorded.
5. No answer cites evidence absent from the assembled subgraph.
6. Every §7 error state reachable and correct, verified by invocation.
7. Full suite green and pristine; every commit on the personal account, pushed.

## 10. Out of scope

MCP tools (Phase 4), the web UI (Phase 5), LLM-generated Cypher (§4), book
discovery (Phase 6), and the extraction-quality eval set (Phase 7). The
deferred Phase 2B items not named in §2 — the Ollama provider's error-response
handling, its hardcoded 120 s timeout, and empty dead-letter error strings —
stay deferred unless they block Stage 0.
