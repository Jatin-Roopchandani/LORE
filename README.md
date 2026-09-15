# LORE — Local Orchestration & Relational Extraction

A local-first literary knowledge graph: e-books in, entities/relationships out
(extracted on free Kaggle GPUs or fully locally), served from embedded
databases (Kuzu + LanceDB) with a hybrid graph-vector query agent, an MCP
server, and a graph UI.

See `SPEC.md` for the full design. Status: Phase 3 (the query agent) in progress.

## Quickstart

    uv sync
    uv run lore doctor            # checks config, stores, ollama, kaggle auth
    uv run lore doctor --kaggle   # + full free-tier GPU round trip (slow)
    uv run lore ingest book.epub  # parse + chunk + register a book (.epub/.pdf)
    uv run lore books             # list the ingested library
    uv run lore extract <book_id> # extract entities + relationships (Kaggle T4s)
    uv run lore extract <book_id> --resume   # continue after an interrupted run
    uv run lore process <book_id> # extract, resolve entities, load the graph
    uv run lore process <book_id> --skip-extract   # reload the graph only
    uv run lore graph             # node/edge counts and the busiest entities
    uv run lore quota             # this week's free-tier GPU budget

Set `extraction_provider = "ollama"` in `lore.toml` for fully local,
zero-egress extraction (slower, smaller model, no Kaggle involved).

Extraction runs one work unit per Kaggle session. `work_unit_size` is 10 by
default — verified on a Kaggle T4; larger units crash vLLM's engine mid-run.

## Asking questions (`lore ask`)

    uv run lore ask "Who owned the steam launch Aurora?"       # cited answer from the graph
    uv run lore ask "Who owned the steam launch Aurora?" --baseline   # vector search alone

`ask` is read-only. It answers from books that have been loaded into the
graph by `lore process`. The model picks one of a fixed set of graph searches,
then writes the answer as claims. Each claim must quote its cited passage word
for word, and code drops any claim whose quote isn't there. Sources print as
`[n] <book title>, chapter <n>`. The last line gives the mode (`graph`,
`vector-fallback`, `vector-baseline` or `empty`), the search used and the model
that answered.

By default, both LLM calls run on the local Ollama model. To have answers
written by Gemini Flash Lite instead, opt in:

    [lore.synthesis]
    provider = "gemini"   # default "ollama"
    models = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]

With Gemini on identical evidence, the answers misread 0 of 11 cited quotes.
The local 3B misread 8 of 11. Selection stays local, and the quote check
applies either way. Opting in sends the question and its evidence passages to
Google, at one request per question. The key comes from `GEMINI_API_KEY` /
`GOOGLE_API_KEY`, or from a one-line `GEMINI_API_KEY=...` in
`~/.config/lore/gemini.env`. That file is refused unless its mode is 600, and
the key is never read from `lore.toml`. With Gemini chosen and no key found,
`ask` exits. It never quietly answers with the local model instead.

## How it works: from book to answer

LORE turns a book into a knowledge graph in three commands, then answers
questions from that graph with citations.

    lore ingest  ──►  lore process  ──►  (optional) lore disambiguate  ──►  lore ask
    parse+chunk       extract + build        settle ambiguous                 answer
                      the graph              surnames with Gemini

### Where data lives

| Store | Kind | Holds |
|---|---|---|
| **SQLite** (`data/lore.db`) | relational | Book catalog, **full chunk text**, the `jobs` table (the authority on which extraction units finished), the GPU quota ledger |
| **Extraction artifacts** (Parquet under `data/`) | files | Raw model output per work unit: entities, relationships, evidence sentences, chunk vectors |
| **Kuzu** (`data/kuzu/graph`) | graph, Cypher | Nodes `Book`, `Chapter`, `Chunk`, `Entity`; edges `HAS_CHAPTER`, `HAS_CHUNK`, `MENTIONS`, `ALIAS_OF`, and `REL` (the relationships, each carrying its predicate, evidence sentence and chunk id) |
| **LanceDB** (`data/lancedb`) | vectors, 768-d | `chunks`: vector + text + `entity_ids`, the bridge from a passage to its graph entities; `entities`: entity vectors |
| `data/disambiguation/answers.json` | JSON cache | Optional Gemini answers for ambiguous surnames |

### Models

| Model | Runs on | Used for |
|---|---|---|
| Qwen2.5-7B-Instruct (AWQ) | Kaggle T4, vLLM | Extraction (default) |
| nomic-embed-text v1.5 | Kaggle during extraction; local Ollama otherwise | Chunk vectors, entity similarity, predicate mapping, question embedding |
| qwen2.5:3b | Local Ollama | Resolution yes/no calls, choosing a graph search, answers (default), local extraction |
| Gemini Flash Lite (3.1, then 3.5) | Google API, **opt-in** | Surname disambiguation; answers when `[lore.synthesis] provider = "gemini"` |

### 1. `lore ingest`: parse and chunk (no models)

    EPUB / PDF ─► chapters in spine order ─► drop Gutenberg front matter
               ─► scenes (only ***, ---, ### are hard breaks)
               ─► sentences per paragraph (pysbd) ─► ~1,500-char chunks
               ─► SQLite: books row (status "chunked") + chunk rows

Chunk ids are positional: `{book_id}:{chapter_seq}:{seq}`.

### 2. `lore process`, part one: extraction

The book's chunks are packed into work units. By default each unit runs as its
own free Kaggle GPU session:

- **Qwen2.5-7B-Instruct** on vLLM reads each chunk and emits entities,
  relationships and the evidence sentence for each.
- **nomic-embed-text v1.5** embeds every chunk (document prefix).

Results come back as Parquet. A unit counts as finished only when its `jobs`
row says so, never merely because its files exist, and GPU hours are recorded
in the quota ledger. With `extraction_provider = "ollama"` the same step runs
fully locally on qwen2.5:3b and nomic-embed-text: slower and weaker, but
nothing leaves the machine.

### 3. `lore process`, part two: building the graph

    artifacts ─► filter non-entities (pronouns, descriptions, licence text)
              ─► entity resolution ─► relationship mapping
              ─► write Kuzu + LanceDB

**Entity resolution** decides which mentions are the same thing, in three passes:

| Pass | How | Model |
|---|---|---|
| 1. Exact name, alias, or name variant within a book (`Holmes` ⊂ `Sherlock Holmes`) | rules | none |
| 2. Embedding similarity | cosine distance | nomic-embed-text |
| 3. Borderline cases | yes/no question at temperature 0 | qwen2.5:3b |

A bare name that fits **two or more** people (`Sholto` → Major, Thaddeus or
Bartholomew) is refused rather than guessed: the entities stay separate and
the question is recorded. Cross-book merges follow stricter rules.

**Relationship mapping** maps each extracted verb ("distrusts") to one of ten
curated predicates (`KILLS`, `OPPOSES`, `LOVES`, …) by embedding similarity.
The original wording is kept alongside it.

**Writes:** the whole graph to Kuzu; chunk vectors with their `entity_ids`, and
entity vectors, to LanceDB.

**Optional, `lore disambiguate`:** sends every recorded surname question to
Gemini in one batched request and caches the answers; the next `lore process`
applies them. See the Gemini section below.

### 4. `lore ask`: answering a question

    question
      │ 1. EMBED       nomic-embed-text (query prefix)
      ▼
    LanceDB: the 8 most similar chunks ─► their entity_ids
      │ 2. SEED        Kuzu: follow ALIAS_OF to canonical entities, drop any
      │                with no relationships, keep the best 8
      ▼
      │ 3. SELECT      qwen2.5:3b (local, temperature 0) picks one of seven
      │                fixed, read-only Cypher searches and numbered entities
      ▼
      │ 4. TRAVERSE    Kuzu runs that search ─► real directed edges
      ▼
      │ 5. ASSEMBLE    SQLite chunk text for those edges; duplicates removed;
      │                books alternated; capped at 12,000 chars; numbered
      ▼
      │ 6. SYNTHESIZE  qwen2.5:3b (default) or Gemini Flash Lite (opt-in)
      │                ─► claims, each with a passage number and a
      │                   word-for-word quote
      ▼
      │ 7. VERIFY      code checks each quote is really in its passage;
      │                failing claims are dropped; none left ─► refusal
      ▼
    answer + sources (book, chapter) + mode + the model that answered

`ask` never writes: it opens SQLite read-only and Kuzu read-only, and treats a
book as queryable when the graph holds a `Book` node for it.

The model never writes Cypher: it chooses among seven hand-written templates
(`neighbours`, `two_hop`, `path_between`, `by_predicate`, `shared_neighbours`,
`across_books`, `chapter_window`), and every value binds as a parameter.

Every answer reports one of four modes:

- **graph**: a search returned edges, and the answer is built from them.
- **vector-fallback**: the search returned nothing, or the model's choice was
  invalid. The answer comes from plain vector search and **says so**.
- **empty**: nothing in the library was close enough to start from.
- **vector-baseline**: `--baseline`, vector search only, for comparison.

## Optional: the Gemini disambiguator (`lore disambiguate`)

Off by default, and not part of `lore process`. Entity resolution decides
almost everything with deterministic rules; **one** class genuinely needs
judgement and no rule can settle it: a bare surname that is a subset of two
or more candidates. `Major Sholto` normalizes to `sholto`, which nests inside
both `thaddeus sholto` and `bartholomew sholto` — a murdered father and his
two sons — so LORE refuses that merge and leaves the entities separate. Safe,
but lossy: 36 refused pairs on the real corpus, over 17 distinct bare names
(`sholto`, `ferrier`, `smith`, `drebber`, `morstan`, `young`, `lucy`,
`agra`, `baker street`, …).

`lore disambiguate` asks Gemini about exactly that residue, and nothing else.

### One question per bare name, never pairwise

A bare name is asked about **once**, with *every* rival it nests inside
offered at the same time, and exactly one answer accepted:

    mention:    Major Sholto
    sentence:   Major Sholto had been dead for years
    options:    1) Mr. Thaddeus Sholto  2) Mr. Bartholomew Sholto  none

This is not a presentation detail — it is the correctness of the feature.
Asked *pairwise*, the live model says yes to every superset: `Sholto` vs
`Major Sholto` → Yes **and** `Sholto` vs `Thaddeus Sholto` → Yes; `Ferrier`
vs `John Ferrier` → Yes **and** vs `Lucy Ferrier` → Yes. Each answer is
individually reasonable — a bare surname really could be either man — and
nothing in a pairwise question can see that it is one of several, so the
aggregate fuses a murdered father into both his sons: exactly the outcome
the ambiguity refusal exists to prevent. The grouped shape scored 12/12 on
the eval, including answering `none` when the evidence does not
disambiguate. The shape is doing the work, not the model.

Grouping also *reduces* request count: on the real corpus 36 pairwise
refusals collapse to **17** grouped questions, which is one batch instead of
two.

    export GEMINI_API_KEY=...            # or GOOGLE_API_KEY
    uv run lore disambiguate --dry-run   # see the questions, send nothing
    uv run lore disambiguate             # one batched request, answers cached
    uv run lore process <book_id> --skip-extract   # apply the answers

Enable the load-side half in `lore.toml`:

    [lore.disambiguation]
    enabled = true
    # optional: the cascade, in order. The default is below.
    # models = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite",
    #           "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.7-flash"]

A free key comes from <https://aistudio.google.com/apikey>. It is read from
the environment only — never from `lore.toml`, which would put it in git; an
`api_key` line there is a hard config error rather than a silent no-op.

### Quota, and why the design looks like this

Free-tier limits, measured from the console:

| model | RPM | requests/day |
|---|---|---|
| `gemini-3.1-flash-lite`, `gemini-3.5-flash-lite` | 15 | **500** |
| `gemini-3.6/3.7/3.8-flash` | 5 | **20** |

Twenty a day on the Flash tier is what shaped this feature, and it still
governs the fallbacks:

* **The API is not in the load path.** `lore process` stays local, fast and
  offline; it only records the questions it refused. The quota is spent by a
  separate, deliberate command.
* **One request for the whole corpus.** All pending questions go in a single
  numbered batch. 17 questions is ~2k tokens against a 250,000 TPM ceiling —
  token budget is a non-issue, request count is everything.
* **Every answer is cached** under `data/disambiguation/answers.json`, keyed
  by a hash of the question. A reload spends **zero** requests. First run on
  the full corpus: **1 request**. Every reload after that: **0**.
* **The models cascade.** A 429, 503 or 404 moves to the next model instead
  of sleeping, which turns five per-model quotas into ~1100 requests/day of
  headroom for a workload that needs one.

The cascade order is by **measured score and quota, not by version number**.
On a 12-case eval built from the real corpus cases (6 merges that should
happen, 6 that must not, one of them unresolvable), `gemini-3.1-flash-lite`
scored 12/12 and the newer `gemini-3.5-flash-lite` 11/12 — its single miss
was answering with prose where `none` belonged. `gemini-2.5-flash` and
`gemini-2.5-flash-lite` return 404 and are not listed.

A cached *refusal* is an answer too: it is never re-asked, and never costs a
second request. To ask a question again, delete its entry from
`answers.json` (or the whole file) and re-run.

An answer that cannot be read cleanly as an offered option number or `none`
is treated as a **refusal**, never as a guess: under-merging leaves two nodes
a later pass can join, while a wrong merge is unrecoverable. An answer set
that does not line up with its questions — short, long, duplicated or
rotated — is rejected outright and nothing is cached.

### Privacy, and what has not been verified

Sending these questions to Google sends the mention, the entity names, and
**one evidence sentence of the book's text** per question, off your machine.
**Google may train on free-tier data.** That is the tradeoff for the free key;
everything else in LORE runs locally or on Kaggle. The feature is off by
default for exactly this reason, and `--dry-run` shows precisely what would
be sent before anything is.

Verified: the disambiguation quality was checked against real corpus cases —
four live cases originally, then a 12-case eval across the model cascade.
**Not yet verified: the end-to-end pipeline against live quota.** No
`lore process` → `lore disambiguate` → `lore process` cycle has been run
against the real API; every test in this repo stubs the transport.
