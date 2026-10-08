# LORE — Local Orchestration & Relational Extraction

A local-first literary knowledge graph: e-books in, entities/relationships out
(extracted by Gemini Flash Lite, on free Kaggle GPUs, or fully locally), served from embedded
databases (Kuzu + LanceDB) with a hybrid graph-vector query agent, an MCP
server, and a graph UI.

See `SPEC.md` for the full design. Status: Phase 3 (the query agent) complete — see `docs/superpowers/reports/2026-09-17-phase-3-close.md`. Next: Phase 4, the MCP server.

## Quickstart

    uv sync
    uv run lore doctor            # checks config, stores, ollama, kaggle auth
    uv run lore doctor --kaggle   # + full free-tier GPU round trip (slow)
    uv run lore ingest book.epub  # parse + chunk + register a book (.epub/.pdf)
    uv run lore books             # list the ingested library
    uv run lore extract <book_id> # extract entities + relationships (Gemini in this lore.toml)
    uv run lore extract <book_id> --resume   # continue after an interrupted run
    uv run lore process <book_id> # extract, resolve entities, load the graph
    uv run lore process <book_id> --skip-extract   # reload the graph only
    uv run lore graph             # node/edge counts and the busiest entities
    uv run lore ask "how are Holmes and Watson connected?"   # cited answer from the graph
    uv run lore ask "..." --baseline   # vector search alone, for comparison
    uv run lore quota             # this week's free-tier GPU budget (Kaggle only)

`extraction_provider` picks who reads the chunks:

| Value | Where | Notes |
|---|---|---|
| `"gemini"` (**this project's `lore.toml`**) | Google API, one request per chunk | Best measured quality. Needs a key (see below). Embeddings stay local on Ollama |
| `"kaggle"` (code default) | Free Kaggle T4, Qwen2.5-7B on vLLM | One work unit per Kaggle session; GPU hours go in the quota ledger |
| `"ollama"` | This machine, qwen2.5:3b | Zero egress; slower and weaker |

Extraction runs in work units of `work_unit_size` chunks (code default 10,
verified on a Kaggle T4; this `lore.toml` sets 10 for Gemini, see below).

## Asking questions (`lore ask`)

    uv run lore ask "Who owned the steam launch Aurora?"       # cited answer from the graph
    uv run lore ask "Who owned the steam launch Aurora?" --baseline   # vector search alone

`ask` does not modify your books, graph or vectors (it records its Gemini
requests in a ledger table; see below). It answers from books that have been
loaded into the graph by `lore process`. The model picks one of a fixed set of graph searches,
then writes the answer as claims. Each claim must quote its cited passage word
for word, and code drops any claim whose quote isn't there. One further call
then judges every surviving claim against the passage it cites and drops the
ones that passage does not state; if that check cannot run, every claim is
kept and the note says so. Sources print as
`[n] <book title>, chapter <n>`. The last line gives the mode (`graph`,
`vector-fallback`, `vector-baseline` or `empty`), the search used, and the
models: `· selected by X · answered by Y`, or `· selected and answered by X`
when one model did both.

Each of the two LLM calls, choosing the search and writing the answer, has its
own switch. The code default for both is the local Ollama model, so a checkout
with no key and no network still works. **This project's `lore.toml` puts
both on Gemini Flash Lite**, for quality:

    [lore.selection]
    provider = "gemini"   # code default "ollama"

    [lore.synthesis]
    provider = "gemini"   # code default "ollama"
    # either section also takes models = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]

Measured on the frozen questions: Gemini honoured 24 of 24 search choices,
identical across three runs. The local 3B honoured 21 of 24, and its choices
changed between runs at temperature 0 and seed 0 with nothing else changed. On
identical evidence, Gemini's answers misread 0 of 11 cited quotes and the 3B's
misread 8 of 11. The quote check applies whichever model answers.

With both on Gemini, a question costs two requests, and both share one client,
so throttling counts them together. Gemini receives the question, the names of
its seed entities and its evidence passages. The key comes from
`GEMINI_API_KEY` / `GOOGLE_API_KEY`, or from a one-line `GEMINI_API_KEY=...` in
`~/.config/lore/gemini.env`. That file is refused unless its mode is 600, and
the key is never read from `lore.toml`. If either step is on Gemini and no key
is found, `ask` exits. It never quietly uses the local model instead.

## Web UI

```bash
cd web && npm install && npm run build   # once, and after frontend changes
lore ui                                   # http://127.0.0.1:8766
```

Ask a question and watch LORE work: live steps in the chat, and the evidence
graph lighting up as it searches. Click a citation to read the passage, click
a node for its entity card, and expand its neighbours. Loopback only, with no
authentication. Asking spends Gemini requests, like `lore ask`.

For frontend development, run `lore ui --dev` with `cd web && npm run dev`
(http://localhost:5173). Tests: `npm test` (free). The end-to-end test
spends Gemini requests, so it's opt-in: `LORE_E2E=1 npx playwright test`.

## Ask LORE from an MCP client

`lore mcp` serves the library to any MCP client. Register it in `.mcp.json`
with an absolute path, so it works whatever directory the client starts in:

    {
      "mcpServers": {
        "lore": {
          "command": "/home/jatin/LORE/.venv/bin/lore",
          "args": ["mcp"]
        }
      }
    }

There are two transports:

    lore mcp                    # stdio, for a client that spawns it
    lore mcp --http --port 8765 # Streamable HTTP, loopback only (127.0.0.1)

The HTTP server has no authentication, so it binds loopback only: `--host`
accepts 127.0.0.0/8, `localhost` or `::1` and refuses anything else, such as
`0.0.0.0`. It also answers only requests addressed to a loopback host; a
request carrying any other `Host` header gets 421.

No tool modifies your books, graph or vectors. `lore_search` records its
Gemini requests in a ledger table in `data/lore.db`, and its first run migrates
`lore.db` to schema v4, which adds that ledger. Only `lore_search` spends
Gemini budget. `lore_search` and `vector_search` embed the question through
Ollama, so Ollama has to be running; if it is not, they return
`upstream_unavailable`. On this machine it must run CPU-only
(`CUDA_VISIBLE_DEVICES=-1 ~/.local/ollama/bin/ollama serve`).

`library`, `ingest_status` and `vector_search` do not need the graph, so they
keep answering before `lore graph load` has run and while a load holds the
graph's lock. During that time `library` lists every book with
`entity_count: null` and a `note` saying why. `entity`, `subgraph`,
`cypher_query` and `lore_search` need the graph and return `no_graph` without
it. A book that was chunked but never loaded has no embedded passages, and
`vector_search` limited to that book says so instead of returning an empty
list.

A long-lived `--http` server should run under a supervisor, because an engine
crash takes the whole process down.

## How it works: from book to answer

LORE turns a book into a knowledge graph in three commands, then answers
questions from that graph with citations.

    lore ingest  ──►  lore process  ──►  lore disambiguate  ──►  lore process --skip-extract  ──►  lore ask
    parse+chunk       extract + build     settle ambiguous       apply the answers                 answer
                      the graph           surnames (Gemini)      (0 new requests)

### Where data lives

| Store | Kind | Holds |
|---|---|---|
| **SQLite** (`data/lore.db`) | relational | Book catalog, **full chunk text**, the `jobs` table (the authority on which extraction units finished), the GPU quota ledger |
| **Extraction artifacts** (Parquet under `data/`) | files | Raw model output per work unit: entities, relationships, evidence sentences, chunk vectors |
| **Kuzu** (`data/kuzu/graph`) | graph, Cypher | Nodes `Book`, `Chapter`, `Chunk`, `Entity`; edges `HAS_CHAPTER`, `HAS_CHUNK`, `MENTIONS`, `ALIAS_OF`, and `REL` (the relationships, each carrying its predicate, evidence sentence and chunk id) |
| **LanceDB** (`data/lancedb`) | vectors, 768-d | `chunks`: vector + text + `entity_ids`, the bridge from a passage to its graph entities; `entities`: entity vectors |
| `data/disambiguation/answers.json` | JSON cache | Gemini answers for ambiguous surnames |
| `data/adjudication/answers.json` | JSON cache | Gemini's yes/no answers to borderline merges, keyed by a hash of provider + prompt, so a reload spends zero requests |

### Models

| Model | Runs on | Used for |
|---|---|---|
| Gemini Flash Lite (3.1, then 3.5) | Google API, **opt-in in code, on in this project's `lore.toml`** | Extraction (`extraction_provider = "gemini"`), resolution yes/no calls (`[lore.adjudication]`), surname disambiguation, choosing a graph search (`[lore.selection]`) and answers (`[lore.synthesis]`) |
| nomic-embed-text v1.5 | Local Ollama; on Kaggle only during Kaggle extraction | Chunk vectors, entity similarity, predicate mapping, question embedding |
| Qwen2.5-7B-Instruct (AWQ) | Kaggle T4, vLLM | Extraction when `extraction_provider = "kaggle"` (the code default) |
| qwen2.5:3b | Local Ollama | Every step above when its setting is left on `"ollama"` (the code default), and `extraction_provider = "ollama"` |

### 1. `lore ingest`: parse and chunk (no models)

    EPUB / PDF ─► chapters in spine order ─► drop Gutenberg front matter
               ─► scenes (only ***, ---, ### are hard breaks)
               ─► sentences per paragraph (pysbd) ─► ~1,500-char chunks
               ─► SQLite: books row (status "chunked") + chunk rows

Chunk ids are positional: `{book_id}:{chapter_seq}:{seq}`.

### 2. `lore process`, part one: extraction

The book's chunks are packed into work units, and every unit produces the same
Parquet artifacts whichever provider ran it. A unit counts as finished only when
its `jobs` row says so, never merely because its files exist.

**`extraction_provider = "gemini"` (this project's `lore.toml`).** For each unit:

- **nomic-embed-text** on local Ollama embeds every chunk (document prefix)
  first, so a stopped Ollama fails the unit before any Gemini request is spent.
- **Gemini Flash Lite** reads the chunks **one per request**, in order, with the
  same extraction prompt the other providers use. One client serves the whole
  run, so its per-model throttle (15 requests/minute) holds across units.
- The manifest's `model` names the model(s) that actually answered
  (`gemini-3.1-flash-lite`, or a comma-joined list if the cascade fell through).

Measured on 22 chunks, same prompt, every fact judged against the text
(Phase 3 extraction eval): relationships supported **81%** (Gemini) vs **61%**
(Kaggle 7B), wrong entity kinds **1 vs 56**, key facts found **26/35 vs 22/35**.
Gemini did not repeat "Hope KILLS Lucy", "Holmes KILLS Athelney Jones" or the
Wiggins-as-a-Place kind split. Sending several chunks in one request lost about
two thirds of the relationships, which is why it is one chunk per request. Both
Holmes novels (407 chunks) are 407 requests, about 30 minutes, inside one Lite
model's 500 requests/day.

Two kinds of failure are handled differently:

- **Bad output** (empty, or JSON that fails the schema): the chunk is retried
  once, then dead-lettered, exactly as for the other providers.
- **No model can serve** (every model returned 429/503/404, the key was
  rejected, or the API is unreachable): nothing is dead-lettered. The unit is
  marked `failed` with the reason, the run stops, and `lore process` does not
  load the half-extracted book. When the daily budget resets,
  `lore extract <book_id> --resume` (or `lore process <book_id> --resume`)
  continues from the first unfinished unit. `work_unit_size = 10` keeps the
  requests a re-run unit spends again to at most 9.

Gemini receives each chunk's text. The key is read as for `lore ask` (below);
with no key, `extract` and `process` exit before dispatching anything.

**`extraction_provider = "kaggle"` (code default).** Each unit runs as its own
free Kaggle GPU session: **Qwen2.5-7B-Instruct** on vLLM reads each chunk, and
nomic-embed-text embeds them. GPU hours are recorded in the quota ledger.

**`extraction_provider = "ollama"`.** The same step fully locally on qwen2.5:3b
and nomic-embed-text: slower and weaker, but nothing leaves the machine.

A book must be extracted with **one** packing. Its completed `jobs` rows are what
the load reads, so completed units from an older run at a different
`work_unit_size` would cover the same chunks twice; the load refuses that
rather than mixing two extractions into one graph.

### 3. `lore process`, part two: building the graph

    artifacts ─► filter non-entities (pronouns, descriptions, licence text)
              ─► entity resolution ─► relationship mapping
              ─► write Kuzu + LanceDB

**Entity resolution** decides which mentions are the same thing, in three passes:

| Pass | How | Model |
|---|---|---|
| 1. Exact name, alias, or name variant within a book (`Holmes` ⊂ `Sherlock Holmes`) | rules | none |
| 2. Embedding similarity | cosine distance | nomic-embed-text |
| 3. Borderline cases | yes/no question at temperature 0 | Gemini Flash Lite (this `lore.toml`, `[lore.adjudication] provider = "gemini"`) or qwen2.5:3b (code default) |

Pass 3 sends the same prompt to either model and reads the verdict the same way
("yes…" merges, anything else does not). The local 3B's record on this graph
was answers that ran against the truth, which is why this project moves it to
Gemini. Gemini's answers are cached in `data/adjudication/answers.json` as each
arrives, keyed by a hash of the provider and the full prompt, so reloading a
book re-asks nothing it has already been answered. If the quota runs out
mid-load, the load fails with that reason before anything is written; it never
treats an unanswered question as "no", because that would quietly under-merge
the graph. The answers already received stay cached, so re-running
`lore process <book_id> --skip-extract` after the reset only pays for the rest.

A bare name that fits **two or more** people (`Sholto` → Major, Thaddeus or
Bartholomew) is refused rather than guessed: the entities stay separate and
the question is recorded. Cross-book merges follow stricter rules.

**Relationship mapping** maps each extracted verb ("distrusts") to one of ten
curated predicates (`KILLS`, `OPPOSES`, `LOVES`, …) by embedding similarity.
The original wording is kept alongside it.

**Writes:** the whole graph to Kuzu; chunk vectors with their `entity_ids`, and
entity vectors, to LanceDB.

**`lore disambiguate`** (enabled in this `lore.toml`): sends every recorded
surname question to Gemini in one batched request and caches the answers; the
next `lore process --skip-extract` applies them. See the Gemini section below.

### 4. `lore ask`: answering a question

    question (a blank one is refused before anything opens)
      │ 1. EMBED       nomic-embed-text (query prefix), once per question
      ▼
    LanceDB: the 8 most similar chunks ─► their entity_ids
      │                (nearest chunk under 0.62 cosine ─► mode empty)
      │ 2. SEED        Kuzu: follow ALIAS_OF to canonical entities, drop any
      │                with no relationships, keep the best 8
      ▼
      │ 3. SELECT      Gemini Flash Lite (this lore.toml) or qwen2.5:3b (code
      │                default), temperature 0, picks one of seven fixed,
      │                read-only Cypher searches and numbered entities
      ▼
      │ 4. TRAVERSE    Kuzu runs that search ─► up to 250 real directed edges
      ▼
      │ 5. ASSEMBLE    SQLite chunk text for those edges; duplicates removed;
      │                passages ranked by similarity to the question vector,
      │                books alternated; capped at 12,000 chars; numbered
      │                in reading order
      ▼
      │ 6. SYNTHESIZE  Gemini Flash Lite (this lore.toml) or qwen2.5:3b (code
      │                default)
      │                ─► claims, each with a passage number and a
      │                   word-for-word quote
      ▼
      │ 7. VERIFY      code checks each quote is really in its passage;
      │                failing claims are dropped; none left ─► refusal
      ▼
      │ 8. CHECK       one batched call judges each surviving claim against
      │                the passage it cites (its book, chapter, quote and
      │                text); `not_supported` ─► dropped; none left ─►
      │                refusal. A failed or misaligned check keeps every
      │                claim and says `the faithfulness check did not run`
      ▼
    answer + sources (book, chapter) + mode + the models that selected
    and answered

`ask` opens SQLite read-only and Kuzu read-only, and treats a book as
queryable when the graph holds a `Book` node for it. Its one write is
bookkeeping: when a step runs on Gemini, each request is recorded in the
`gemini_ledger` table of `lore.db`, which may first migrate it to schema v4.

The model never writes Cypher: it chooses among seven hand-written templates
(`neighbours`, `two_hop`, `path_between`, `by_predicate`, `shared_neighbours`,
`across_books`, `chapter_window`), and every value binds as a parameter.
Each search returns up to 250 facts (`across_books`: up to 250 from **each**
book), more than any single entity in the Holmes graph has, so the search
decides what is *connected* and does not cut it by chapter. The question then
decides what *matters*: every candidate passage is scored by cosine similarity
between the question's vector and the chunk's stored vector, and passages are
kept most relevant first, one book at a time in turn, until 12,000 characters.
Cutting by chapter instead kept the earliest chapters and lost the passages
that answered the question.

Every answer reports one of four modes:

- **graph**: a search returned edges, and the answer is built from them.
- **vector-fallback**: the search returned nothing, or the model's choice was
  invalid. The answer comes from plain vector search and **says so**.
- **empty**: nothing in the library was close enough to start from.
- **vector-baseline**: `--baseline`, vector search only, for comparison.

## The Gemini disambiguator (`lore disambiguate`)

Off in the code default, **enabled in this project's `lore.toml`**, and not
part of `lore process`. Entity resolution decides
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

    export GEMINI_API_KEY=...            # or GOOGLE_API_KEY, or ~/.config/lore/gemini.env
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
the environment or `~/.config/lore/gemini.env` (mode 600) — never from
`lore.toml`, which would put it in git; an `api_key` line there is a hard
config error rather than a silent no-op.

### Quota, and why the design looks like this

Free-tier limits, measured from the console:

| model | RPM | requests/day |
|---|---|---|
| `gemini-3.1-flash-lite`, `gemini-3.5-flash-lite` | 15 | **500** |
| `gemini-3.6/3.7/3.8-flash` | 5 | **20** |

Twenty a day on the Flash tier is what shaped this feature, and it still
governs the fallbacks:

* **The disambiguation API is not in the load path.** A load only records the
  surname questions it refused; the quota for them is spent by a separate,
  deliberate command. (Pass-3 adjudication, when `[lore.adjudication]` is on
  Gemini, is a different, cached call that does run during the load.)
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
**Google may train on free-tier data.** That is the tradeoff for the free key.
With this project's `lore.toml`, extraction also sends **every chunk of every
book**, adjudication sends names and an evidence sentence per borderline merge,
and `lore ask` sends questions and passages. Every one of those settings
defaults to local in code, and `--dry-run` shows precisely what
`lore disambiguate` would send before anything is.

Verified: the disambiguation quality was checked against real corpus cases —
four live cases originally, then a 12-case eval across the model cascade.
**Not yet verified: the end-to-end pipeline against live quota.** No full
`lore process` → `lore disambiguate` → `lore process` cycle has been run
against the real API; every test in this repo stubs the transport. Gemini
extraction and adjudication were each smoke-tested live on a handful of
requests (Task R1), not on a whole book.
