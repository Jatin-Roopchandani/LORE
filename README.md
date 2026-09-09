# LORE — Local Orchestration & Relational Extraction

A local-first literary knowledge graph: e-books in, entities/relationships out
(extracted on free Kaggle GPUs or fully locally), served from embedded
databases (Kuzu + LanceDB) with a hybrid graph-vector query agent, an MCP
server, and a graph UI.

See `SPEC.md` for the full design. Phase 0 status: foundation scaffold.

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

## Optional: the Gemini disambiguator (`lore disambiguate`)

Off by default, and not part of `lore process`. Entity resolution decides
almost everything with deterministic rules; **one** class genuinely needs
judgement and no rule can settle it: a bare surname that is a subset of two
or more candidates. `Major Sholto` normalizes to `sholto`, which nests inside
both `thaddeus sholto` and `bartholomew sholto` — a murdered father and his
two sons — so LORE refuses that merge and leaves the entities separate. Safe,
but lossy: 17 declined merges on the two-novel corpus (`sholto`, `ferrier`,
`smith`, `drebber`, `morstan`, `young`).

`lore disambiguate` asks Gemini about exactly that residue, and nothing else.

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
