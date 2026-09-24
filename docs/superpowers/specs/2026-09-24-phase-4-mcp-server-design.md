# Phase 4 — MCP Server

**Status:** approved in chat 2026-09-24. Implements SPEC.md §9 Phase 4 (§5.7 tools).

**Goal:** any MCP client — Claude Code today, the Phase 5 web UI next — can ask
LORE a question and explore its graph over the Model Context Protocol, getting
back JSON structured enough to draw, not prose to re-parse.

**Done when:** an external MCP client answers a real lore question through the
running stack, against the live graph.

---

## 1. What exists already

Phase 3 closed with `lore ask` answering multi-hop questions at 13 correct / 0
refused against a baseline's 7 correct / 5 refused, over 26 questions. The parts
this phase builds on:

**`lore/query/agent.py`** — `answer_question(conn, kuzu, lance, question, models)
-> Answer` is the whole agent behind one call. The CLI's `ask` command is a thin
shell: open stores, call it, print. `lore_search` is the same call plus
serialization; there is no second implementation of the agent in this phase.

**`lore/db/kuzu_store.py`** — `KuzuStore(path, read_only=True)` already exists and
`lore ask` already uses it (`cli.py:699`).

**`lore/orchestrator/quota.py`** + the `quota_ledger` table — the pattern the
Gemini budget ledger follows: a SQLite table, a module of small functions over
it, surfaced by `lore quota`.

**`lore/query/seed.py`** — `NameIndex`, `named_entity_ids`, `canonical_ids`,
`live_entity_ids`. The `entity(name)` tool resolves names the same way the query
agent does, so an alias finds its canonical by the same path.

---

## 2. Decisions taken, and why

Five questions settled in chat, plus one decided on measurement.

**2.1 Consumer: Claude Code *and* the Phase 5 UI backend.** Structured JSON is
a day-one requirement, not a later refactor. This is what forces §4.

**2.2 Transport: stdio + Streamable HTTP.** SPEC §5.7 said "stdio + SSE". SSE is
the older HTTP transport the MCP spec superseded; the Python SDK still speaks it
but calls Streamable HTTP "the transport you deploy". Streamable HTTP still
streams — it upgrades to SSE per response when the server pushes — so the UI
gets progress events without building on the deprecated path.

**SPEC.md §5.7 and §9 Phase 4 are amended to say "stdio + Streamable HTTP".**

**2.3 Budget guard: one ledger, every caller counts.** A `gemini_ledger` table
that CLI and MCP both write to. An MCP-only ledger reports the wrong number on
any day the CLI is also used, which is most days.

**2.4 Raw `cypher_query`: yes, with hard guards.** See §5 — the guards turned out
to be load-bearing in a way that was not obvious when the question was asked.

**2.5 Store handles are per-request.** Measured, not assumed:

| measurement | result |
|---|---|
| Kuzu open (read-only) + one query | 21 ms median, 93 ms max over 10 runs |
| LanceDB connect + open table | 5 ms median |
| Writer opening the graph while a reader holds it | **fails**: `IO exception: Could not set lock on file` |
| Second reader while a reader holds it | succeeds |

A server that holds the graph open makes `lore graph load` fail outright. At
21 ms against a `lore_search` that spends seconds in Gemini, opening per request
is free. So `stores.py` opens, yields, and closes — and rebuilding the graph
while the server runs keeps working.

---

## 3. Module layout

```
lore/mcp/
  __init__.py
  server.py     MCPServer app; builds the seven tools, picks the transport
  stores.py     per-request handles (open -> yield -> close)
  shapes.py     the JSON each tool returns, as pydantic models
  guards.py     cypher validation: allowlist, banned tokens, row cap, timeout
lore/orchestrator/gemini_budget.py    the shared ledger, sibling to quota.py
```

New dependency: **`mcp>=2.2`** (2.2.0 is current on PyPI).

The API below was read from the SDK's own source and examples, not recalled.
Note the class is `MCPServer`, not `FastMCP` — `FastMCP` is the 1.x name and
would not import:

```python
from mcp.server import MCPServer

mcp = MCPServer("lore")

@mcp.tool()
def lore_search(query: str) -> SearchResult: ...

mcp.run(transport="stdio")
mcp.run(transport="streamable-http", host="127.0.0.1", port=8765,
        transport_security=TransportSecuritySettings(allowed_hosts=[...]))
```

Entry points on the existing Typer app:

```
lore mcp                        # stdio, for Claude Code
lore mcp --http --port 8765     # streamable-http, for the Phase 5 UI
```

---

## 4. The seven tools

| tool | returns | Gemini |
|---|---|---|
| `lore_search(query)` | answer text, claims, passages, edges, seeds, mode, models used | yes — budget-guarded |
| `vector_search(query, book_id?, limit?)` | chunks: text, book, chapter, distance | no (one local embedding) |
| `cypher_query(query)` | rows + column names, or a guard error | no |
| `entity(name)` | canonical card: id, kind, aliases, books, in/out edges in chapter order | no |
| `subgraph(entity, depth=1)` | `{nodes: [...], edges: [...]}`, drawable as-is | no |
| `library()` | books: id, title, author, status, chunk and entity counts | no |
| `ingest_status(book_id)` | job states, last run, dead letters | no |

### 4.1 The `Answer` widening

`Answer` (`lore/query/models.py`) carries `text`, `passages`, `mode`, `template`,
`note`, `model`, `selection_model`, `sub_questions`. Two things the UI needs are
built during synthesis and discarded before it returns:

- **Claims.** `Claim{text, passage, quote}` is quote-checked against its passage
  and judged by the faithfulness pass — then `_compose` (`synthesize.py:283`)
  joins the claims into one prose string and the structure is gone. The
  per-claim citation the UI makes clickable dies a layer before the return.
- **Evidence edges.** `Evidence.edges` never reaches `Answer`, so there is no
  subgraph for the UI to draw.

`Answer` therefore gains three fields, all defaulted so every existing
construction still compiles:

```python
claims: tuple[Claim, ...] = ()
edges:  tuple[EdgeRow, ...] = ()
seeds:  tuple[SeedEntity, ...] = ()
```

`Claim` currently lives in `synthesize.py` while `Answer` lives in `models.py`,
whose docstring promises no cross-module imports. So `Claim` moves to
`models.py` and `synthesize.py` imports it from there.

`_compose` keeps returning prose and the CLI keeps printing it unchanged. It
simply stops being the only thing that survives.

**What `claims` does and does not carry.** Only the survivors. `validated_verdicts`
returns one `(supported, reason)` per claim and the unsupported ones are dropped
before composition, so every claim that reaches `Answer` has already passed both
the quote check and the faithfulness pass — the verdict is implicit and is not a
field. When the check could not run at all, that is already reported through the
existing `note` (`CHECK_SKIPPED_NOTE`), so a client can tell "all claims passed"
from "nothing was checked". Dropped claims are not exposed; a claim the judge
rejected is not evidence of anything a UI should draw.

---

## 5. Cypher guards

### 5.1 What read-only does not protect

Measured on a connection opened `read_only=True`:

| statement | result |
|---|---|
| `CREATE (n:Entity {...})` | blocked — "Cannot execute write operations in a read-only database!" |
| `MATCH (e:Entity) ... SET e.name = 'x'` | blocked, same message |
| `MATCH (e:Entity) DETACH DELETE e` | blocked, same message |
| `COPY (MATCH (e:Entity) RETURN e.id, e.name) TO '/tmp/leak.csv'` | **allowed** — file written, real entity rows read back |
| `LOAD FROM '/etc/hostname' (file_format='csv') RETURN *` | **allowed** — returned the file's contents as rows (the machine's hostname) |

Kuzu's read-only mode protects the database, not the filesystem. `cypher_query`
behind read-only alone is an arbitrary file read and write primitive, reachable
over the HTTP transport. The token guard below is the actual control; the
read-only connection is the backstop.

### 5.2 The rules, failing closed

1. **Allowlisted opening.** The statement must begin with `MATCH`,
   `OPTIONAL MATCH`, `RETURN`, `WITH`, `UNWIND`, or an allowlisted `CALL`
   (`show_tables`, `table_info`, `db_version`). Anything else is rejected unread.
2. **Banned tokens anywhere:** `COPY`, `LOAD`, `EXPORT`, `IMPORT`, `ATTACH`,
   `INSTALL`, `CREATE`, `MERGE`, `SET`, `DELETE`, `DROP`, `ALTER`. Matched as
   whole words **including inside string literals** — a question about a
   character named "Copy" gets a clear rejection rather than a parser this guard
   has to trust. False positives are the right side to err on.
3. **One statement.** A `;` joining two statements is rejected; otherwise rule 1
   vets only the first.
4. **Row cap**, default 200, returning `truncated: true` when it bites. This is
   the real resource control: Kuzu streams, so
   `MATCH (a:Entity),(b:Entity),(c:Entity) RETURN a.id,b.id,c.id` — 175,616,000
   rows — returns its first rows in 60 ms, and reading 200 then stopping costs
   nothing.
5. **`set_query_timeout(5000)`.** Insurance, and unproven on this corpus: 560
   entities is too small to build a slow query, and the optimizer folds
   `count(*)` over a triple product into arithmetic (answered in 30 ms). It is
   there for a larger library, not because it was demonstrated here.
6. **Read-only connection**, as the engine-level backstop for rule 2.

---

## 6. Gemini budget ledger

```sql
CREATE TABLE gemini_ledger (
    id      INTEGER PRIMARY KEY,
    day     TEXT NOT NULL,        -- 2026-09-24, local date
    model   TEXT NOT NULL,        -- gemini-3.5-flash-lite
    count   INTEGER NOT NULL,
    UNIQUE (day, model)
)
```

**The counter lives in the inference layer, not the server.** `lore/inference/
gemini.py` is the one place every caller passes through — extraction,
disambiguation, adjudication, `lore ask`, `lore_search`. It gains a recorder that
increments `(day, model)` on each call and reads the day's count before one. Put
the counter in the MCP server instead and the number is wrong the moment an
extraction run happens.

`lore quota` grows a Gemini section beside the GPU-hours one.

At the cap, `lore_search` returns `budget_exhausted` naming the reset time. The
other six tools are unaffected — `vector_search`'s one embedding call is local —
so the graph stays fully explorable after the daily cap is spent. That was the
point of the shared-ledger decision: a guard that tells the truth, and a failure
that is narrow.

---

## 7. Errors and refusals

No tool raises through the protocol. Each returns `{error: {code, message}}`, so
a client branches on the code and a human reads the message.

| code | raised when |
|---|---|
| `no_graph` | stores missing — the condition `lore ask` already checks at `cli.py:692` |
| `budget_exhausted` | Gemini daily cap; message names the reset time. `lore_search` only |
| `cypher_rejected` | which guard rule tripped, and the token that tripped it |
| `not_found` | unknown entity name or book id |
| `timeout` | the query timeout fired |

---

## 8. Testing

Following the existing layout, building on `tests/fixtures.py`:

- **`tests/test_mcp_tools.py`** — each tool called directly against fixture
  stores; shape assertions on the JSON.
- **`tests/test_mcp_guards.py`** — the allowlist, the banned tokens, the
  multi-statement case, the row cap. `COPY ... TO` and `LOAD FROM` go in as
  **named regression tests with the exact payloads from §5.1**, because those
  are the two proven to work against read-only.
- **`tests/test_mcp_budget.py`** — the ledger counts, the cap refuses
  `lore_search`, and the six cheap tools still answer after the cap.
- **Transport smoke test** — through the SDK's in-memory client, not a
  subprocess.
- **Concurrency test** — a writer can open the graph while tool calls are in
  flight. This is the regression guard for §2.5.

Acceptance: registered in `.mcp.json`, asked a real multi-hop question through
Claude Code, against the live graph.

---

## 9. Out of scope

- The web UI (Phase 5). This phase produces the JSON it will draw, nothing more.
- Authentication on the HTTP transport. It binds `127.0.0.1` and sets
  `TransportSecuritySettings` (the SDK's DNS-rebinding protection: an
  `allowed_hosts` / `allowed_origins` allowlist, which matters because a page
  in your browser can otherwise reach a localhost port). The Phase 5 spec
  revisits auth if anything is ever exposed beyond the loopback interface.
- Writing through MCP. Every tool is read-only. Ingestion and extraction stay on
  the CLI.
- The carried graph debt from Phase 3 (4 false `KILLS` edges, the
  "Miss Mary Morstan" split, Drebber's node name, 1 schema-failing chunk). These
  are data defects, not server defects; Phase 7 owns them.
