# Phase 5a — Web UI: ask, read, explore

**Status:** approved in chat 2026-10-07, section by section. Implements the
first slice of SPEC.md §9 Phase 5 (§5.8 "Search box" and "Graph explorer").

**Goal:** ask LORE a question in the browser, watch it work, read a cited
answer, and see the graph it came from, light up stage by stage as the agent
actually searched it.

**Done when:** in a browser, against the live library, you ask a question,
watch the live steps and the graph highlighting, open a cited passage, click a
node for its entity card, expand its neighbours, and replay an earlier
question from history -- in light and dark themes, and at phone width.

**Mock:** `assets/2026-10-07-phase-5a-mock.html` (a static page replaying two
answers recorded from the real system; also published privately as an
artifact during the design). The layout, controls and wording it shows are
approved; the implementation follows it.

---

## 1. Scope: Phase 5 split in three

SPEC.md's Phase 5 is four independent pieces. They are built as three
sub-projects, each with its own spec, plan and implementation:

| | Sub-project | Status |
|---|---|---|
| **5a** | Search + answer panel + graph explorer (this spec) | read-only, over existing tools |
| 5b | Library manager: upload, live ingest progress | later |
| 5c | Pipeline and quota dashboard | later |

5a first because asking a question and seeing *why* the answer is what it is
is the core of LORE (SPEC.md §7 "Query", step 5).

**Out of scope for 5a**, decided in chat:
- Uploading a book. The **Add book** button and dialog exist (the mock's
  drop zone) but the dialog shows the `lore ingest` command to run; wiring
  it to ingestion is 5b.
- Real follow-up questions. History is chat-shaped, but each question stands
  alone (§5.4). A memory layer comes later.
- Edge-type and entity-type filters, and the chapter timeline slider. The
  timeline needs its own design: chapter numbers do not line up across books.
- The books dropdown limiting the *search*. In 5a it filters what the graph
  draws; restricting retrieval would change the query agent, which is still
  being evaluated on the `answer-quality` branch.
- Multi-tenancy, authentication and deployment. 5a is a local, single-user
  tool. Two hooks prepare for hiding internals later (§6).

---

## 2. Decisions taken in chat

1. **Start with ask-and-explore (5a)**, ahead of the library and dashboard.
2. **The answer view shows the "how" behind an expandable panel**: parts,
   graph search used, starting entities, passages read, models. Dropped
   claims are not shown (that needs new plumbing; only eval traces have it).
3. **Live steps in the chat, and stage-by-stage graph highlighting**, in
   sync. Every highlight is a real event: seeds light up, the template's
   edges fan out, edges whose passage missed the evidence cut fade, cited
   edges glow. Within one stage, edges arrive together; the UI staggers
   them over ~0.5 s for legibility. That stagger is presentation only; the
   order *between* stages is real. No fake node-by-node traversal.
4. **Chat-style history, each question independent**, stored in a shape a
   memory layer can later read.
5. **Graph interactions:** click a node for its entity card, expand its
   neighbours, filter by book. Types and timeline later.
6. **Architecture: a FastAPI backend calling LORE's code directly** (option
   1 of 3). Rejected: the browser speaking MCP to the existing server
   (browser-hostile, origin rules would loosen, no fit for 5b uploads), and
   FastAPI proxying to MCP (an extra hop with the same limits).
7. From the mock review: **light and dark themes** with a one-click toggle
   and a System/Light/Dark setting; **books as a searchable multi-select
   dropdown** (libraries will hold dozens); a **settings menu (gear)** with
   MCP connection details and appearance; **Add book** as a primary button
   in the top bar.

---

## 3. Architecture

```
browser (React app)  --HTTP/SSE-->  lore ui  (FastAPI, 127.0.0.1:8766)
                                       |  calls
                                       v
                                lore/mcp/tools.py  <--  lore mcp (8765, unchanged)
                                       |
                        Kuzu (read-only) · LanceDB · SQLite · Ollama · Gemini
```

- New command **`lore ui [--port 8766]`** runs one FastAPI process serving
  both `/api/*` and the built frontend. It refuses a non-loopback `--host`,
  exactly as `lore mcp --http` does (`lore/mcp/server.py:_check_loopback`).
- The MCP server is untouched. Both processes can run at once; both open
  Kuzu read-only.
- **New package `lore/web/`** (`app.py`, `events.py`, `static/`), thin by
  rule: every endpoint calls the same function in `lore/mcp/tools.py` that
  the matching MCP tool calls, so the UI, Claude Code and the evals see the
  same data and the same structured errors.
- **Dependencies:** add `fastapi` and `uvicorn` to `pyproject.toml`
  (`uvicorn` already arrives through `mcp`; declared explicitly).
- **Frontend source in `web/`** at the repo root (React + TypeScript + Vite,
  as SPEC.md §4 names). `npm run build` writes to `lore/web/static/`, which
  ships in the package, so running the UI needs no Node, only building it.

### 3.1 Endpoints

| Endpoint | Calls | Used by |
|---|---|---|
| `GET /api/library` | `tools.library` | books dropdown: title, author, status |
| `GET /api/entity?name=` | `tools.entity` | entity card |
| `GET /api/subgraph?entity=&depth=1` | `tools.subgraph` | Expand neighbours |
| `POST /api/ask` (SSE response) | `tools.lore_search` with `on_event` | live steps, graph stages, answer |
| `GET /api/quota` | `gemini_budget.day_report` | quota chip |
| `GET /api/mcp` | config + a TCP probe of the MCP port | settings menu: commands, address, status |
| `GET /api/settings` | config | `show_internals` (§6) |

Errors pass through as the tools return them (`{"error": {"code", ...}}`);
HTTP status 200 for a tool-level error, as MCP does, 4xx only for a bad
request to the API itself.

### 3.2 Security

Loopback bind only. `Host` and `Origin` are checked on every request (the
same allowlist idea as the MCP server's `TransportSecuritySettings`): a
page on another origin in the same browser must not be able to drive the
API or spend Gemini requests. `POST /api/ask` additionally requires
`Content-Type: application/json`, so a cross-site form cannot reach it.

---

## 4. The live event stream

### 4.1 The hook

`answer_question(..., on_event: Callable[[dict], None] | None = None)`.
`lore_search` gains the same optional argument and passes it through.
Called synchronously at each real stage; the web layer's callback only puts
the event on an `asyncio.Queue`. **With `on_event=None` the pipeline is
unchanged** -- the CLI, MCP and `evals/answer_quality` runs get identical
behaviour and identical answers, which a test pins.

A callback that raises is caught and logged; it never fails an answer.

### 4.2 Events

Sent as SSE `event:`/`data:` pairs, in this order:

| Event | When | Data |
|---|---|---|
| `step` | each stage starts and ends | `{name: split\|seeds\|search\|rank\|write\|check, state: start\|done, detail}` |
| `parts` | after decomposition | `{sub_questions: [...]}` |
| `seeds` | per part | `{part, entities: [{id, name, kind, books}]}` |
| `edges` | per part, after its template ran | `{part, template, total, edges: [...]}` (first 150) |
| `evidence` | after ranking | `{chunk_ids: [...], edge_keys: [...]}` -- what made the cut |
| `answer` | at the end | the full `lore_search` result, identical to MCP's |
| `error` | on failure | `{code, message, ...}` as the tools return it |

An edge key is `subject_id|predicate|object_id|chunk_id`: one extracted
relationship, which the same chunk can state once.

Per-part `seeds` and `edges` give each sub-question its own colour (the
mock could not: today's `Answer.seeds` is merged). `edges` before
`evidence` lets the graph show edges fading when their passage misses the
budget; `Answer.edges` only ever held the survivors.

### 4.3 Node identity

Template rows return entity names only, so `EdgeRow` and the edges in
`lore_search` carry empty `subject_id`/`object_id` (`lore/mcp/shapes.py:72`,
a Phase 4 deferral). The templates in `lore/query/templates.py` also
return `a.id, b.id`; `EdgeRow` gains `subject_id`/`object_id`; the ids flow
to the events and the answer. Names are not unique, and the entity card
must open the entity that was actually traversed.

### 4.4 Behaviour

- **One question at a time** per server. A second `POST /api/ask` while
  one runs gets `{"error": {"code": "busy"}}`. Gemini's 15 RPM ceiling makes
  parallel asks pointless.
- **Cancellation:** when the client disconnects (tab closed, new question),
  the agent task is cancelled, so it stops spending Gemini requests.
- Event delivery is best-effort; the `answer` event is what matters, and a
  client that missed earlier events can still render it.

---

## 5. Frontend

### 5.1 Components

- **TopBar:** BooksDropdown (filter box, Select all / Clear, per-book
  checkbox with author and status; unprocessed books listed, disabled),
  AddBook (dialog; shows the `lore ingest` command in 5a), QuotaChip,
  ThemeToggle, SettingsMenu (MCP: Claude Code and HTTP tabs with copyable
  commands and a server status light; Appearance: System/Light/Dark).
- **ChatPane:** HistoryBar, Thread (per turn: Question, StepList with
  per-step timing, Claims with citation buttons that expand the passage
  with the quote highlighted, "How this was answered" panel), AskBox.
- **GraphPane:** GraphCanvas, StageBar and Legend, EntityCard (kind,
  books, relationships in this answer, chapters, Expand neighbours).

### 5.2 Graph rendering

**Sigma.js with graphology** (SPEC.md §4): WebGL, so it stays smooth at
thousands of nodes. Visual state (visible, faded, cited, per-part colour,
hovered claim's edges) is applied through Sigma's node and edge reducers
from derived state, never set imperatively. Expanding a node places the
new neighbours around it without moving nodes already placed. Edges are
drawn one per subject-object pair, labelled with the strongest predicate
and a count, as the `subgraph` tool already groups them.

### 5.3 State

- **One pure reducer, `(state, event) -> state`**, turns the event stream
  into the current answer's state: steps, parts, seeds, edges, evidence,
  answer, error. Chat and graph both read it, so they cannot disagree.
- **Graph view state is a pure function of that state** plus UI choices
  (books shown, hovered claim, expanded nodes).
- The stagger is a scheduler over when derived state is *shown*, honouring
  `prefers-reduced-motion` (no stagger).

### 5.4 History

Stored in browser storage as `{id, question, events, answer, askedAt}` per
question. Replaying an old question re-runs its recorded events through the
reducer (animation included) with no new Gemini request. The record is the
shape a server-side conversation store will take when the memory layer
arrives; then `events` and `answer` also become the context a follow-up
question reads. Storage access is wrapped so a blocked or full store only
loses history, never breaks the page.

### 5.5 Talking to the backend

Small `fetch` wrappers per endpoint, and a streaming SSE reader over
`fetch` for `POST /api/ask` (the browser's `EventSource` cannot POST). In
development Vite proxies `/api` to `lore ui`.

### 5.6 Look

The mock's tokens: a cool paper ground with a Prussian-blue accent and a
gaslight-amber "cited" highlight; Newsreader for book text and claims,
Public Sans for the interface, JetBrains Mono for ids and timings; light
and dark palettes defined as CSS variables with the System/Light/Dark
switch setting `data-theme`. Fonts are bundled with the build, not loaded
from a CDN. Phone width: the graph stacks above the chat.

---

## 6. Errors, and hiding internals later

| Situation | Shown |
|---|---|
| `budget_exhausted` | "Today's Gemini requests are used up. They reset at midnight Pacific (in about 3h 20m)." Quota chip amber under 10%, red at 0. |
| `upstream_unavailable` | "Gemini is busy right now. Try again in a minute." + Retry |
| Ollama unreachable | "LORE can't reach Ollama. Start it, then ask again." + the CPU-only start command, copyable |
| `empty` mode / refusal | Said plainly; the graph shows what was searched |
| Partial answer | Claims found, plus the parts not found (the answer's note) |
| `busy` | Ask is disabled while a question runs; a stray request gets "still answering" |
| Stream dropped | Steps so far stay; the rest marked interrupted + Retry |
| No books selected | Graph empty state "Pick at least one book"; search still covers all books |
| `ambiguous` on expand | The entity card lists the candidates to pick |
| Very large answers | Draw cap 150 edges per part, "showing 150 of 412" |
| MCP HTTP server down | Settings status grey, "not running", with the start command |

**Hooks for multi-tenant deployment later** (the user's direction: a
deployed build hides Gemini and other internals):
- **All user-facing wording comes from one message table** keyed by error
  code and state; provider names appear only there.
- **A `show_internals` setting** (`[lore.web]` in `lore.toml`, default
  `true` locally), served by `GET /api/settings`, gates everything
  provider-specific: model names in the "how" panel, the Gemini quota
  chip, the Ollama command, the MCP panel. A deployed build sets it false.

---

## 7. Testing

**Backend (pytest):**
- The hook: with scripted models (the `tests/test_query_agent.py`
  pattern), events arrive in order with correct per-part seeds and edges;
  with `on_event=None` the answer equals a run without the hook; a raising
  callback does not fail the answer.
- Node ids: template rows carry `subject_id`/`object_id` matching the
  graph.
- API: FastAPI's test client against every endpoint; `/api/ask`'s stream
  parsed into events and compared; each error code passes through; `busy`
  is refused; a client disconnect cancels the run (no model call after it).
- Security: a foreign `Origin` or `Host` is refused; a non-JSON POST to
  `/api/ask` is refused; a non-loopback `--host` is refused.

**Frontend (Vitest):**
- The reducer over recorded event streams, including interrupted, error,
  empty and partial runs.
- Derived graph state: cited, faded, per-part colour, edge caps, expand
  without moving placed nodes.
- Targeted component tests: books dropdown filter, quote highlighting,
  history replay.

**Fixtures** are event streams recorded from real runs, so frontend tests
are free and deterministic.

**End to end** (opt-in, spends Gemini requests): `lore ui`, one real
question in a headless browser (Playwright): steps arrive, claims render,
the graph draws, a citation opens its passage.

---

## 8. Risks

- **The answer pipeline is still being tuned** on `answer-quality`. The UI
  depends only on the `lore_search` result shape and the event hook, both
  additive; the hook and the template id columns are small, separate
  commits so the two branches merge cleanly.
- **Sigma.js learning curve** for the staged animation: the reducers make it
  declarative; if a stage proves awkward, fall back to plain node/edge
  attribute updates driven by the same derived state.
- **Gemini quota during development.** Frontend work uses recorded event
  streams; only the end-to-end test and manual checks spend requests.
