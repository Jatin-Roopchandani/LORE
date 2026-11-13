# LORE user guide

Running LORE yourself: the command line, the local web app, worlds, the kinds of answer, and connecting an MCP client. For a first install, start with the [Quick start in the README](../README.md#run-it-yourself).

## Command-line quick start

    uv sync
    uv run lore doctor            # checks config, stores and ollama
    uv run lore ingest book.epub --world "My Series"   # parse + chunk + register a book in a world
    uv run lore books             # list the ingested library
    uv run lore extract <book_id> # extract entities + relationships (Gemini in this lore.toml)
    uv run lore extract <book_id> --resume   # continue after an interrupted run
    uv run lore process <book_id> # extract, resolve entities, load the graph
    uv run lore process <book_id> --skip-extract   # reload the graph only
    uv run lore process <book_id> --resume   # continue after the daily Gemini limit (--force: before the reset)
    uv run lore world list        # worlds and their books (also: rename, delete, reconcile)
    uv run lore book move <book_id> --world "Other"   # move a book to another world (no Gemini requests)
    uv run lore book series <book_id> --series "Name" --index 2   # set a book's place in its series
    uv run lore graph             # node/edge counts and the busiest entities
    uv run lore ask "how are Holmes and Watson connected?"   # cited answer from the graph
    uv run lore ask "..." --baseline   # vector search alone, for comparison
    uv run lore ask "..." --depth deep --world "My Series"   # a long, sectioned answer within one world
    uv run lore quota             # today's Gemini requests per model

`extraction_provider` picks who reads the chunks:

| Value | Where | Notes |
|---|---|---|
| `"gemini"` (**this project's `lore.toml`**) | The remote model `[lore.llm]` picks (Gemini, Claude or OpenAI), one request per chunk | Best measured quality. Needs a key (see below). Embeddings stay local on Ollama |
| `"ollama"` (code default) | This machine, qwen2.5:3b | Zero egress; slower and weaker |

Extraction runs in work units of `work_unit_size` chunks (default 10). On
Claude, OpenAI or a paid Gemini plan up to `[lore.processing] llm_concurrency`
(default 4) chunk requests are in flight at once, and the next unit is embedded
while this one is extracted; free-tier Gemini asks one chunk at a time.

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
graph lighting up as it searches. Click a citation to read the passage. The
graph pane switches between **This answer** and the **Library** overview; click
a node for its entity panel (codex profile, story timeline, relationships with
their evidence, and one-click follow-up questions), click an edge for what
connects two entities, search for any entity, or **Connect to…** another to see
the path between them. **History** opens as a drawer, and **Ctrl+K** jumps to
any past question. The **Books** panel shows each book's processing and a
**Continue** button once the daily Gemini limit resets. Loopback only, with no
authentication. Asking spends Gemini requests, like `lore ask`.

For frontend development, run `lore ui --dev` with `cd web && npm run dev`
(http://localhost:5173). Tests: `npm test` (free). The end-to-end test
spends Gemini requests, so it's opt-in: `LORE_E2E=1 npx playwright test`.
`lore ui --dev` lets any page served from localhost:5173 call the API, so use
it only while developing.

Packaging a wheel needs `cd web && npm run build` first: the built app in
`lore/web/static` is what ships.

## Running fully local

LORE can run with no cloud key at all: every model step on a model you serve
yourself, from any server that speaks OpenAI's chat completions API —
llama.cpp's `llama-server`, vLLM, LM Studio, or Ollama's `/v1` endpoint.
Embeddings stay on Ollama as always (`ollama pull nomic-embed-text`), so keep
Ollama running too.

With `llama-server` (e.g. `llama-server -m qwen-28b-q4_k_m.gguf --port 8080
--jinja -c 32768`), put this in `lore.toml`:

```toml
[lore]
# "gemini" in these settings means "the [lore.llm] model", whichever it is.
extraction_provider = "gemini"

[lore.llm]
provider = "openai_compatible"
base_url = "http://localhost:8080/v1"
model = "qwen-28b"          # the name the server knows it by
# fast_model = "qwen-7b"    # optional: a smaller model for the short steps
concurrency = 1             # requests in flight at once; 1 for one GPU
# max_output_tokens = 8192  # each reply's cap (0: none; vLLM needs it to fit the context);
#                           # 4096 suits small models, which sometimes repeat themselves to the cap

[lore.selection]
provider = "gemini"

[lore.synthesis]
provider = "gemini"

[lore.adjudication]
provider = "gemini"
```

Then extraction, entity disambiguation and synonyms, story beats, the codex,
and every answer step (decompose, select, synthesize, check, write) run on
that model. Other servers differ only in the address: vLLM
`http://localhost:8000/v1`, LM Studio `http://localhost:1234/v1`, Ollama
`http://localhost:11434/v1` (Ollama's `/v1` uses a 4,096-token context unless
the server is started with `OLLAMA_CONTEXT_LENGTH=16384` or more; LORE's
prompts need it).

- **Key:** optional. If your server wants one, set `LORE_LLM_API_KEY`, or put
  one line `LORE_LLM_API_KEY=...` in `~/.config/lore/llm.env` (mode 600). Never
  in `lore.toml`.
- **Concurrency:** `[lore.llm] concurrency` (default 1) caps the requests LORE
  sends at once, for processing and questions alike. Raise it only if your
  server runs several slots (`llama-server -np 4`).
- **Structured output:** LORE asks for JSON through `response_format`
  (`json_schema`), validates every reply, and asks once more with the error if
  a reply doesn't fit. A server that refuses `json_schema` gets `json_object`
  with the schema in the prompt instead.
- **Usage:** tokens are counted when the server reports them; no price is
  ever shown.
- **From the web app:** in local mode, **Settings → AI providers → Local
  model** takes the server address, the model name and an optional key, with
  **Test connection**. A model chosen there overrides `[lore.llm]` in
  `lore.toml` and runs every step, exactly like the example above (it is
  saved in `<data dir>/llm.json`, the key in `~/.config/lore/llm.env`);
  **Stop using it** goes back to `lore.toml` as written. The hosted
  multi-user server never offers this.
- **Quality:** extraction quality depends heavily on model size. Measured on
  22 chunks, a local 7B's relationships were 61% supported by the text against
  81% for Gemini 3.1 Flash Lite, with 56 wrong entity kinds against 1; a 3B is
  weaker still. Use the largest model your hardware serves at a bearable speed,
  and expect answers to follow the graph's quality. Larger local models have
  not been measured yet.

With no model configured at all (no key, no local server), books still
process: the story beats, codex and disambiguation steps are skipped with a
note ("skipped: no model configured for story beats") and the rest of the
graph is built.

## Worlds

Every book belongs to a **world**, chosen when it is added (`lore ingest --world`,
or the World picker in the UI's Add book dialog). Characters merge across books
only inside one world, so two unrelated series with a same-named character stay
apart, while series that share a universe can share a world. Questions, the
graph and MCP tools take an optional `world`; "all worlds" searches each world
separately and never merges entities across them. A book in the wrong world is
moved with `lore book move` (or "Move to world…" in the Books panel); its cached
extraction and answers are reused, so a move costs no Gemini requests.

## Answers: prose, interpretive and deep

Answers are written as cited prose over checked claims, in story order (books
ordered by `lore book series`, then chapters). Every sentence cites the claims it
rests on, and a separate check judges each sentence; anything unsupported is
dropped, and an answer that fails falls back to its claim list.

- **Interpretive questions** (analysis, judgement, theme, comparison) are split
  into *lenses* that gather evidence for each side. The answer opens with a
  **short answer**, then the evidence by lens, LORE's labelled reading, the
  strongest counter-case and what the books don't settle. The editorial policy
  is `lore/query/viewpoint.md`, replaceable via `[lore.answers] viewpoint`.
- **Deep answers** (broad questions, or the UI's *Deep dive* toggle, or
  `--depth deep`) are planned as sections around the question's aspects, each
  written and checked on its own. A full deep answer can use around 40 Gemini
  requests.
- **Story beats** and the **codex** (quote-verified moments and entity profiles)
  are built once per book as processing stages and feed both. Turn them off with
  `[lore.beats] enabled = false` / `[lore.codex] enabled = false`.

Most of these behaviours are module switches that default on
(`PROSE_ANSWERS`, `STORY_ORDER`, `INTERPRETIVE`, `DEEP`, `TIMELINE`, …); each has
an `evals/answer_quality/run_pair.py` arm for paired evaluation.

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

MCP is local-only. It serves one library and has no notion of which user is
asking, so `lore mcp` refuses to start while `LORE_MULTI_USER=1` is set, and
the web app's MCP panel (`/api/mcp`) is off in multi-user mode.

