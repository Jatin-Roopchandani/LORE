# Phase 5a Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A browser UI where you ask LORE a question, watch live steps and the evidence graph light up stage by stage, read a cited answer, and explore entities -- served by a new `lore ui` command.

**Architecture:** A thin FastAPI app (`lore/web/`) on 127.0.0.1:8766 calls the same functions in `lore/mcp/tools.py` the MCP server uses, plus one SSE endpoint fed by a new optional `on_event` hook in `answer_question`. A React + TypeScript + Vite app (`web/`) builds into `lore/web/static/`; one pure reducer turns the event stream into state that both the chat and the Sigma.js graph read.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, pytest (+ pytest-asyncio, auto mode); React 18, TypeScript 5, Vite 5, Vitest 2, @testing-library/react, jsdom, sigma 3, graphology, @fontsource fonts; Playwright (opt-in e2e).

**Spec:** `docs/superpowers/specs/2026-10-07-phase-5a-web-ui-design.md` (and its mock, `docs/superpowers/specs/assets/2026-10-07-phase-5a-mock.html`, the visual reference for every component).

## Global Constraints

- Work happens in the worktree `/home/jatin/LORE-ui` on branch `phase-5a-web-ui`. Never switch branches in `/home/jatin/LORE` (answer-quality evals run there).
- Git identity must be `jatinroopchandani3007@gmail.com` (check `git config user.email` before every commit); push to `origin` (github-personal).
- Python: run tests with `.venv/bin/python -m pytest` from the worktree root. If the worktree has no `.venv`, create it with `uv sync` first.
- Ollama, if a step needs it, runs CPU-only: `CUDA_VISIBLE_DEVICES=-1 OLLAMA_VULKAN=0 ~/.local/ollama/bin/ollama serve`. Stop it with `pkill -x ollama` only. Never `pkill -f`.
- Never print, log or commit the Gemini key (`~/.config/lore/gemini.env`) or the Kaggle token.
- `lore ui` binds loopback only: 127.0.0.0/8, `localhost`, `::1`. Default port **8766**. The MCP server stays on 8765 and is not modified except where a task says so.
- With `on_event=None`, `answer_question` behaves exactly as before (same calls, same answer).
- Edge key format, everywhere: `subject_id|predicate|object_id|chunk_id`.
- Per-part edge cap in events: **150** (`EDGES_PER_PART_CAP`), with the true `total`.
- Question length: 1-2000 characters after stripping, else HTTP 400.
- All user-facing wording in the frontend comes from `web/src/messages.ts`. Provider names (Gemini, Ollama) appear only there and in components gated by `show_internals`.
- Frontend bundles its fonts (@fontsource). No CDN at runtime.
- Respect `prefers-reduced-motion`: no stagger, no pulse.
- Theme: CSS variables on `:root`, dark values under `@media (prefers-color-scheme: dark) :root:not([data-theme="light"])` and `:root[data-theme="dark"]`. The toggle sets `data-theme`; "System" removes it.

## Review Focus

1. **Closing the tab mid-answer**: the run must stop and spend no further Gemini requests. → Task 5, `test_closing_the_stream_cancels_the_run`.
2. **A seed or edge with a non-finite score in an event** (named seeds score 0.0, but distances can be `inf`): the stream must still be valid JSON. → Task 5, `test_events_with_infinite_scores_are_valid_json`.
3. **Two entities with the same name**: clicking a node must open the entity that was traversed, not an "ambiguous" error. → Task 1 (ids on edges), Task 4 (`/api/entity?id=`), `test_entity_by_id_opens_that_entity_when_names_collide`.
4. **A quote with an ellipsis, curly quotes or em-dashes**: the cited passage must still highlight the quoted words. → Task 11, `highlight.test.ts`.
5. **Browser storage blocked or full**: history is lost but the page works. → Task 9, `history.test.ts` "survives a throwing storage".

---

## File Structure

**Backend (modify):**
- `lore/query/models.py`: `EdgeRow` gains `subject_id`, `object_id`.
- `lore/query/templates.py`: every template also returns `a.id AS subject_id, b.id AS object_id`; ordering and fact keys include them.
- `lore/query/subgraph.py`: `run_template` maps the two new columns.
- `lore/query/agent.py`: `answer_question(..., on_event=None)`; emits events at real stages.
- `lore/mcp/shapes.py`: `to_search_result` fills `subject_id`/`object_id` on edges.
- `lore/mcp/tools.py`: `lore_search(..., on_event=None)` passes the hook through; new `entity_by_id`, `subgraph_by_id`.
- `lore/config.py`: `WebConfig` (`show_internals`, `port`) as `LoreConfig.web`.
- `lore/cli.py`: `lore ui` command.
- `pyproject.toml`: `fastapi`, `uvicorn` dependencies; hatch artifacts for `lore/web/static`.
- `.gitignore`: `lore/web/static/*` except `.gitkeep`; `web/node_modules`.

**Backend (create):**
- `lore/query/events.py`: event names, `emit()`, `EDGES_PER_PART_CAP`, `edge_key()`, JSON-safe conversion.
- `lore/web/__init__.py`: empty.
- `lore/web/security.py`: loopback check plus a Host/Origin ASGI middleware.
- `lore/web/ask.py`: `ask_events()`, the async generator behind `POST /api/ask` (busy lock, queue, cancellation, SSE framing).
- `lore/web/app.py`: `create_app(config, *, dev=False)`, the routes and static serving.
- `lore/web/static/.gitkeep`
- `scripts/record_ask.py`: records a real `/api/ask` event stream to a fixture.
- Tests: `tests/test_query_events.py`, `tests/test_web_security.py`, `tests/test_web_app.py`, `tests/test_web_ask.py` (plus additions to `tests/test_query_templates.py`, `tests/test_query_agent.py`, `tests/test_mcp_tools.py`).

**Frontend (create, under `web/`):**
- `package.json`, `tsconfig.json`, `vite.config.ts`, `index.html`
- `src/main.tsx`: mount.
- `src/theme.css`: tokens, both themes, base styles.
- `src/types.ts`: API and event types.
- `src/api.ts`: fetch wrappers and the SSE stream reader.
- `src/askReducer.ts`: `(state, event) -> state`.
- `src/graphView.ts`: pure derivation of graph nodes and edges from state.
- `src/layout.ts`: deterministic placement that never moves placed nodes.
- `src/scheduler.ts`: stagger, replay timing, reduced motion.
- `src/history.ts`: history records in browser storage, fault-tolerant.
- `src/messages.ts`: every user-facing string, by code.
- `src/highlight.ts`: quote highlighting in passages.
- `src/useAsk.ts`: React hook wiring api + scheduler + reducer + history.
- `src/components/TopBar.tsx`, `BooksDropdown.tsx`, `SettingsMenu.tsx`, `AddBookDialog.tsx`, `QuotaChip.tsx`
- `src/components/ChatPane.tsx`, `StepList.tsx`, `Claims.tsx`, `HowPanel.tsx`, `AskBox.tsx`, `HistoryBar.tsx`
- `src/components/GraphPane.tsx`, `GraphCanvas.tsx`, `EntityCard.tsx`
- `src/App.tsx`
- `src/fixtures/*.json`: recorded and synthetic event streams.
- Tests next to sources: `src/*.test.ts(x)`.
- `e2e/ask.spec.ts`, `playwright.config.ts`: opt-in end-to-end.

---

### Task 1: Entity ids on template rows and search edges

**Files:**
- Modify: `lore/query/models.py` (class `EdgeRow`, ~line 45)
- Modify: `lore/query/templates.py` (`ROW_FIELDS`, `_RETURN`, `_ORDER`, `_ORDER_BY_CHAPTER`, `_FACT_KEY`, `_ACROSS_BOOKS`)
- Modify: `lore/query/subgraph.py` (`run_template`)
- Modify: `lore/mcp/shapes.py` (`to_search_result`, edges list ~line 236)
- Test: `tests/test_query_templates.py`, `tests/test_mcp_tools.py`

**Interfaces:**
- Produces: `EdgeRow(subject, predicate, predicate_raw, object, evidence, confidence, book_id, chapter_seq, chunk_id, subject_id: str = "", object_id: str = "")`; `ROW_FIELDS` ends with `"subject_id", "object_id"`; `lore_search` result edges carry real `subject_id`/`object_id`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_query_templates.py`:

```python
from lore.db.kuzu_store import KuzuStore  # noqa: E402
from lore.query.models import TemplateCall  # noqa: E402
from lore.query.subgraph import run_template  # noqa: E402
from tests.mcp_fixtures import build_store_triple  # noqa: E402


def test_every_template_returns_entity_ids(tmp_path) -> None:
    # Names are not unique; the UI must open the entity actually traversed.
    config = build_store_triple(
        tmp_path,
        books=[("b1", "A Study in Scarlet")],
        chunks=[("b1:1:0", "b1", 1)],
        entities=[("e1", "Holmes", "Character", ["b1"], []),
                  ("e2", "Watson", "Character", ["b1"], [])],
        edges=[("e1", "e2", "INTERACTS_WITH", "b1:1:0", "b1", 1)],
    )
    kuzu = KuzuStore(config.kuzu_dir / "graph", read_only=True)
    slots = {"entity_id": "e1", "a_id": "e1", "b_id": "e2", "limit": 10,
             "predicate": "INTERACTS_WITH", "lo": 0, "hi": 99}
    for name, template in TEMPLATES.items():
        call = TemplateCall(template=name, slots={s: slots[s] for s in template.slots})
        rows = run_template(kuzu, call)
        assert rows, f"{name} returned nothing"
        assert {(r.subject_id, r.object_id) for r in rows} == {("e1", "e2")}, name
```

Append to `tests/test_mcp_tools.py`:

```python
def test_search_result_edges_carry_ids():
    from lore.mcp.shapes import to_search_result
    from lore.query.models import Answer, EdgeRow

    edge = EdgeRow("Holmes", "INTERACTS_WITH", "met", "Watson", "ev", 0.9,
                   "b1", 1, "b1:1:0", subject_id="e1", object_id="e2")
    answer = Answer(text="x", passages=(), mode="graph", template="neighbours",
                    note=None, edges=(edge,))
    out = to_search_result(answer, {}).model_dump()
    assert (out["edges"][0]["subject_id"], out["edges"][0]["object_id"]) == ("e1", "e2")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_query_templates.py::test_every_template_returns_entity_ids tests/test_mcp_tools.py::test_search_result_edges_carry_ids`
Expected: FAIL. `EdgeRow` has no `subject_id` (TypeError), and the template rows have no ids.

Before writing code, check how `TemplateCall` and `Answer` are constructed in `lore/query/models.py`, and adapt the two test constructors if the field names differ (e.g. `slots` as a dict vs. tuple pairs; `Answer`'s `edges` keyword).

- [ ] **Step 3: Implement**

In `lore/query/models.py`, add to `EdgeRow` after `chunk_id: str`:

```python
    # Filled by every template (Phase 5a): names are not unique, and the UI
    # opens the entity that was traversed. "" where a producer has no ids.
    subject_id: str = ""
    object_id: str = ""
```

In `lore/query/templates.py`:

```python
ROW_FIELDS: tuple[str, ...] = (
    "subject", "predicate", "predicate_raw", "object", "evidence",
    "confidence", "book_id", "chapter_seq", "chunk_id",
    "subject_id", "object_id",
)

_RETURN = (
    "RETURN DISTINCT a.name AS subject, r.predicate AS predicate, "
    "r.predicate_raw AS predicate_raw, b.name AS object, "
    "r.evidence AS evidence, r.confidence AS confidence, "
    "r.book_id AS book_id, r.chapter_seq AS chapter_seq, "
    "r.chunk_id AS chunk_id, a.id AS subject_id, b.id AS object_id "
)

_ORDER = (
    "ORDER BY book_id, chapter_seq, chunk_id, predicate, predicate_raw, "
    "subject, object, confidence, evidence, subject_id, object_id "
)

_ORDER_BY_CHAPTER = (
    "ORDER BY chapter_seq, book_id, chunk_id, predicate, predicate_raw, "
    "subject, object, confidence, evidence, subject_id, object_id "
)
```

Extend `_FACT_KEY` with two more terms: `_sized("subject_id"), _sized("object_id")`. In `_ACROSS_BOOKS`:
- add `, a.id AS subject_id, b.id AS object_id` to the `WITH DISTINCT ...` list;
- add `subject_id := subject_id, object_id := object_id, ` inside `struct_pack(...)` before `k := `;
- add `fact.subject_id AS subject_id, fact.object_id AS object_id` to the final `RETURN` (after `fact.chunk_id AS chunk_id`);
- append `, subject_id, object_id` to its final `ORDER BY`.

Ids join the ordering because two distinct entities can share a name. Without the ids, two such rows would tie and their order would depend on storage.

In `lore/query/subgraph.py`, `run_template`:

```python
    return [
        EdgeRow(
            subject=str(row[0]), predicate=str(row[1]), predicate_raw=str(row[2]),
            object=str(row[3]), evidence=str(row[4]), confidence=float(row[5]),
            book_id=str(row[6]), chapter_seq=int(row[7]), chunk_id=str(row[8]),
            subject_id=str(row[9]), object_id=str(row[10]),
        )
        for row in rows
    ]
```

In `lore/mcp/shapes.py`, `to_search_result`, add to the `EdgeOut(...)` call: `subject_id=e.subject_id, object_id=e.object_id`.

- [ ] **Step 4: Run the tests to verify they pass, then the query and MCP suites**

Run: `.venv/bin/python -m pytest -q tests/test_query_templates.py tests/test_mcp_tools.py tests/test_query_agent.py tests/test_query_subgraph.py`
Expected: all PASS. If a fake Kuzu in a test returns 9-column rows to `run_template`, extend that fake's rows with two id strings rather than weakening `run_template`.

- [ ] **Step 5: Commit**

```bash
git config user.email   # must print jatinroopchandani3007@gmail.com
git add lore/query/models.py lore/query/templates.py lore/query/subgraph.py lore/mcp/shapes.py tests/
git commit -m "feat: template rows and search edges carry entity ids

Names are not unique, and the web UI opens the entity a search actually
traversed. Ids also join the row order, so two same-name entities never
tie."
```

---

### Task 2: The event hook in answer_question

**Files:**
- Create: `lore/query/events.py`
- Modify: `lore/query/agent.py` (`answer_question`)
- Modify: `lore/mcp/tools.py` (`lore_search`)
- Test: `tests/test_query_events.py`, `tests/test_query_agent.py`

**Interfaces:**
- Consumes: `EdgeRow` with ids (Task 1).
- Produces:
  - `lore.query.events`: `OnEvent = Callable[[dict], None]`; `emit(on_event: OnEvent | None, type_: str, **data) -> None`; `edge_key(edge: EdgeRow) -> str`; `json_safe(value) -> value` (non-finite floats → None, tuples → lists, dataclasses → dicts); `EDGES_PER_PART_CAP = 150`.
  - `answer_question(conn, kuzu, lance, question, models, *, baseline=False, on_event: OnEvent | None = None)`
  - `lore_search(config, query, models=None, on_event=None)`
  - Event dicts, all with key `"type"`:
    - `{"type":"step","name":<split|seeds|search|rank|write|check>,"state":<start|done>,"part":int|None,"detail":str|None}`
    - `{"type":"parts","sub_questions":[str]}`
    - `{"type":"seeds","part":int,"entities":[{"id","name","kind","books","score"}]}`
    - `{"type":"edges","part":int,"template":str|None,"total":int,"edges":[EdgeRow as dict]}`
    - `{"type":"evidence","chunk_ids":[str],"edge_keys":[str]}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_query_events.py`:

```python
"""The event helpers: emitting never fails an answer, payloads are JSON-safe."""
import json
import math

from lore.query.events import EDGES_PER_PART_CAP, edge_key, emit, json_safe
from lore.query.models import EdgeRow, SeedEntity


def test_emit_calls_the_hook_with_a_typed_dict():
    seen = []
    emit(seen.append, "step", name="split", state="start", part=None, detail=None)
    assert seen == [{"type": "step", "name": "split", "state": "start",
                     "part": None, "detail": None}]


def test_emit_without_a_hook_does_nothing():
    emit(None, "step", name="split", state="start")


def test_a_raising_hook_never_fails_the_caller(caplog):
    def broken(event):
        raise RuntimeError("socket closed")
    emit(broken, "parts", sub_questions=["a"])
    assert "socket closed" in caplog.text


def test_json_safe_turns_dataclasses_and_infinities_into_json():
    seed = SeedEntity(id="e1", name="Holmes", kind="Character", books=("b1",),
                      score=math.inf)
    out = json_safe({"entities": [seed]})
    assert out == {"entities": [{"id": "e1", "name": "Holmes", "kind": "Character",
                                 "books": ["b1"], "score": None}]}
    json.dumps(out, allow_nan=False)


def test_edge_key_is_subject_predicate_object_chunk():
    e = EdgeRow("Holmes", "KILLS", "k", "X", "ev", 0.9, "b1", 1, "b1:1:0",
                subject_id="e1", object_id="e2")
    assert edge_key(e) == "e1|KILLS|e2|b1:1:0"


def test_the_cap_is_150():
    assert EDGES_PER_PART_CAP == 150
```

Append to `tests/test_query_agent.py`, in the Task 10d section after `_scripted` (reuses `_conn`, `_chunk`, `_row`, `VectorLance`, `EntityKuzu`, `_edge_row`, `_scripted`, `Q`, `A`, `B`):

```python
# --- Phase 5a: the event hook -------------------------------------------------


def _edge_row_ids(chunk_id, chapter, subject):
    return [subject, "UNCATEGORIZED", "met", "X", "ev", 0.9, "b1", chapter,
            chunk_id, "e1", "x1"]


async def _ask_with_events(tmp_path, on_event):
    conn = _conn(tmp_path)
    lance = VectorLance({tuple(Q): [_row("b1:1:0", ["e1"], 0.2, 1)]}, {"b1:1:0": Q})
    kuzu = EntityKuzu({"e1": [_edge_row_ids("b1:1:0", 1, "Holmes")]})
    models, calls = _scripted({QUERY_PREFIX + "who?": Q}, ["who?"],
                              [TemplateChoice(template="neighbours", entities=[1])],
                              GOOD)
    answer = await answer_question(conn, kuzu, lance, "who?", models, on_event=on_event)
    conn.close()
    return answer, calls


async def test_events_arrive_in_stage_order(tmp_path: Path) -> None:
    events = []
    await _ask_with_events(tmp_path, events.append)
    kinds = [(e["type"], e.get("name"), e.get("state")) for e in events]
    assert kinds == [
        ("step", "split", "start"), ("parts", None, None), ("step", "split", "done"),
        ("step", "seeds", "start"), ("seeds", None, None), ("step", "seeds", "done"),
        ("step", "search", "start"), ("edges", None, None), ("step", "search", "done"),
        ("step", "rank", "start"), ("evidence", None, None), ("step", "rank", "done"),
        ("step", "write", "start"), ("step", "write", "done"),
    ]
    seeds = next(e for e in events if e["type"] == "seeds")
    assert seeds["part"] == 0 and seeds["entities"][0]["id"] == "e1"
    edges = next(e for e in events if e["type"] == "edges")
    assert edges["total"] == 1 and edges["edges"][0]["subject_id"] == "e1"
    evidence = next(e for e in events if e["type"] == "evidence")
    assert evidence["chunk_ids"] == ["b1:1:0"]
    assert evidence["edge_keys"] == ["e1|UNCATEGORIZED|x1|b1:1:0"]


async def test_the_hook_changes_nothing_about_the_answer(tmp_path: Path) -> None:
    with_hook, calls_a = await _ask_with_events(tmp_path / "a", lambda e: None)
    without, calls_b = await _ask_with_events(tmp_path / "b", None)
    assert with_hook == without
    assert calls_a == calls_b


async def test_a_raising_hook_does_not_fail_the_answer(tmp_path: Path) -> None:
    def broken(event):
        raise RuntimeError("client went away")
    answer, _ = await _ask_with_events(tmp_path, broken)
    assert answer.mode == "graph"


async def test_check_steps_wrap_the_faithfulness_call(tmp_path: Path) -> None:
    from dataclasses import replace as dc_replace
    from lore.query.synthesize import Judgement, Judgements  # adapt names: see Step 2 note

    events = []
    conn = _conn(tmp_path)
    lance = VectorLance({tuple(Q): [_row("b1:1:0", ["e1"], 0.2, 1)]}, {"b1:1:0": Q})
    kuzu = EntityKuzu({"e1": [_edge_row_ids("b1:1:0", 1, "Holmes")]})
    models, _ = _scripted({QUERY_PREFIX + "who?": Q}, ["who?"],
                          [TemplateChoice(template="neighbours", entities=[1])], GOOD)

    async def check(prompt, schema):
        return Judgements(judgements=[Judgement(claim=1, verdict="supported", reason="ok")])

    models = dc_replace(models, check=check)
    await answer_question(conn, kuzu, lance, "who?", models, on_event=events.append)
    conn.close()
    tail = [(e["name"], e["state"]) for e in events if e["type"] == "step"][-4:]
    assert tail == [("write", "start"), ("write", "done"), ("check", "start"), ("check", "done")]
```

Make `_scripted` accept the `GOOD` synthesis positionally (it already takes `synthesis=None` as its fourth parameter).

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_query_events.py tests/test_query_agent.py -k "event or hook or check_steps"`
Expected: FAIL with `ModuleNotFoundError: lore.query.events` and `unexpected keyword argument 'on_event'`.

Note: open `lore/query/synthesize.py` and find the faithfulness schema classes (search for `judgements`). Use their real names and fields in `test_check_steps_wrap_the_faithfulness_call`.

- [ ] **Step 3: Implement `lore/query/events.py`**

```python
"""Progress events from the query agent, for the web UI's live view.

`answer_question` calls `emit` at each real stage when given a hook. The
hook is optional and best-effort: with none, nothing changes; a hook that
raises is logged and ignored, never failing an answer. Payloads are made
JSON-safe here (non-finite scores become None), so every consumer can
serialise them with allow_nan=False.
"""
from __future__ import annotations

import dataclasses
import logging
import math
from typing import Any, Callable

log = logging.getLogger(__name__)

OnEvent = Callable[[dict], None]

# Edges sent per sub-question; the true count travels as `total`. A path
# template can return hundreds of rows, more than a graph view can show.
EDGES_PER_PART_CAP = 150


def json_safe(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def emit(on_event: OnEvent | None, type_: str, **data: Any) -> None:
    if on_event is None:
        return
    try:
        on_event({"type": type_, **json_safe(data)})
    except Exception:  # a broken listener must never fail an answer
        log.exception("query event listener failed on %r", type_)


def edge_key(edge) -> str:
    return f"{edge.subject_id}|{edge.predicate}|{edge.object_id}|{edge.chunk_id}"
```

- [ ] **Step 4: Implement the hook in `answer_question`**

In `lore/query/agent.py`, add the import `from lore.query.events import EDGES_PER_PART_CAP, OnEvent, edge_key, emit`, and change `answer_question`:

1. Signature: add `on_event: OnEvent | None = None` after `baseline`.
2. Right after `if baseline: ...` returns, wrap the check so the write/check steps are real:

```python
    if on_event is not None:
        models = _with_check_events(models, on_event)
```

with this module-level helper (after `_cosine`):

```python
def _with_check_events(models: QueryModels, on_event: OnEvent) -> QueryModels:
    """`models` whose faithfulness check announces itself. Writing ends,
    and checking begins, exactly when the check is called."""
    if models.check is None:
        return models
    check = models.check

    async def announced(prompt, schema):
        emit(on_event, "step", name="write", state="done", part=None, detail=None)
        emit(on_event, "step", name="check", state="start", part=None, detail=None)
        reply = await check(prompt, schema)
        verdicts = [getattr(j, "verdict", None) for j in getattr(reply, "judgements", [])]
        dropped = sum(v == "not_supported" for v in verdicts)
        emit(on_event, "step", name="check", state="done", part=None,
             detail="all supported" if not dropped else f"{dropped} not supported")
        return reply

    return replace(models, check=announced)
```

3. Around `parts, decomposed = await _parts(...)`:

```python
    emit(on_event, "step", name="split", state="start", part=None, detail=None)
    parts, decomposed = await _parts(question, vector, models, kuzu)
    sub_questions = tuple(part for part, _ in parts)
    emit(on_event, "parts", sub_questions=list(sub_questions))
    emit(on_event, "step", name="split", state="done", part=None,
         detail=f"{len(parts)} part" + ("s" if len(parts) != 1 else ""))
```

4. Inside the `for part, part_vector in parts:` loop, number the parts (`for index, (part, part_vector) in enumerate(parts):`), and:

```python
        emit(on_event, "step", name="seeds", state="start", part=index, detail=None)
        named = ...  # unchanged
        seeds = await seed_entities(...)  # unchanged
        emit(on_event, "seeds", part=index, entities=list(seeds))
        emit(on_event, "step", name="seeds", state="done", part=index,
             detail=f"{len(seeds)} found")
        if not seeds:
            continue
        ...  # unchanged up to the select call
        emit(on_event, "step", name="search", state="start", part=index, detail=None)
        call, model = await _select(part, seeds, models)
        ...  # unchanged
        if call is None:
            emit(on_event, "edges", part=index, template=None, total=0, edges=[])
            emit(on_event, "step", name="search", state="done", part=index,
                 detail="no graph search fitted")
            continue
        ...
        found = run_template(kuzu, call)
        rows.extend(found)
        emit(on_event, "edges", part=index, template=call.template, total=len(found),
             edges=found[:EDGES_PER_PART_CAP])
        emit(on_event, "step", name="search", state="done", part=index,
             detail=f"{call.template.replace('_', ' ')} · {len(found)} relationships")
```

(The `seeded = True` and seed-dedupe lines stay where they are, after `if not seeds: continue`.)

5. Around `evidence = assemble_ranked(...)`:

```python
    emit(on_event, "step", name="rank", state="start", part=None, detail=None)
    evidence = assemble_ranked(...)  # unchanged
    emit(on_event, "evidence", chunk_ids=[p.chunk_id for p in evidence.passages],
         edge_keys=[edge_key(e) for e in evidence.edges])
    emit(on_event, "step", name="rank", state="done", part=None,
         detail=f"{len(evidence.passages)} passages")
    emit(on_event, "step", name="write", state="start", part=None, detail=None)
```

6. After the `if rows: answer = ... else: answer = ...` block, close the write step when no check ran:

```python
    if models.check is None or not answer.claims:
        emit(on_event, "step", name="write", state="done", part=None,
             detail=f"{len(answer.claims)} claims")
```

(When the check ran, the wrapper already sent `write done`. When the check exists but had nothing to judge, it was never called, so `write done` comes from here.)

- [ ] **Step 5: Pass the hook through `lore_search`**

In `lore/mcp/tools.py`, change the signature to `async def lore_search(config, query, models=None, on_event=None)` and the call to `answer_question(stores.conn, stores.kuzu, stores.lance, query, models, on_event=on_event)`. In `tests/test_mcp_budget.py`, the autouse `fake_answer` fake must accept the new keyword: change its signature to `async def answer(conn, kuzu, lance, question, models, *, baseline=False, on_event=None):`.

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_query_events.py tests/test_query_agent.py tests/test_mcp_budget.py tests/test_mcp_tools.py tests/test_mcp_server.py`
Expected: all PASS. If `test_events_arrive_in_stage_order` shows `write done` twice, the check is non-None in `_scripted` but was not called. Keep step 6's condition as written; it fires only if the check never ran, so look for an earlier extra emit.

- [ ] **Step 7: Commit**

```bash
git config user.email
git add lore/query/events.py lore/query/agent.py lore/mcp/tools.py tests/
git commit -m "feat: answer_question reports each real stage to an optional hook

Split, seeds and search per part, ranking, writing and checking, with
the per-part seeds and edges (capped at 150, true total sent) and what
made the evidence cut. Without a hook nothing changes; a raising hook is
logged, never fatal."
```

---

### Task 3: Web config, loopback and Host/Origin security

**Files:**
- Modify: `lore/config.py` (add `WebConfig`, field `web` on `LoreConfig`)
- Create: `lore/web/__init__.py` (empty), `lore/web/security.py`
- Modify: `pyproject.toml` (dependencies)
- Test: `tests/test_web_security.py`

**Interfaces:**
- Produces:
  - `WebConfig(show_internals: bool = True, port: int = 8766)`, reachable as `config.web`; `[lore.web]` in lore.toml.
  - `lore.web.security.check_loopback(host: str) -> None` (raises `NonLoopbackHost`, re-exported from `lore.mcp.server`).
  - `lore.web.security.allowed_hosts(host, port) -> set[str]`, `allowed_origins(host, port, dev) -> set[str]`.
  - `lore.web.security.OriginGuard(app, hosts: set[str], origins: set[str])`, a pure ASGI middleware. For `http` scopes it returns 403 JSON `{"error":{"code":"forbidden","message":...}}` when the Host header is not in `hosts`, or when an Origin header is present and not in `origins`.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, add `"fastapi>=0.115"` and `"uvicorn>=0.30"` to `dependencies`. Run `uv sync` and confirm `uv run python -c "import fastapi, uvicorn"` exits 0.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_web_security.py`:

```python
"""lore ui is unauthenticated, so it must only be reachable from this machine
and only be drivable by its own page."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lore.config import LoreConfig
from lore.web.security import (
    NonLoopbackHost, OriginGuard, allowed_hosts, allowed_origins, check_loopback,
)


def test_web_config_defaults():
    config = LoreConfig()
    assert config.web.show_internals is True and config.web.port == 8766


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1"])
def test_loopback_hosts_are_allowed(host):
    check_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", ""])
def test_other_hosts_are_refused(host):
    with pytest.raises(NonLoopbackHost):
        check_loopback(host)


def _client(dev=False):
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    guarded = OriginGuard(app, allowed_hosts("127.0.0.1", 8766),
                          allowed_origins("127.0.0.1", 8766, dev))
    return TestClient(guarded, base_url="http://127.0.0.1:8766")


def test_same_origin_requests_pass():
    r = _client().get("/api/ping", headers={"Origin": "http://127.0.0.1:8766"})
    assert r.status_code == 200


def test_requests_without_origin_pass():  # curl, the browser's own navigation
    assert _client().get("/api/ping").status_code == 200


def test_a_foreign_origin_is_refused():
    r = _client().get("/api/ping", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


def test_a_rebound_host_is_refused():
    r = _client().get("/api/ping", headers={"Host": "evil.example:8766"})
    assert r.status_code == 403


def test_the_vite_dev_origin_passes_only_in_dev():
    headers = {"Origin": "http://localhost:5173"}
    assert _client(dev=False).get("/api/ping", headers=headers).status_code == 403
    assert _client(dev=True).get("/api/ping", headers=headers).status_code == 200
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_web_security.py`
Expected: FAIL with `ModuleNotFoundError: lore.web`.

- [ ] **Step 4: Implement**

In `lore/config.py`, before `class LoreConfig`:

```python
class WebConfig(BaseModel):
    """`lore ui` (Phase 5a). show_internals gates provider details (model
    names, the Gemini quota, Ollama commands, the MCP panel): on for a
    local install, off for a future deployed build."""

    show_internals: bool = True
    port: int = 8766
```

and the field `web: WebConfig = Field(default_factory=WebConfig)` on `LoreConfig` (after `adjudication`).

Create `lore/web/__init__.py` (empty) and `lore/web/security.py`:

```python
"""lore ui has no authentication, so: bind loopback only, and serve only
requests addressed to this server (Host) from its own page (Origin). Same
reasoning as the MCP server's DNS-rebinding protection: any page in the
user's browser can otherwise reach a localhost port and spend Gemini."""
from __future__ import annotations

import json

from lore.mcp.server import NonLoopbackHost, _check_loopback

__all__ = ["NonLoopbackHost", "OriginGuard", "allowed_hosts", "allowed_origins",
           "check_loopback"]

VITE_DEV_PORT = 5173


def check_loopback(host: str) -> None:
    _check_loopback(host)


def _shown(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def allowed_hosts(host: str, port: int) -> set[str]:
    return {f"{_shown(host)}:{port}", f"127.0.0.1:{port}", f"localhost:{port}"}


def allowed_origins(host: str, port: int, dev: bool) -> set[str]:
    origins = {f"http://{h}" for h in allowed_hosts(host, port)}
    if dev:
        origins |= {f"http://localhost:{VITE_DEV_PORT}",
                    f"http://127.0.0.1:{VITE_DEV_PORT}"}
    return origins


class OriginGuard:
    """Pure ASGI middleware (not BaseHTTPMiddleware: it must not buffer the
    SSE stream or hide client disconnects from it)."""

    def __init__(self, app, hosts: set[str], origins: set[str]) -> None:
        self.app, self.hosts, self.origins = app, hosts, origins

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            host, origin = headers.get("host"), headers.get("origin")
            if host not in self.hosts:
                return await _forbidden(send, "this server only answers requests"
                                        " addressed to it on this machine")
            if origin is not None and origin not in self.origins:
                return await _forbidden(send, "requests from other web pages are"
                                        " refused")
        await self.app(scope, receive, send)


async def _forbidden(send, message: str) -> None:
    body = json.dumps({"error": {"code": "forbidden", "message": message}}).encode()
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_web_security.py tests/test_config.py`
Expected: PASS. If `tests/test_config.py` does not exist, run only the first file.

- [ ] **Step 6: Commit**

```bash
git config user.email
git add pyproject.toml uv.lock lore/config.py lore/web/ tests/test_web_security.py
git commit -m "feat: web config and the loopback, Host and Origin guard for lore ui"
```

---

### Task 4: Read endpoints and entity lookup by id

**Files:**
- Modify: `lore/mcp/tools.py` (add `entity_by_id`, `subgraph_by_id`)
- Create: `lore/web/app.py`
- Create: `lore/web/static/.gitkeep`
- Modify: `.gitignore`
- Test: `tests/test_web_app.py`, `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: `OriginGuard`, `allowed_hosts`, `allowed_origins` (Task 3); `config.web` (Task 3).
- Produces:
  - `tools.entity_by_id(config, entity_id) -> dict` (same shape as `tools.entity`; `not_found` if no such id).
  - `tools.subgraph_by_id(config, entity_id, depth=1) -> dict` (same shape as `tools.subgraph`).
  - `lore.web.app.create_app(config, *, host="127.0.0.1", port=8766, dev=False) -> ASGI app` (the FastAPI app wrapped in `OriginGuard`; the inner FastAPI is `app.app`).
  - Routes:
    - `GET /api/library` → `tools.library(config)`
    - `GET /api/entity?id=` or `?name=` → `entity_by_id` / `entity`
    - `GET /api/subgraph?id=|name=&depth=1` → `subgraph_by_id` / `subgraph`
    - `GET /api/quota` → `{"models":[{model,count,limit,remaining}], "resets_at": ISO8601 str, "provider": "gemini"|"local"}`
    - `GET /api/mcp` → `{"stdio_command": str, "http_command": str, "http_url": str, "http_running": bool}`
    - `GET /api/settings` → `{"show_internals": bool}`
    - `/` → static `index.html` if built, else a plain HTML page saying how to build.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_tools.py`:

```python
def test_entity_by_id_opens_that_entity_when_names_collide(tmp_path):
    from lore.mcp.tools import entity, entity_by_id, subgraph_by_id
    config = build_store_triple(
        tmp_path,
        books=[("b1", "A Study")],
        chunks=[("c1", "b1", 1)],
        entities=[("e1", "Stangerson", "Character", ["b1"], []),
                  ("e2", "Stangerson", "Character", ["b1"], []),
                  ("e3", "Hope", "Character", ["b1"], [])],
        edges=[("e3", "e2", "KILLS", "c1", "b1", 1)],
    )
    assert entity(config, "Stangerson")["error"]["code"] == "not_found"  # ambiguous by name
    card = entity_by_id(config, "e2")
    assert card["id"] == "e2" and card["edges_in"][0]["subject_id"] == "e3"
    assert entity_by_id(config, "nope")["error"]["code"] == "not_found"
    sub = subgraph_by_id(config, "e2")
    assert {n["id"] for n in sub["nodes"]} == {"e2", "e3"}
```

Create `tests/test_web_app.py`:

```python
"""The read endpoints: thin pass-throughs to lore.mcp.tools."""
import pytest
from fastapi.testclient import TestClient

from lore.web.app import create_app
from tests.mcp_fixtures import build_store_triple


@pytest.fixture
def config(tmp_path):
    return build_store_triple(
        tmp_path,
        books=[("b1", "A Study in Scarlet")],
        chunks=[("c1", "b1", 1)],
        entities=[("e1", "Holmes", "Character", ["b1"], []),
                  ("e2", "Watson", "Character", ["b1"], [])],
        edges=[("e1", "e2", "INTERACTS_WITH", "c1", "b1", 1)],
    )


@pytest.fixture
def client(config):
    return TestClient(create_app(config), base_url="http://127.0.0.1:8766")


def test_library(client):
    books = client.get("/api/library").json()["books"]
    assert books[0]["title"] == "A Study in Scarlet"


def test_entity_by_name_and_by_id(client):
    assert client.get("/api/entity", params={"name": "Holmes"}).json()["id"] == "e1"
    assert client.get("/api/entity", params={"id": "e2"}).json()["name"] == "Watson"


def test_entity_needs_a_name_or_id(client):
    assert client.get("/api/entity").status_code == 400


def test_subgraph_by_id(client):
    body = client.get("/api/subgraph", params={"id": "e1"}).json()
    assert {n["id"] for n in body["nodes"]} == {"e1", "e2"}


def test_unknown_entity_is_a_tool_error_not_a_crash(client):
    r = client.get("/api/entity", params={"name": "Moriarty"})
    assert r.status_code == 200 and r.json()["error"]["code"] == "not_found"


def test_quota_reports_models_and_the_reset_time(client):
    body = client.get("/api/quota").json()
    assert "resets_at" in body and isinstance(body["models"], list)


def test_mcp_details(client):
    body = client.get("/api/mcp").json()
    assert body["stdio_command"].startswith("claude mcp add lore -- ")
    assert body["http_url"] == "http://127.0.0.1:8765/mcp"
    assert body["http_running"] in (True, False)


def test_settings_follow_config(config):
    hidden = config.model_copy(update={"web": config.web.model_copy(
        update={"show_internals": False})})
    client = TestClient(create_app(hidden), base_url="http://127.0.0.1:8766")
    assert client.get("/api/settings").json() == {"show_internals": False}


def test_root_serves_a_page_even_before_the_frontend_is_built(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_web_app.py tests/test_mcp_tools.py::test_entity_by_id_opens_that_entity_when_names_collide`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement `entity_by_id` and `subgraph_by_id`**

In `lore/mcp/tools.py`, after `subgraph`:

```python
def _exists(kuzu, entity_id: str) -> bool:
    return bool(list(kuzu.execute(
        "MATCH (e:Entity) WHERE e.id = $id RETURN e.id", {"id": entity_id})))


@_guarded
def entity_by_id(config: LoreConfig, entity_id: str) -> dict:
    """`entity`, for a caller that already holds the id (the web UI, from
    a search's edges). No name resolution, so no ambiguity."""
    with open_stores(config) as stores:
        if not _exists(stores.kuzu, entity_id):
            return error("not_found", f"no entity with id {entity_id!r}")
        canonical = canonical_ids(stores.kuzu, [entity_id])[0]
        return _entity_card(stores.kuzu, canonical, CARD_EDGE_CAP).model_dump()


@_guarded
def subgraph_by_id(config: LoreConfig, entity_id: str, depth: int = 1) -> dict:
    with open_stores(config) as stores:
        if not _exists(stores.kuzu, entity_id):
            return error("not_found", f"no entity with id {entity_id!r}")
        root = canonical_ids(stores.kuzu, [entity_id])[0]
        clamped = max(1, min(depth, MAX_DEPTH))
        nodes, edges, truncated = _expand(stores.kuzu, root, clamped,
                                          SUBGRAPH_NODE_CAP, SUBGRAPH_EDGE_CAP)
        return SubgraphOut(nodes=nodes, edges=edges, truncated=truncated,
                           depth=clamped, note=None).model_dump()
```

Before writing this, read `entity()` (~line 290) and copy exactly how it calls `_entity_card` and what cap constant it passes; use the same names. `canonical_ids` is already imported from `lore.query.seed` in this module. If not, import it.

- [ ] **Step 4: Implement `lore/web/app.py`**

```python
"""lore ui: the browser's API, a thin layer over lore.mcp.tools.

Every endpoint calls the same function the matching MCP tool calls, so
the UI, Claude Code and the evals see the same data and the same
structured errors (tool errors are HTTP 200 with an `error` body, as over
MCP; 4xx only for a malformed request to this API).
"""
from __future__ import annotations

import shutil
import socket
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from lore.config import LoreConfig
from lore.mcp import tools
from lore.orchestrator.gemini_budget import GOOGLE_DAY_ZONE, day_report, today
from lore.web.security import OriginGuard, allowed_hosts, allowed_origins

STATIC = Path(__file__).parent / "static"
MCP_PORT = 8765

_NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>LORE</title>
<body style="font:15px system-ui;margin:3rem;max-width:40rem">
<h1>LORE's web app is not built yet</h1>
<p>The API is running. To build the interface once:</p>
<pre>cd web &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>then reload this page.</p></body>"""


def _lore_executable() -> str:
    beside = Path(sys.executable).with_name("lore")
    return str(beside) if beside.exists() else (shutil.which("lore") or "lore")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _quota(config: LoreConfig) -> dict:
    now = datetime.now(GOOGLE_DAY_ZONE)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
    if config.synthesis.provider != "gemini":
        return {"provider": "local", "models": [], "resets_at": midnight.isoformat()}
    try:
        conn = sqlite3.connect(f"file:{config.sqlite_path}?mode=ro", uri=True)
        try:
            models = day_report(conn, today())
        finally:
            conn.close()
    except sqlite3.Error:  # no ledger yet: nothing spent
        models = []
    return {"provider": "gemini", "models": models, "resets_at": midnight.isoformat()}


def create_app(config: LoreConfig, *, host: str = "127.0.0.1", port: int = 8766,
               dev: bool = False):
    api = FastAPI(title="LORE", docs_url=None, redoc_url=None)

    @api.get("/api/library")
    def library():
        return tools.library(config)

    @api.get("/api/entity")
    def entity(id: str | None = None, name: str | None = None):
        if id:
            return tools.entity_by_id(config, id)
        if name:
            return tools.entity(config, name)
        raise HTTPException(400, "give id or name")

    @api.get("/api/subgraph")
    def subgraph(id: str | None = None, name: str | None = None,
                 depth: int = Query(1, ge=1, le=3)):
        if id:
            return tools.subgraph_by_id(config, id, depth)
        if name:
            return tools.subgraph(config, name, depth)
        raise HTTPException(400, "give id or name")

    @api.get("/api/quota")
    def quota():
        return _quota(config)

    @api.get("/api/mcp")
    def mcp():
        return {
            "stdio_command": f"claude mcp add lore -- {_lore_executable()} mcp",
            "http_command": f"lore mcp --http --port {MCP_PORT}",
            "http_url": f"http://127.0.0.1:{MCP_PORT}/mcp",
            "http_running": _port_open(MCP_PORT),
        }

    @api.get("/api/settings")
    def settings():
        return {"show_internals": config.web.show_internals}

    if (STATIC / "index.html").exists():
        api.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    else:
        @api.get("/", response_class=HTMLResponse)
        def not_built():
            return _NOT_BUILT

    return OriginGuard(api, allowed_hosts(host, port), allowed_origins(host, port, dev))
```

Expose the inner app for tests and later tasks: in `OriginGuard.__init__`, `self.app` already holds it. Task 5 adds a route to `api` before the static mount, so Task 5 restructures the order (see there).

Create `lore/web/static/.gitkeep` (empty). In `.gitignore`, add:

```
lore/web/static/*
!lore/web/static/.gitkeep
web/node_modules/
web/test-results/
web/playwright-report/
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_web_app.py tests/test_mcp_tools.py tests/test_web_security.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git config user.email
git add lore/mcp/tools.py lore/web/ .gitignore tests/
git commit -m "feat: lore ui read endpoints over the MCP tool functions

Library, entity and subgraph (by id as well as by name, so a search's
edges open the entity actually traversed), quota with its reset time,
MCP connection details, and the show_internals setting."
```

---

### Task 5: The ask stream (SSE), busy guard and cancellation

**Files:**
- Create: `lore/web/ask.py`
- Modify: `lore/web/app.py` (route `POST /api/ask`, before the static mount)
- Create: `scripts/record_ask.py`
- Test: `tests/test_web_ask.py`

**Interfaces:**
- Consumes: `tools.lore_search(config, query, models=None, on_event=None)` (Task 2); `create_app` (Task 4).
- Produces:
  - `lore.web.ask.sse(event: str, data: dict) -> bytes`: `b"event: <event>\ndata: <json>\n\n"` with `allow_nan=False`.
  - `lore.web.ask.ask_events(config, question: str, lock: asyncio.Lock, search=tools.lore_search) -> AsyncIterator[bytes]`.
  - Wire format: each SSE `data` is a JSON object with `"t"` (seconds since the request started, 2 decimals). For agent events it is the event dict minus `"type"` (the type is the SSE `event:` name). The final event is `answer` with `{"t", "result": <lore_search dict>}`, or `error` with `{"t", "error": {"code", "message"}}`. `busy` arrives as an `error` event whose code is `busy`.
  - `POST /api/ask` takes body `{"question": str}`, requires `Content-Type: application/json`, and returns `text/event-stream`. An empty or over-2000-character question gets 400.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_ask.py`:

```python
"""POST /api/ask: events stream in order, errors pass through, one question
at a time, and closing the stream stops the run (no more Gemini spend)."""
import asyncio
import json
import math

import pytest
from fastapi.testclient import TestClient

from lore.web.ask import ask_events, sse
from lore.web.app import create_app
from tests.mcp_fixtures import build_store_triple

RESULT = {"answer": "Holmes met Watson.", "mode": "graph", "claims": [],
          "passages": [], "edges": [], "seeds": [], "sub_questions": ["who?"]}


def parse(chunks: list[bytes]) -> list[tuple[str, dict]]:
    out = []
    for block in b"".join(chunks).decode().strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


async def fake_search(config, query, models=None, on_event=None):
    on_event({"type": "step", "name": "split", "state": "start", "part": None, "detail": None})
    on_event({"type": "parts", "sub_questions": ["who?"]})
    await asyncio.sleep(0)
    return RESULT


async def collect(gen):
    return [chunk async for chunk in gen]


def test_sse_framing_refuses_nan():
    assert sse("parts", {"t": 0.0, "sub_questions": []}).startswith(b"event: parts\ndata: ")
    with pytest.raises(ValueError):
        sse("x", {"v": math.nan})


async def test_events_then_the_answer():
    events = parse(await collect(ask_events(None, "who?", asyncio.Lock(), search=fake_search)))
    assert [e for e, _ in events] == ["step", "parts", "answer"]
    assert events[1][1]["sub_questions"] == ["who?"]
    assert events[-1][1]["result"] == RESULT
    assert all("t" in d for _, d in events)


async def test_a_tool_error_becomes_an_error_event():
    async def failing(config, query, models=None, on_event=None):
        return {"error": {"code": "budget_exhausted", "message": "spent"}}
    events = parse(await collect(ask_events(None, "q", asyncio.Lock(), search=failing)))
    assert events == [("error", {"t": events[0][1]["t"],
                                 "error": {"code": "budget_exhausted", "message": "spent"}})]


async def test_a_second_question_while_one_runs_is_busy():
    lock = asyncio.Lock()
    await lock.acquire()
    events = parse(await collect(ask_events(None, "q", lock, search=fake_search)))
    assert events[0][0] == "error" and events[0][1]["error"]["code"] == "busy"


async def test_closing_the_stream_cancels_the_run():
    calls_after = []

    async def slow(config, query, models=None, on_event=None):
        on_event({"type": "step", "name": "split", "state": "start", "part": None, "detail": None})
        try:
            await asyncio.sleep(10)          # stands in for a Gemini call
            calls_after.append("spent")      # must never run
        except asyncio.CancelledError:
            calls_after.append("cancelled")
            raise
        return RESULT

    lock = asyncio.Lock()
    gen = ask_events(None, "q", lock, search=slow)
    first = await gen.__anext__()
    assert b"event: step" in first
    await gen.aclose()                       # what Starlette does on disconnect
    await asyncio.sleep(0)
    assert calls_after == ["cancelled"]
    assert not lock.locked()


async def test_events_with_infinite_scores_are_valid_json():
    async def search(config, query, models=None, on_event=None):
        # emit() makes payloads safe; this checks the web layer keeps them so.
        from lore.query.events import emit
        from lore.query.models import SeedEntity
        emit(on_event, "seeds", part=0, entities=[SeedEntity("e1", "H", "Character", ("b1",), math.inf)])
        return RESULT
    events = parse(await collect(ask_events(None, "q", asyncio.Lock(), search=search)))
    assert events[0][1]["entities"][0]["score"] is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    config = build_store_triple(tmp_path, books=[("b1", "A Study")])
    monkeypatch.setattr("lore.web.app.tools.lore_search", fake_search)
    return TestClient(create_app(config), base_url="http://127.0.0.1:8766")


def test_the_endpoint_streams_server_sent_events(client):
    with client.stream("POST", "/api/ask", json={"question": "who?"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes())
    assert [e for e, _ in parse([body])] == ["step", "parts", "answer"]


@pytest.mark.parametrize("question", ["", "   ", "x" * 2001])
def test_bad_questions_are_400(client, question):
    assert client.post("/api/ask", json={"question": question}).status_code == 400


def test_a_form_post_is_refused(client):
    r = client.post("/api/ask", data={"question": "who?"})
    assert r.status_code in (400, 415, 422)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_web_ask.py`
Expected: FAIL with `ModuleNotFoundError: lore.web.ask`.

- [ ] **Step 3: Implement `lore/web/ask.py`**

```python
"""The stream behind POST /api/ask.

lore_search runs as a task; its progress events go through a queue to the
response as server-sent events, each stamped with seconds since the start.
One question at a time (Gemini's 15 RPM makes parallel asks pointless):
a request while one runs gets a `busy` error event. If the client goes
away, Starlette closes this generator, and the `finally` cancels the run
so it stops spending Gemini requests.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import AsyncIterator

from lore.mcp import tools

BUSY = {"code": "busy", "message": "LORE is still answering the previous question."}


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, allow_nan=False)}\n\n".encode()


async def ask_events(config, question: str, lock: asyncio.Lock,
                     search=None) -> AsyncIterator[bytes]:
    search = search or tools.lore_search
    started = time.monotonic()

    def stamp(payload: dict) -> dict:
        return {"t": round(time.monotonic() - started, 2), **payload}

    if lock.locked():
        yield sse("error", stamp({"error": BUSY}))
        return
    async with lock:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        task = asyncio.create_task(search(config, question, on_event=queue.put_nowait))
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, task},
                                             return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    event = getter.result()
                    yield sse(event.pop("type"), stamp(event))
                    continue
                getter.cancel()
                break
            while not queue.empty():
                event = queue.get_nowait()
                yield sse(event.pop("type"), stamp(event))
            result = task.result()
            if isinstance(result, dict) and "error" in result:
                yield sse("error", stamp({"error": result["error"]}))
            else:
                yield sse("answer", stamp({"result": result}))
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
```

- [ ] **Step 4: Add the route**

In `lore/web/app.py`:
- imports: `import asyncio`, `from fastapi import Request`, `from fastapi.responses import StreamingResponse`, `from pydantic import BaseModel, field_validator`, `from lore.web.ask import ask_events`;
- module level:

```python
MAX_QUESTION_CHARS = 2000


class AskBody(BaseModel):
    question: str

    @field_validator("question")
    @classmethod
    def _sized(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > MAX_QUESTION_CHARS:
            raise ValueError(f"a question is 1-{MAX_QUESTION_CHARS} characters")
        return value
```

- inside `create_app`, after the other routes and **before** the static mount:

```python
    lock = asyncio.Lock()

    @api.post("/api/ask")
    async def ask(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "send JSON")
        try:
            body = AskBody.model_validate(await request.json())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return StreamingResponse(
            ask_events(config, body.question, lock, search=tools.lore_search),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
```

Passing `search=tools.lore_search` looks the attribute up at request time, so the test's monkeypatch of `lore.web.app.tools.lore_search` takes effect. `pydantic.ValidationError` subclasses `ValueError`, so the `except` catches it.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest -q tests/test_web_ask.py tests/test_web_app.py tests/test_web_security.py`
Expected: PASS.

- [ ] **Step 6: Add the fixture recorder**

Create `scripts/record_ask.py`:

```python
"""Record one real /api/ask event stream as a frontend test fixture.

    .venv/bin/python scripts/record_ask.py "How is Jefferson Hope connected to John Ferrier?" \
        web/src/fixtures/single-part.json

Spends about six Gemini requests. Needs Ollama running (CPU-only):
    CUDA_VISIBLE_DEVICES=-1 OLLAMA_VULKAN=0 ~/.local/ollama/bin/ollama serve
Writes [{"type": <event>, ...data}, ...]: the same objects the frontend's
stream reader produces.
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lore.config import LoreConfig  # noqa: E402
from lore.web.ask import ask_events  # noqa: E402


async def main(question: str, out: Path) -> None:
    config = LoreConfig.load(ROOT / "lore.toml")
    events = []
    async for chunk in ask_events(config, question, asyncio.Lock()):
        block = chunk.decode().strip()
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append({"type": lines["event"], **json.loads(lines["data"])})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(events, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(events)} events -> {out}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], Path(sys.argv[2])))
```

- [ ] **Step 7: Commit**

```bash
git config user.email
git add lore/web/ask.py lore/web/app.py scripts/record_ask.py tests/test_web_ask.py
git commit -m "feat: POST /api/ask streams live events, one question at a time

Each agent event goes out as an SSE event stamped with elapsed seconds,
then the full lore_search result. A second question while one runs gets
busy; closing the stream cancels the run so it stops spending Gemini."
```

---

### Task 6: `lore ui` command and packaging

**Files:**
- Modify: `lore/cli.py` (new command after `mcp_command`)
- Modify: `pyproject.toml` (hatch artifacts)
- Test: `tests/test_cli_ui.py`

**Interfaces:**
- Consumes: `create_app`, `check_loopback` (Tasks 3-5).
- Produces: `lore ui [--host 127.0.0.1] [--port 8766] [--dev]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_ui.py`:

```python
from typer.testing import CliRunner

from lore.cli import app


def test_ui_refuses_a_non_loopback_host():
    result = CliRunner().invoke(app, ["ui", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "loopback" in result.output


def test_ui_starts_uvicorn_on_loopback(monkeypatch):
    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, **kw: seen.update(host=host, port=port))
    result = CliRunner().invoke(app, ["ui"])
    assert result.exit_code == 0, result.output
    assert seen == {"host": "127.0.0.1", "port": 8766}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest -q tests/test_cli_ui.py`
Expected: FAIL (no such command `ui`).

- [ ] **Step 3: Implement**

In `lore/cli.py`, after `mcp_command`:

```python
@app.command(name="ui")
def ui_command(
    host: str = typer.Option(
        "127.0.0.1",
        help="bind address; loopback only (127.0.0.0/8, localhost, ::1)"),
    port: int | None = typer.Option(None, help="port (default: [lore.web] port, 8766)"),
    dev: bool = typer.Option(False, "--dev",
                             help="also accept the Vite dev server's origin (port 5173)"),
) -> None:
    """Serve LORE's web app: ask questions, read cited answers, explore the graph.

    Read-only on your books, graph and vectors; asking a question spends
    Gemini requests like `lore ask`. Loopback only: there is no
    authentication.
    """
    import uvicorn

    from lore.web.app import create_app
    from lore.web.security import NonLoopbackHost, check_loopback

    config = LoreConfig.load()
    port = port or config.web.port
    try:
        check_loopback(host)
    except NonLoopbackHost as exc:
        typer.echo(f"lore ui: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(f"lore ui: http://{host}:{port}", err=True)
    uvicorn.run(create_app(config, host=host, port=port, dev=dev),
                host=host, port=port, log_level="warning")
```

In `pyproject.toml`, add:

```toml
[tool.hatch.build.targets.wheel]
artifacts = ["lore/web/static/**"]
```

If a `[tool.hatch.build.targets.wheel]` table already exists, add the `artifacts` key to it instead.

- [ ] **Step 4: Run the tests, then the whole suite**

Run: `.venv/bin/python -m pytest -q tests/test_cli_ui.py` (PASS), then `.venv/bin/python -m pytest -q tests` (all PASS; ~12 minutes).

- [ ] **Step 5: Manual check**

Run `.venv/bin/lore ui` and open http://127.0.0.1:8766. Expected: the "not built yet" page. `curl -s http://127.0.0.1:8766/api/library | head -c 200` shows the books JSON. Stop the server with Ctrl+C.

- [ ] **Step 6: Commit**

```bash
git config user.email
git add lore/cli.py pyproject.toml tests/test_cli_ui.py
git commit -m "feat: lore ui serves the web app on 127.0.0.1:8766"
git push -q origin phase-5a-web-ui
```

---

### Task 7: Frontend scaffold, theme and build into the package

**Files:**
- Create: `web/package.json`, `web/tsconfig.json`, `web/vite.config.ts`, `web/index.html`, `web/src/main.tsx`, `web/src/theme.css`, `web/src/App.tsx` (placeholder shell), `web/src/App.test.tsx`, `web/src/setupTests.ts`

**Interfaces:**
- Produces:
  - `npm run dev` (Vite on 5173, proxying `/api` to 8766)
  - `npm run build` (writes `../lore/web/static`)
  - `npm test` (Vitest, jsdom)
  - CSS tokens used by every component: `--bg --panel --sunk --ink --muted --line --accent --accent-soft --glow --glow-soft --ok --warn --danger --k-character --k-place --k-faction --k-object --k-other --part-0 --part-1 --part-2 --part-3 --edge --edge-faint --f-ui --f-book --f-mono`.

- [ ] **Step 1: Create the project files**

`web/package.json`:

```json
{
  "name": "lore-web",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "test": "vitest run",
    "e2e": "playwright test"
  },
  "dependencies": {
    "@fontsource/jetbrains-mono": "^5.1.0",
    "@fontsource/newsreader": "^5.1.0",
    "@fontsource/public-sans": "^5.1.0",
    "graphology": "^0.25.4",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "sigma": "^3.0.0"
  },
  "devDependencies": {
    "@playwright/test": "^1.48.0",
    "@testing-library/jest-dom": "^6.6.0",
    "@testing-library/react": "^16.0.1",
    "@testing-library/user-event": "^14.5.2",
    "@types/react": "^18.3.11",
    "@types/react-dom": "^18.3.1",
    "@vitejs/plugin-react": "^4.3.2",
    "jsdom": "^25.0.1",
    "typescript": "^5.6.3",
    "vite": "^5.4.9",
    "vitest": "^2.1.3"
  }
}
```

`web/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "skipLibCheck": true,
    "types": ["vitest/globals", "@testing-library/jest-dom"]
  },
  "include": ["src", "e2e"]
}
```

`web/vite.config.ts`:

```ts
/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "../lore/web/static", emptyOutDir: true },
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8766" } },
  test: { environment: "jsdom", globals: true, setupFiles: ["src/setupTests.ts"], exclude: ["e2e/**", "node_modules/**"] },
});
```

`emptyOutDir` deletes `lore/web/static/.gitkeep` on build. That's fine: git still tracks it, and `git status` shows it deleted only until you run `git checkout lore/web/static/.gitkeep`. To avoid that noise, copy it back after the build with a `postbuild` script: add `"postbuild": "node -e \"require('fs').writeFileSync('../lore/web/static/.gitkeep','')\""` to `scripts`.

`web/index.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <title>LORE Reader</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

`web/src/setupTests.ts`:

```ts
import "@testing-library/jest-dom/vitest";
```

`web/src/main.tsx`:

```tsx
import "@fontsource/newsreader/400.css";
import "@fontsource/newsreader/600.css";
import "@fontsource/newsreader/400-italic.css";
import "@fontsource/public-sans/400.css";
import "@fontsource/public-sans/500.css";
import "@fontsource/public-sans/600.css";
import "@fontsource/jetbrains-mono/400.css";
import "./theme.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
```

`web/src/theme.css` holds the tokens and base styles from the mock's `<style>` block (`docs/superpowers/specs/assets/2026-10-07-phase-5a-mock.html`). Copy its `:root` block, both dark blocks, and every component rule. Then make these changes:
- add `--danger:#B3261E` (light) and `#F2867E` (dark);
- add the part colours: light `--part-0:#1E5AA0; --part-1:#8A4FA3; --part-2:#2E7D4F; --part-3:#B0662A`, dark `--part-0:#78AEEA; --part-1:#BC8AD3; --part-2:#6CC596; --part-3:#E19A5C`;
- font stacks: `--f-ui:"Public Sans",system-ui,-apple-system,"Segoe UI",sans-serif; --f-book:"Newsreader",Georgia,serif; --f-mono:"JetBrains Mono",ui-monospace,Menlo,monospace;`;
- `html,body,#root{height:100%}`.

Every class name the mock uses (`.app`, `.pane`, `.steps`, `.claim`, `.cite`, `.passage`, `.menu`, `.booklist`, `.snip`, `.overlay`, `.dialog`, `.card`, ...) keeps its name, and the components below reuse those classes.

`web/src/App.tsx` (placeholder until Task 13):

```tsx
export default function App() {
  return <div className="app"><header><span className="mark">LORE<small>Reader</small></span></header></div>;
}
```

`web/src/App.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import App from "./App";

test("renders the LORE mark", () => {
  render(<App />);
  expect(screen.getByText("LORE")).toBeInTheDocument();
});
```

- [ ] **Step 2: Install and run the test**

Run: `cd web && npm install && npm test`
Expected: 1 test PASS. `npm install` writes `package-lock.json`. Commit it.

- [ ] **Step 3: Build into the package and check it is served**

Run: `cd web && npm run build`, then `.venv/bin/lore ui` from the worktree root, and open http://127.0.0.1:8766.
Expected: a page titled "LORE Reader" with the LORE mark, and fonts loading from `/assets/` (check the Network tab: no external hosts). Stop the server.

- [ ] **Step 4: Commit**

```bash
git config user.email
git add web/package.json web/package-lock.json web/tsconfig.json web/vite.config.ts web/index.html web/src/
git commit -m "feat: web app scaffold (React, TypeScript, Vite) building into lore/web/static"
```

---

### Task 8: Types, API client and the SSE stream reader

**Files:**
- Create: `web/src/types.ts`, `web/src/api.ts`, `web/src/api.test.ts`

**Interfaces:**
- Consumes: the wire formats from Tasks 4 and 5.
- Produces (exact names other tasks use):

```ts
// types.ts
export type StepName = "split" | "seeds" | "search" | "rank" | "write" | "check";
export const STEP_ORDER: StepName[] = ["split", "seeds", "search", "rank", "write", "check"];
export interface Seed { id: string; name: string; kind: string; books: string[]; score: number | null }
export interface Edge { subject: string; predicate: string; predicate_raw: string; object: string;
  evidence: string; confidence: number; book_id: string; chapter_seq: number; chunk_id: string;
  subject_id: string; object_id: string }
export interface Claim { text: string; passage: number; quote: string }
export interface Passage { n: number; chunk_id: string; book_id: string; book_title: string | null;
  chapter_seq: number; text: string }
export interface SearchResult { answer: string; mode: string; template: string | null; note: string | null;
  model: string | null; selection_model: string | null; sub_questions: string[]; claims: Claim[];
  passages: Passage[]; edges: Edge[]; seeds: Seed[] }
export interface ApiError { code: string; message: string }
export type AskEvent =
  | { type: "step"; t: number; name: StepName; state: "start" | "done"; part: number | null; detail: string | null }
  | { type: "parts"; t: number; sub_questions: string[] }
  | { type: "seeds"; t: number; part: number; entities: Seed[] }
  | { type: "edges"; t: number; part: number; template: string | null; total: number; edges: Edge[] }
  | { type: "evidence"; t: number; chunk_ids: string[]; edge_keys: string[] }
  | { type: "answer"; t: number; result: SearchResult }
  | { type: "error"; t: number; error: ApiError };
export interface Book { id: string; title: string; author: string | null; status: string;
  chunk_count: number; entity_count: number | null }
export interface QuotaModel { model: string; count: number; limit: number; remaining: number }
export interface Quota { provider: "gemini" | "local"; models: QuotaModel[]; resets_at: string }
export interface McpInfo { stdio_command: string; http_command: string; http_url: string; http_running: boolean }
export interface EntityCardData { id: string; name: string; kind: string; aliases: string[]; books: string[];
  edges_out: Edge[]; edges_in: Edge[]; edges_out_total: number; edges_in_total: number; truncated: boolean }
export interface SubgraphNode { id: string; name: string; kind: string; books: string[] }
export interface SubgraphLink { subject: string; subject_id: string; object: string; object_id: string;
  count: number; predicates: Record<string, number> }
export interface SubgraphData { nodes: SubgraphNode[]; edges: SubgraphLink[]; truncated: boolean; depth: number }
export const edgeKey = (e: Edge) => `${e.subject_id}|${e.predicate}|${e.object_id}|${e.chunk_id}`;

// api.ts
export function parseSse(buffer: string): { events: AskEvent[]; rest: string };
export async function* streamAsk(question: string, signal: AbortSignal): AsyncGenerator<AskEvent>;
export function getJson<T>(path: string): Promise<T | { error: ApiError }>;
export const api: { library(): ...; entity(id: string): ...; subgraph(id: string): ...; quota(): ...; mcp(): ...; settings(): Promise<{ show_internals: boolean }> };
```

Read `lore/mcp/shapes.py` (`LinkOut`) and adjust `SubgraphLink` to its exact field names before writing (e.g. whether `predicates` is a dict or a list).

- [ ] **Step 1: Write the failing tests**

`web/src/api.test.ts`:

```ts
import { parseSse, streamAsk } from "./api";

const frame = (event: string, data: object) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

test("parses complete frames and keeps the partial tail", () => {
  const text = frame("parts", { t: 0.1, sub_questions: ["a"] }) + "event: step\ndata: {\"t\":";
  const { events, rest } = parseSse(text);
  expect(events).toEqual([{ type: "parts", t: 0.1, sub_questions: ["a"] }]);
  expect(rest).toBe("event: step\ndata: {\"t\":");
});

test("streamAsk yields events across chunk boundaries", async () => {
  const body = frame("parts", { t: 0, sub_questions: ["a"] }) + frame("answer", { t: 1, result: { answer: "x" } });
  const bytes = new TextEncoder().encode(body);
  const chunks = [bytes.slice(0, 17), bytes.slice(17, 60), bytes.slice(60)];
  const stream = new ReadableStream({ start(c) { chunks.forEach((x) => c.enqueue(x)); c.close(); } });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } })));
  const out = [];
  for await (const e of streamAsk("who?", new AbortController().signal)) out.push(e.type);
  expect(out).toEqual(["parts", "answer"]);
  const [, init] = (fetch as any).mock.calls[0];
  expect(init.method).toBe("POST");
  expect(init.headers["Content-Type"]).toBe("application/json");
});

test("a 400 becomes an error event", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "a question is 1-2000 characters" }), { status: 400 })));
  const out = [];
  for await (const e of streamAsk("", new AbortController().signal)) out.push(e);
  expect(out).toEqual([{ type: "error", t: 0, error: { code: "bad_request", message: "a question is 1-2000 characters" } }]);
});

test("a dropped connection ends with an interrupted error", async () => {
  const stream = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(frame("parts", { t: 0, sub_questions: [] }))); c.error(new TypeError("network")); } });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(stream, { status: 200 })));
  const out = [];
  for await (const e of streamAsk("q", new AbortController().signal)) out.push(e.type === "error" ? e.error.code : e.type);
  expect(out).toEqual(["parts", "interrupted"]);
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/api.test.ts`. Expected: FAIL (module not found).

- [ ] **Step 3: Implement `types.ts`** with exactly the declarations in the Interfaces block above.

- [ ] **Step 4: Implement `api.ts`**

```ts
import type { ApiError, AskEvent, Book, EntityCardData, McpInfo, Quota, SubgraphData } from "./types";

export function parseSse(buffer: string): { events: AskEvent[]; rest: string } {
  const events: AskEvent[] = [];
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let type = "message";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) type = line.slice(7);
      else if (line.startsWith("data: ")) data += line.slice(6);
    }
    if (data) events.push({ type, ...JSON.parse(data) } as AskEvent);
  }
  return { events, rest };
}

const errorEvent = (code: string, message: string): AskEvent => ({ type: "error", t: 0, error: { code, message } });

export async function* streamAsk(question: string, signal: AbortSignal): AsyncGenerator<AskEvent> {
  let response: Response;
  try {
    response = await fetch("/api/ask", {
      method: "POST", signal,
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ question }),
    });
  } catch {
    if (signal.aborted) return;
    yield errorEvent("unreachable", "");
    return;
  }
  if (!response.ok || !response.body) {
    let message = "";
    try { message = (await response.json()).detail ?? ""; } catch { /* not JSON */ }
    yield errorEvent(response.status === 403 ? "forbidden" : "bad_request", String(message));
    return;
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let finished = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      const parsed = parseSse(buffer + value);
      buffer = parsed.rest;
      for (const e of parsed.events) {
        if (e.type === "answer" || e.type === "error") finished = true;
        yield e;
      }
    }
  } catch {
    if (signal.aborted) return;
  }
  if (!finished && !signal.aborted) yield errorEvent("interrupted", "");
}

export async function getJson<T>(path: string): Promise<T | { error: ApiError }> {
  try {
    const r = await fetch(path, { headers: { Accept: "application/json" } });
    return (await r.json()) as T | { error: ApiError };
  } catch {
    return { error: { code: "unreachable", message: "" } };
  }
}

export const isError = (x: unknown): x is { error: ApiError } =>
  typeof x === "object" && x !== null && "error" in x;

export const api = {
  library: () => getJson<{ books: Book[]; note: string | null }>("/api/library"),
  entity: (id: string) => getJson<EntityCardData>(`/api/entity?id=${encodeURIComponent(id)}`),
  subgraph: (id: string) => getJson<SubgraphData>(`/api/subgraph?id=${encodeURIComponent(id)}&depth=1`),
  quota: () => getJson<Quota>("/api/quota"),
  mcp: () => getJson<McpInfo>("/api/mcp"),
  settings: () => getJson<{ show_internals: boolean }>("/api/settings"),
};
```

- [ ] **Step 5: Run the tests.** Run: `cd web && npx vitest run src/api.test.ts`. Expected: 4 PASS.

- [ ] **Step 6: Commit**

```bash
git config user.email
git add web/src/types.ts web/src/api.ts web/src/api.test.ts
git commit -m "feat: web types, API client and a streaming SSE reader for POST /api/ask"
```

---

### Task 9: The ask reducer, fixtures, and history

**Files:**
- Create: `web/src/askReducer.ts`, `web/src/askReducer.test.ts`
- Create: `web/src/history.ts`, `web/src/history.test.ts`
- Create: `web/src/fixtures/single-part.json`, `web/src/fixtures/two-part.json` (recorded), `web/src/fixtures/synthetic.ts`

**Interfaces:**
- Consumes: `AskEvent`, `STEP_ORDER`, `SearchResult` (Task 8).
- Produces:

```ts
// askReducer.ts
export type StepState = "pending" | "running" | "done" | "skipped";
export interface StepView { name: StepName; state: StepState; detail: string | null; startedAt: number | null; doneAt: number | null }
export interface PartEdges { template: string | null; total: number; edges: Edge[] }
export interface AskState {
  status: "idle" | "running" | "done" | "error" | "interrupted";
  question: string; steps: StepView[]; subQuestions: string[];
  seeds: Seed[][]; edges: PartEdges[]; evidence: { chunkIds: string[]; edgeKeys: string[] } | null;
  result: SearchResult | null; error: ApiError | null; elapsed: number;
}
export function initialState(question?: string): AskState;
export function askReducer(state: AskState, event: AskEvent): AskState;
// history.ts
export interface HistoryItem { id: string; question: string; events: AskEvent[]; answer: SearchResult | null; askedAt: string }
export function loadHistory(storage?: Storage | null): HistoryItem[];
export function saveHistory(items: HistoryItem[], storage?: Storage | null): boolean;
export function addToHistory(items: HistoryItem[], item: HistoryItem): HistoryItem[];  // newest first, max 50
export const HISTORY_KEY = "lore-history-v1";
```

Reducer rules:
- `step start`: that step becomes `running` (keep the first `startedAt`). Every earlier step in `STEP_ORDER` still `running` or `pending` becomes `done`.
- `step done`: for part-tagged steps (`seeds`, `search`), mark `done` only when `part === subQuestions.length - 1` or `subQuestions` is empty. Keep the latest `detail`.
- `edges`: append to `edges[part]` and set `total` (the scheduler may split one event into several appends).
- `answer`: `status:"done"`, `result` set, every `pending`/`running` step becomes `skipped`/`done` respectively, and if no `parts` event came, `subQuestions = result.sub_questions`.
- `error` with code `interrupted`: `status:"interrupted"`. Any other error: `status:"error"`. Running steps become `pending` again.
- `elapsed`: the last event's `t`.

- [ ] **Step 1: Record the two fixtures (spends ~12 Gemini requests; needs Ollama running CPU-only)**

```bash
.venv/bin/python scripts/record_ask.py "How is Jefferson Hope connected to John Ferrier?" web/src/fixtures/single-part.json
.venv/bin/python scripts/record_ask.py "Where was Joseph Stangerson's body found, and how did he come to be killed?" web/src/fixtures/two-part.json
```

Expected: each prints `N events -> ...` and ends with an `answer` event. If the Gemini budget is spent, wait for the reset (midnight Pacific). Do not hand-write these two.

`web/src/fixtures/synthetic.ts`, the failure streams:

```ts
import type { AskEvent } from "../types";
const step = (t: number, name: any, state: any, part: number | null = null, detail: string | null = null): AskEvent =>
  ({ type: "step", t, name, state, part, detail });
export const interrupted: AskEvent[] = [step(0, "split", "start"), { type: "parts", t: 0.4, sub_questions: ["q"] },
  step(0.4, "split", "done", null, "1 part"), step(0.5, "seeds", "start", 0), { type: "error", t: 0, error: { code: "interrupted", message: "" } }];
export const budget: AskEvent[] = [{ type: "error", t: 0, error: { code: "budget_exhausted", message: "spent" } }];
export const empty: AskEvent[] = [{ type: "answer", t: 1.2, result: { answer: "The loaded books do not contain enough to answer that.", mode: "empty",
  template: null, note: null, model: null, selection_model: null, sub_questions: [], claims: [], passages: [], edges: [], seeds: [] } }];
```

- [ ] **Step 2: Write the failing tests**

`web/src/askReducer.test.ts`:

```ts
import single from "./fixtures/single-part.json";
import two from "./fixtures/two-part.json";
import { budget, empty, interrupted } from "./fixtures/synthetic";
import { askReducer, initialState } from "./askReducer";
import type { AskEvent } from "./types";

const run = (events: AskEvent[]) => events.reduce(askReducer, initialState("q"));

test("a recorded single-part answer ends done with every step settled", () => {
  const s = run(single as AskEvent[]);
  expect(s.status).toBe("done");
  expect(s.result?.claims.length).toBeGreaterThan(0);
  expect(s.steps.every((x) => x.state === "done" || x.state === "skipped")).toBe(true);
  expect(s.seeds[0].length).toBeGreaterThan(0);
});

test("a two-part answer keeps seeds and edges per part", () => {
  const s = run(two as AskEvent[]);
  expect(s.subQuestions).toHaveLength(2);
  expect(s.seeds).toHaveLength(2);
  expect(s.edges.length).toBeGreaterThanOrEqual(1);
  expect(s.evidence?.chunkIds.length).toBeGreaterThan(0);
});

test("seeds is not done until the last part reports", () => {
  let s = initialState("q");
  s = askReducer(s, { type: "parts", t: 0, sub_questions: ["a", "b"] });
  s = askReducer(s, { type: "step", t: 0, name: "seeds", state: "start", part: 0, detail: null });
  s = askReducer(s, { type: "step", t: 0, name: "seeds", state: "done", part: 0, detail: "3 found" });
  expect(s.steps.find((x) => x.name === "seeds")!.state).toBe("running");
  s = askReducer(s, { type: "step", t: 0, name: "seeds", state: "done", part: 1, detail: "2 found" });
  expect(s.steps.find((x) => x.name === "seeds")!.state).toBe("done");
});

test("split edges events append", () => {
  const edge = (c: string) => ({ subject: "a", predicate: "P", predicate_raw: "p", object: "b", evidence: "", confidence: 1,
    book_id: "b1", chapter_seq: 1, chunk_id: c, subject_id: "a", object_id: "b" });
  let s = initialState("q");
  s = askReducer(s, { type: "edges", t: 0, part: 0, template: "neighbours", total: 3, edges: [edge("1"), edge("2")] });
  s = askReducer(s, { type: "edges", t: 0, part: 0, template: "neighbours", total: 3, edges: [edge("3")] });
  expect(s.edges[0].edges.map((e) => e.chunk_id)).toEqual(["1", "2", "3"]);
});

test("an interrupted stream keeps what arrived", () => {
  const s = run(interrupted);
  expect(s.status).toBe("interrupted");
  expect(s.subQuestions).toEqual(["q"]);
  expect(s.steps.find((x) => x.name === "split")!.state).toBe("done");
});

test("errors and empty answers", () => {
  expect(run(budget).error?.code).toBe("budget_exhausted");
  const e = run(empty);
  expect(e.status).toBe("done");
  expect(e.steps.every((x) => x.state === "skipped")).toBe(true);
});
```

`web/src/history.test.ts`:

```ts
import { addToHistory, HISTORY_KEY, loadHistory, saveHistory, type HistoryItem } from "./history";

const item = (id: string): HistoryItem => ({ id, question: id, events: [], answer: null, askedAt: "2026-10-07T00:00:00Z" });

class MemoryStorage { m = new Map<string, string>(); getItem(k: string) { return this.m.get(k) ?? null; }
  setItem(k: string, v: string) { this.m.set(k, v); } removeItem(k: string) { this.m.delete(k); } clear() { this.m.clear(); }
  key() { return null; } get length() { return this.m.size; } }

test("round-trips newest first and caps at 50", () => {
  const s = new MemoryStorage() as unknown as Storage;
  let items: HistoryItem[] = [];
  for (let i = 0; i < 55; i++) items = addToHistory(items, item(String(i)));
  expect(saveHistory(items, s)).toBe(true);
  const back = loadHistory(s);
  expect(back).toHaveLength(50);
  expect(back[0].id).toBe("54");
});

test("survives a throwing storage", () => {
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("full"); } } as unknown as Storage;
  expect(loadHistory(broken)).toEqual([]);
  expect(saveHistory([item("a")], broken)).toBe(false);
});

test("ignores corrupt data", () => {
  const s = new MemoryStorage() as unknown as Storage;
  s.setItem(HISTORY_KEY, "{not json");
  expect(loadHistory(s)).toEqual([]);
  expect(loadHistory(null)).toEqual([]);
});
```

- [ ] **Step 3: Run to verify failure.** `cd web && npx vitest run src/askReducer.test.ts src/history.test.ts`. Expected: FAIL.

- [ ] **Step 4: Implement `askReducer.ts`**

```ts
import { STEP_ORDER, type ApiError, type AskEvent, type Edge, type SearchResult, type Seed, type StepName } from "./types";

export type StepState = "pending" | "running" | "done" | "skipped";
export interface StepView { name: StepName; state: StepState; detail: string | null; startedAt: number | null; doneAt: number | null }
export interface PartEdges { template: string | null; total: number; edges: Edge[] }
export interface AskState {
  status: "idle" | "running" | "done" | "error" | "interrupted";
  question: string; steps: StepView[]; subQuestions: string[];
  seeds: Seed[][]; edges: PartEdges[]; evidence: { chunkIds: string[]; edgeKeys: string[] } | null;
  result: SearchResult | null; error: ApiError | null; elapsed: number;
}

export function initialState(question = ""): AskState {
  return {
    status: question ? "running" : "idle", question,
    steps: STEP_ORDER.map((name) => ({ name, state: "pending", detail: null, startedAt: null, doneAt: null })),
    subQuestions: [], seeds: [], edges: [], evidence: null, result: null, error: null, elapsed: 0,
  };
}

const PART_STEPS: StepName[] = ["seeds", "search"];

function setAt<T>(list: T[], index: number, value: T): T[] {
  const copy = list.slice();
  copy[index] = value;
  return copy;
}

export function askReducer(state: AskState, event: AskEvent): AskState {
  const s: AskState = { ...state, elapsed: Math.max(state.elapsed, event.t ?? 0) };
  switch (event.type) {
    case "step": {
      const i = STEP_ORDER.indexOf(event.name);
      const steps = s.steps.map((step, j) => {
        if (event.state === "start") {
          if (j === i) return { ...step, state: "running" as const, startedAt: step.startedAt ?? event.t };
          if (j < i && (step.state === "running" || step.state === "pending")) return { ...step, state: "done" as const, doneAt: event.t };
          return step;
        }
        if (j !== i) return step;
        const lastPart = !PART_STEPS.includes(event.name) || s.subQuestions.length === 0
          || event.part === s.subQuestions.length - 1;
        return { ...step, detail: event.detail ?? step.detail, state: lastPart ? ("done" as const) : step.state,
          doneAt: lastPart ? event.t : step.doneAt };
      });
      return { ...s, steps };
    }
    case "parts":
      return { ...s, subQuestions: event.sub_questions };
    case "seeds":
      return { ...s, seeds: setAt(s.seeds, event.part, [...(s.seeds[event.part] ?? []), ...event.entities]) };
    case "edges": {
      const prev = s.edges[event.part] ?? { template: event.template, total: 0, edges: [] };
      return { ...s, edges: setAt(s.edges, event.part, { template: event.template, total: event.total, edges: [...prev.edges, ...event.edges] }) };
    }
    case "evidence":
      return { ...s, evidence: { chunkIds: event.chunk_ids, edgeKeys: event.edge_keys } };
    case "answer":
      return {
        ...s, status: "done", result: event.result,
        subQuestions: s.subQuestions.length ? s.subQuestions : event.result.sub_questions,
        steps: s.steps.map((step) => step.state === "running" ? { ...step, state: "done", doneAt: event.t }
          : step.state === "pending" ? { ...step, state: "skipped" } : step),
      };
    case "error":
      return {
        ...s, status: event.error.code === "interrupted" ? "interrupted" : "error", error: event.error,
        steps: s.steps.map((step) => step.state === "running" ? { ...step, state: "pending" } : step),
      };
  }
}
```

- [ ] **Step 5: Implement `history.ts`**

```ts
import type { AskEvent, SearchResult } from "./types";

export interface HistoryItem { id: string; question: string; events: AskEvent[]; answer: SearchResult | null; askedAt: string }
export const HISTORY_KEY = "lore-history-v1";
const MAX_ITEMS = 50;

function defaultStorage(): Storage | null {
  try { return window.localStorage; } catch { return null; }
}

export function loadHistory(storage: Storage | null = defaultStorage()): HistoryItem[] {
  if (!storage) return [];
  try {
    const parsed = JSON.parse(storage.getItem(HISTORY_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.slice(0, MAX_ITEMS) : [];
  } catch {
    return [];
  }
}

export function saveHistory(items: HistoryItem[], storage: Storage | null = defaultStorage()): boolean {
  if (!storage) return false;
  try {
    storage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, MAX_ITEMS)));
    return true;
  } catch {
    return false;
  }
}

export function addToHistory(items: HistoryItem[], item: HistoryItem): HistoryItem[] {
  return [item, ...items.filter((x) => x.id !== item.id)].slice(0, MAX_ITEMS);
}
```

- [ ] **Step 6: Run the tests.** `cd web && npx vitest run src/askReducer.test.ts src/history.test.ts`. Expected: all PASS. If `resolveJsonModule` errors appear, add `"resolveJsonModule": true` to `tsconfig.json`.

- [ ] **Step 7: Commit**

```bash
git config user.email
git add web/src/askReducer.ts web/src/askReducer.test.ts web/src/history.ts web/src/history.test.ts web/src/fixtures/ web/tsconfig.json
git commit -m "feat: one reducer turns the ask event stream into state; history in browser storage

Fixtures are event streams recorded from real runs (single- and
two-part), plus synthetic interrupted, budget and empty streams."
```

---

### Task 10: Graph view derivation, layout and the stagger scheduler

**Files:**
- Create: `web/src/graphView.ts`, `web/src/graphView.test.ts`
- Create: `web/src/layout.ts`, `web/src/layout.test.ts`
- Create: `web/src/scheduler.ts`, `web/src/scheduler.test.ts`

**Interfaces:**
- Consumes: `AskState` (Task 9), `Edge`, `Seed`, `SubgraphData`, `edgeKey` (Task 8).
- Produces:

```ts
// graphView.ts
export type NodeState = "shown" | "dim";
export type LinkState = "shown" | "cut" | "cited" | "faint";
export interface ViewNode { id: string; label: string; kind: string; books: string[]; seedPart: number | null; state: NodeState; expanded: boolean }
export interface ViewLink { id: string; source: string; target: string; label: string; count: number; chunkIds: string[];
  part: number | null; state: LinkState; hot: boolean; books: string[] }
export interface GraphView { nodes: ViewNode[]; links: ViewLink[]; shownEdges: number; totalEdges: number }
export interface ViewOptions { hiddenBooks: Set<string>; hoverChunk: string | null; expansions: SubgraphData[] }
export function deriveGraph(state: AskState, options: ViewOptions): GraphView;
// layout.ts
export type Positions = Map<string, { x: number; y: number }>;
export function place(previous: Positions, view: GraphView, anchor?: string | null): Positions;
// scheduler.ts
export interface Scheduler { push(event: AskEvent): void; stop(): void }
export function createScheduler(dispatch: (e: AskEvent) => void, opts: { reduceMotion: boolean; replay?: boolean }): Scheduler;
export function splitForStagger(event: AskEvent, chunk: number): AskEvent[];
export const STAGGER_TOTAL_MS = 500;
```

Derivation rules (each one is pinned by a test):
- **Node id.** A node's id is the entity id (`subject_id`/`object_id`, or the seed `id`). If that is empty, the name.
- **Seeds.** Seeds from part *p* get `seedPart = p`, first part wins.
- **Links.** One link per directed (source, target) pair. Its `label` is the most frequent predicate (lowercase, underscores as spaces), with ` ×n` appended when `count > 1`. `chunkIds` are its rows' chunks. `part` is the first part that produced it.
- **Link state, once there is a result.** `cited` if any of its chunks is cited by a claim (resolve the claim's `passage` number to its `chunk_id` through `result.passages`). Otherwise `faint`.
- **Link state, before a result but after evidence.** `cut` if none of its rows' edge keys is in `evidence.edgeKeys`. Otherwise `shown`.
- **Link state, before evidence.** `shown`.
- **`hot`.** True when `hoverChunk` is in the link's `chunkIds`.
- **Book filter.** A node is `dim` when every one of its books is in `hiddenBooks`. A link is dimmed by giving it state `faint` when every one of its rows' books is hidden.
- **Expansions.** Nodes and links from each `SubgraphData` are added; they have `part: null`. The root node of an expansion gets `expanded: true`. An expansion link's state is `shown`, unless a search link already exists for the pair, in which case the search link wins.
- **Edge counts.** `shownEdges` is the sum of edges received; `totalEdges` is the sum of `total` over parts.

Layout rules:
- **Placed nodes never move.** Every id already in `previous` keeps its exact position.
- **New seed nodes** go on a circle of radius 220 around (500, 350). Seeds of part *p* sit at angles starting at `p * 0.6` rad, spaced 2π divided by the number of seeds.
- **Other new nodes** go near their first placed neighbour (or `anchor` for an expansion): at a distance of 140, at an angle from a stable hash of the id. Then 60 iterations of repulsion run among new nodes only, pushing them at least 70 apart from every node.
- **Determinism.** The same inputs always give the same positions. There is no randomness: use the hash.

Scheduler rules:
- **Live, with motion.** A `seeds` event with *n* entities is dispatched as *n* single-entity `seeds` events, spaced `STAGGER_TOTAL_MS / n` ms apart (minimum 15 ms). An `edges` event is split into chunks of `Math.ceil(edges.length / 12)` and spaced the same way. Other events wait until earlier staggered pieces are out, so order is preserved.
- **Reduced motion.** Every event is dispatched immediately, unsplit.
- **Replay** (from history). The gap between consecutive events' `t` is waited, capped at 600 ms. Stagger splitting applies as in live mode.
- **`stop()`** cancels pending timers.

- [ ] **Step 1: Write the failing tests**

`web/src/graphView.test.ts`:

```ts
import { deriveGraph } from "./graphView";
import { askReducer, initialState } from "./askReducer";
import two from "./fixtures/two-part.json";
import type { AskEvent, Edge } from "./types";

const opts = { hiddenBooks: new Set<string>(), hoverChunk: null, expansions: [] };
const edge = (s: string, o: string, chunk: string, book = "b1", p = "KILLS"): Edge => ({ subject: s.toUpperCase(), predicate: p,
  predicate_raw: p, object: o.toUpperCase(), evidence: "", confidence: 1, book_id: book, chapter_seq: 1, chunk_id: chunk, subject_id: s, object_id: o });

function stateWith(events: AskEvent[]) { return events.reduce(askReducer, initialState("q")); }
const base: AskEvent[] = [
  { type: "parts", t: 0, sub_questions: ["a", "b"] },
  { type: "seeds", t: 0, part: 0, entities: [{ id: "h", name: "Hope", kind: "Character", books: ["b1"], score: 0 }] },
  { type: "seeds", t: 0, part: 1, entities: [{ id: "h", name: "Hope", kind: "Character", books: ["b1"], score: 0 }, { id: "d", name: "Drebber", kind: "Character", books: ["b1"], score: 0 }] },
  { type: "edges", t: 0, part: 0, template: "neighbours", total: 400, edges: [edge("h", "d", "c1"), edge("h", "d", "c2", "b1", "OPPOSES"), edge("h", "x", "c3", "b2")] },
];

test("groups edges by pair with the top predicate and count", () => {
  const v = deriveGraph(stateWith(base), opts);
  const hd = v.links.find((l) => l.source === "h" && l.target === "d")!;
  expect(hd.count).toBe(2);
  expect(hd.label).toMatch(/×2$/);
  expect(v.totalEdges).toBe(400);
  expect(v.shownEdges).toBe(3);
});

test("seed colour follows the first part that found it", () => {
  const v = deriveGraph(stateWith(base), opts);
  expect(v.nodes.find((n) => n.id === "h")!.seedPart).toBe(0);
  expect(v.nodes.find((n) => n.id === "d")!.seedPart).toBe(1);
});

test("edges whose passage missed the evidence cut are cut", () => {
  const v = deriveGraph(stateWith([...base, { type: "evidence", t: 0, chunk_ids: ["c1"], edge_keys: ["h|KILLS|d|c1"] }]), opts);
  expect(v.links.find((l) => l.target === "d")!.state).toBe("shown");
  expect(v.links.find((l) => l.target === "x")!.state).toBe("cut");
});

test("after the answer, cited links glow and the rest fade; hover marks hot", () => {
  const result = { answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null, sub_questions: ["a", "b"],
    claims: [{ text: "t", passage: 1, quote: "q" }], passages: [{ n: 1, chunk_id: "c2", book_id: "b1", book_title: null, chapter_seq: 1, text: "" }],
    edges: [], seeds: [] };
  const s = stateWith([...base, { type: "answer", t: 1, result }]);
  const v = deriveGraph(s, { ...opts, hoverChunk: "c2" });
  const hd = v.links.find((l) => l.target === "d")!;
  expect(hd.state).toBe("cited");
  expect(hd.hot).toBe(true);
  expect(v.links.find((l) => l.target === "x")!.state).toBe("faint");
});

test("hiding a book dims its nodes and links", () => {
  const v = deriveGraph(stateWith(base), { ...opts, hiddenBooks: new Set(["b2"]) });
  expect(v.links.find((l) => l.target === "x")!.state).toBe("faint");
  expect(v.nodes.find((n) => n.id === "h")!.state).toBe("shown");
});

test("a recorded answer derives without throwing", () => {
  const v = deriveGraph((two as AskEvent[]).reduce(askReducer, initialState("q")), opts);
  expect(v.nodes.length).toBeGreaterThan(1);
  expect(v.links.some((l) => l.state === "cited")).toBe(true);
});
```

`web/src/layout.test.ts`:

```ts
import { place } from "./layout";
import type { GraphView } from "./graphView";

const node = (id: string, seedPart: number | null = null) => ({ id, label: id, kind: "Character", books: [], seedPart, state: "shown" as const, expanded: false });
const link = (s: string, t: string) => ({ id: `${s}->${t}`, source: s, target: t, label: "", count: 1, chunkIds: [], part: 0, state: "shown" as const, hot: false, books: [] });

test("placed nodes never move when the graph grows", () => {
  const v1: GraphView = { nodes: [node("a", 0), node("b")], links: [link("a", "b")], shownEdges: 1, totalEdges: 1 };
  const p1 = place(new Map(), v1);
  const v2: GraphView = { ...v1, nodes: [...v1.nodes, node("c")], links: [...v1.links, link("b", "c")] };
  const p2 = place(p1, v2);
  expect(p2.get("a")).toEqual(p1.get("a"));
  expect(p2.get("b")).toEqual(p1.get("b"));
  expect(p2.has("c")).toBe(true);
});

test("deterministic and spread out", () => {
  const v: GraphView = { nodes: ["a", "b", "c", "d"].map((id, i) => node(id, i === 0 ? 0 : null)),
    links: [link("a", "b"), link("a", "c"), link("a", "d")], shownEdges: 3, totalEdges: 3 };
  const p = place(new Map(), v);
  expect(place(new Map(), v)).toEqual(p);
  const pts = [...p.values()];
  for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++)
    expect(Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y)).toBeGreaterThanOrEqual(60);
});
```

`web/src/scheduler.test.ts`:

```ts
import { createScheduler, splitForStagger } from "./scheduler";
import type { AskEvent } from "./types";

const seeds: AskEvent = { type: "seeds", t: 0, part: 0, entities: [1, 2, 3].map((i) => ({ id: String(i), name: String(i), kind: "Character", books: [], score: 0 })) };

test("splits seeds one per entity", () => {
  expect(splitForStagger(seeds, 1)).toHaveLength(3);
});

test("reduced motion dispatches everything at once, unsplit", () => {
  const out: AskEvent[] = [];
  const s = createScheduler((e) => out.push(e), { reduceMotion: true });
  s.push(seeds); s.push({ type: "parts", t: 0, sub_questions: [] });
  expect(out.map((e) => e.type)).toEqual(["seeds", "parts"]);
});

test("staggered pieces keep event order", async () => {
  vi.useFakeTimers();
  const out: string[] = [];
  const s = createScheduler((e) => out.push(e.type === "seeds" ? `seed${e.entities[0].id}` : e.type), { reduceMotion: false });
  s.push(seeds); s.push({ type: "parts", t: 0, sub_questions: [] });
  await vi.runAllTimersAsync();
  expect(out).toEqual(["seed1", "seed2", "seed3", "parts"]);
  vi.useRealTimers();
});

test("stop cancels pending pieces", async () => {
  vi.useFakeTimers();
  const out: AskEvent[] = [];
  const s = createScheduler((e) => out.push(e), { reduceMotion: false });
  s.push(seeds); s.stop();
  await vi.runAllTimersAsync();
  expect(out.length).toBeLessThan(3);
  vi.useRealTimers();
});
```

- [ ] **Step 2: Run to verify failure.** `cd web && npx vitest run src/graphView.test.ts src/layout.test.ts src/scheduler.test.ts`. Expected: FAIL.

- [ ] **Step 3: Implement `graphView.ts`**

```ts
import type { AskState } from "./askReducer";
import { edgeKey, type Edge, type SubgraphData } from "./types";

export type NodeState = "shown" | "dim";
export type LinkState = "shown" | "cut" | "cited" | "faint";
export interface ViewNode { id: string; label: string; kind: string; books: string[]; seedPart: number | null; state: NodeState; expanded: boolean }
export interface ViewLink { id: string; source: string; target: string; label: string; count: number; chunkIds: string[];
  part: number | null; state: LinkState; hot: boolean; books: string[] }
export interface GraphView { nodes: ViewNode[]; links: ViewLink[]; shownEdges: number; totalEdges: number }
export interface ViewOptions { hiddenBooks: Set<string>; hoverChunk: string | null; expansions: SubgraphData[] }

const pretty = (p: string) => p.toLowerCase().replace(/_/g, " ");

export function deriveGraph(state: AskState, options: ViewOptions): GraphView {
  const nodes = new Map<string, ViewNode>();
  const touch = (id: string, label: string, kind = "Other", books: string[] = []) => {
    const key = id || label;
    const n = nodes.get(key) ?? { id: key, label, kind, books: [], seedPart: null, state: "shown" as NodeState, expanded: false };
    if (n.kind === "Other" && kind !== "Other") n.kind = kind;
    for (const b of books) if (!n.books.includes(b)) n.books.push(b);
    nodes.set(key, n);
    return n;
  };
  state.seeds.forEach((list, part) => list?.forEach((s) => {
    const n = touch(s.id, s.name, s.kind, s.books);
    if (n.seedPart === null) n.seedPart = part;
  }));

  const groups = new Map<string, { source: string; target: string; rows: Edge[]; part: number | null }>();
  state.edges.forEach((pe, part) => pe?.edges.forEach((e) => {
    const source = touch(e.subject_id, e.subject, undefined, [e.book_id]).id;
    const target = touch(e.object_id, e.object, undefined, [e.book_id]).id;
    const id = `${source}->${target}`;
    const g = groups.get(id) ?? { source, target, rows: [], part };
    g.rows.push(e);
    groups.set(id, g);
  }));

  const cited = new Set<string>();
  if (state.result) {
    const byN = new Map(state.result.passages.map((p) => [p.n, p.chunk_id]));
    for (const c of state.result.claims) { const chunk = byN.get(c.passage); if (chunk) cited.add(chunk); }
  }
  const kept = state.evidence ? new Set(state.evidence.edgeKeys) : null;
  const hidden = options.hiddenBooks;
  const allHidden = (books: string[]) => books.length > 0 && books.every((b) => hidden.has(b));

  const links: ViewLink[] = [];
  for (const [id, g] of groups) {
    const counts = new Map<string, number>();
    g.rows.forEach((r) => counts.set(r.predicate, (counts.get(r.predicate) ?? 0) + 1));
    const top = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0][0];
    const chunkIds = [...new Set(g.rows.map((r) => r.chunk_id))];
    const books = [...new Set(g.rows.map((r) => r.book_id))];
    let linkState: LinkState = "shown";
    if (state.result) linkState = chunkIds.some((c) => cited.has(c)) ? "cited" : "faint";
    else if (kept) linkState = g.rows.some((r) => kept.has(edgeKey(r))) ? "shown" : "cut";
    if (allHidden(books)) linkState = "faint";
    links.push({ id, source: g.source, target: g.target, label: pretty(top) + (g.rows.length > 1 ? ` ×${g.rows.length}` : ""),
      count: g.rows.length, chunkIds, part: g.part, state: linkState,
      hot: options.hoverChunk !== null && chunkIds.includes(options.hoverChunk), books });
  }

  for (const sub of options.expansions) {
    for (const n of sub.nodes) touch(n.id, n.name, n.kind, n.books);
    const root = sub.nodes[0]?.id;
    if (root && nodes.has(root)) nodes.get(root)!.expanded = true;
    for (const l of sub.edges) {
      const id = `${l.subject_id}->${l.object_id}`;
      if (groups.has(id) || links.some((x) => x.id === id)) continue;
      const top = Object.entries(l.predicates).sort((a, b) => b[1] - a[1])[0]?.[0] ?? "";
      links.push({ id, source: l.subject_id, target: l.object_id, label: pretty(top) + (l.count > 1 ? ` ×${l.count}` : ""),
        count: l.count, chunkIds: [], part: null, state: "shown", hot: false, books: [] });
    }
  }

  for (const n of nodes.values()) n.state = allHidden(n.books) ? "dim" : "shown";
  return {
    nodes: [...nodes.values()], links,
    shownEdges: state.edges.reduce((a, pe) => a + (pe?.edges.length ?? 0), 0),
    totalEdges: state.edges.reduce((a, pe) => a + (pe?.total ?? 0), 0),
  };
}
```

`subgraph`'s first node must be the root for `expanded` to mark the right node. Check `_expand` in `lore/mcp/tools.py`. If the root isn't first, have `GraphPane` pass the root id with each expansion (`{root, data}`) and mark that node instead.

- [ ] **Step 4: Implement `layout.ts`**

```ts
import type { GraphView } from "./graphView";

export type Positions = Map<string, { x: number; y: number }>;
const CX = 500, CY = 350;

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0) / 4294967296;
}

export function place(previous: Positions, view: GraphView, anchor: string | null = null): Positions {
  const pos: Positions = new Map(previous);
  const fresh = view.nodes.filter((n) => !pos.has(n.id));
  const seeds = fresh.filter((n) => n.seedPart !== null);
  seeds.forEach((n, i) => {
    const a = (n.seedPart ?? 0) * 0.6 + (2 * Math.PI * i) / Math.max(1, seeds.length);
    pos.set(n.id, { x: CX + 220 * Math.cos(a), y: CY + 220 * Math.sin(a) });
  });
  const neighbours = new Map<string, string[]>();
  for (const l of view.links) {
    neighbours.set(l.source, [...(neighbours.get(l.source) ?? []), l.target]);
    neighbours.set(l.target, [...(neighbours.get(l.target) ?? []), l.source]);
  }
  const others = fresh.filter((n) => n.seedPart === null);
  for (const n of others) {
    const near = (neighbours.get(n.id) ?? []).find((m) => pos.has(m)) ?? anchor ?? null;
    const c = (near && pos.get(near)) || { x: CX, y: CY };
    const a = 2 * Math.PI * hash(n.id);
    pos.set(n.id, { x: c.x + 140 * Math.cos(a), y: c.y + 140 * Math.sin(a) });
  }
  const movable = new Set(others.map((n) => n.id));
  const ids = [...pos.keys()];
  for (let it = 0; it < 60; it++) {
    for (const id of movable) {
      const p = pos.get(id)!;
      for (const other of ids) {
        if (other === id) continue;
        const q = pos.get(other)!;
        let dx = p.x - q.x, dy = p.y - q.y;
        let d = Math.hypot(dx, dy);
        if (d === 0) { dx = hash(id + other) - 0.5; dy = 0.5 - hash(other + id); d = Math.hypot(dx, dy) || 1; }
        if (d < 70) { const push = (70 - d) / 2 + 0.5; p.x += (dx / d) * push; p.y += (dy / d) * push; }
      }
    }
  }
  return pos;
}
```

The minimum-spacing test asks for 60. The repulsion targets 70 among all pairs that include at least one movable node. Seeds on the circle are 2πr/n apart, which for up to 20 seeds (r=220) is at least 69. If a fixture still fails the spacing test, raise the circle radius for more than 20 seeds: `r = Math.max(220, 12 * seeds.length)`.

- [ ] **Step 5: Implement `scheduler.ts`**

```ts
import type { AskEvent } from "./types";

export const STAGGER_TOTAL_MS = 500;
const MIN_GAP_MS = 15;
const REPLAY_GAP_CAP_MS = 600;

export function splitForStagger(event: AskEvent, chunk: number): AskEvent[] {
  if (event.type === "seeds")
    return event.entities.map((e) => ({ ...event, entities: [e] }));
  if (event.type === "edges" && event.edges.length > 0) {
    const out: AskEvent[] = [];
    for (let i = 0; i < event.edges.length; i += chunk) out.push({ ...event, edges: event.edges.slice(i, i + chunk) });
    return out;
  }
  return [event];
}

export interface Scheduler { push(event: AskEvent): void; stop(): void }

export function createScheduler(dispatch: (e: AskEvent) => void, opts: { reduceMotion: boolean; replay?: boolean }): Scheduler {
  if (opts.reduceMotion && !opts.replay) return { push: dispatch, stop: () => {} };
  let chain: Promise<void> = Promise.resolve();
  let stopped = false;
  let lastT: number | null = null;
  const timers = new Set<ReturnType<typeof setTimeout>>();
  const sleep = (ms: number) => new Promise<void>((resolve) => {
    if (ms <= 0) return resolve();
    const id = setTimeout(() => { timers.delete(id); resolve(); }, ms);
    timers.add(id);
  });
  return {
    push(event) {
      chain = chain.then(async () => {
        if (stopped) return;
        if (opts.replay && lastT !== null) await sleep(Math.min(REPLAY_GAP_CAP_MS, Math.max(0, (event.t - lastT) * 1000)));
        lastT = event.t;
        const chunk = event.type === "edges" ? Math.max(1, Math.ceil(event.edges.length / 12)) : 1;
        const pieces = opts.reduceMotion ? [event] : splitForStagger(event, chunk);
        const gap = Math.max(MIN_GAP_MS, STAGGER_TOTAL_MS / pieces.length);
        for (let i = 0; i < pieces.length; i++) {
          if (stopped) return;
          dispatch(pieces[i]);
          if (pieces.length > 1 && i < pieces.length - 1) await sleep(gap);
        }
      });
    },
    stop() { stopped = true; timers.forEach(clearTimeout); timers.clear(); },
  };
}
```

- [ ] **Step 6: Run the tests.** `cd web && npx vitest run src/graphView.test.ts src/layout.test.ts src/scheduler.test.ts`. Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git config user.email
git add web/src/graphView.ts web/src/graphView.test.ts web/src/layout.ts web/src/layout.test.ts web/src/scheduler.ts web/src/scheduler.test.ts
git commit -m "feat: graph view derived purely from ask state, stable layout, stagger scheduler

Cited, cut, faint and per-part colours are computed, never set by hand.
Placed nodes never move. The stagger only delays when real events reach
the reducer, and reduced motion turns it off."
```

---

### Task 11: Messages, quote highlighting and the useAsk hook

**Files:**
- Create: `web/src/messages.ts`, `web/src/messages.test.ts`
- Create: `web/src/highlight.ts`, `web/src/highlight.test.ts`
- Create: `web/src/useAsk.ts`, `web/src/useAsk.test.tsx`

**Interfaces:**
- Consumes: `streamAsk` (Task 8), `askReducer`, `initialState`, `history` (Task 9), `createScheduler` (Task 10).
- Produces:

```ts
// messages.ts
export interface Msg { text: string; retry?: boolean; command?: string }
export function errorMessage(error: ApiError, ctx: { showInternals: boolean; resetsAt?: string; now?: Date }): Msg;
export const STEP_LABEL: Record<StepName, string>;   // "Splitting the question", ...
export const UI: { askPlaceholder: string; noBooks: string; refusal: string; partial: (parts: string[]) => string;
  showingEdges: (shown: number, total: number) => string; ingestHint: string; mcpNotRunning: string };
export const OLLAMA_START = "CUDA_VISIBLE_DEVICES=-1 OLLAMA_VULKAN=0 ~/.local/ollama/bin/ollama serve";
// highlight.ts
export interface Span { text: string; mark: boolean }
export function highlightQuote(text: string, quote: string): Span[];
// useAsk.ts
export function useAsk(): { state: AskState; history: HistoryItem[]; current: string | null;
  ask(question: string): void; replay(id: string): void; retry(): void; busy: boolean };
```

- [ ] **Step 1: Write the failing tests**

`web/src/messages.test.ts`:

```ts
import { errorMessage } from "./messages";

const now = new Date("2026-10-07T20:40:00-07:00");
const resetsAt = "2026-10-08T00:00:00-07:00";

test("budget names Gemini and the time left when internals show", () => {
  const m = errorMessage({ code: "budget_exhausted", message: "" }, { showInternals: true, resetsAt, now });
  expect(m.text).toBe("Today's Gemini requests are used up. They reset at midnight Pacific (in about 3h 20m).");
});

test("budget hides the provider when internals are off", () => {
  const m = errorMessage({ code: "budget_exhausted", message: "" }, { showInternals: false, resetsAt, now });
  expect(m.text).not.toMatch(/Gemini/);
});

test("overload offers a retry; Ollama gives the start command only with internals", () => {
  expect(errorMessage({ code: "upstream_unavailable", message: "" }, { showInternals: true }).retry).toBe(true);
  const o = errorMessage({ code: "ollama_unavailable", message: "" }, { showInternals: true });
  expect(o.command).toContain("CUDA_VISIBLE_DEVICES=-1");
  expect(errorMessage({ code: "ollama_unavailable", message: "" }, { showInternals: false }).command).toBeUndefined();
});

test("unknown codes fall back to the server's message", () => {
  expect(errorMessage({ code: "weird", message: "it broke" }, { showInternals: true }).text).toBe("it broke");
});
```

Before writing `messages.ts`, check which code `tools._ollama_unavailable` returns (read `lore/mcp/tools.py`), and use that code in place of `ollama_unavailable` if it differs.

`web/src/highlight.test.ts`:

```ts
import { highlightQuote } from "./highlight";

const marked = (spans: { text: string; mark: boolean }[]) => spans.filter((s) => s.mark).map((s) => s.text);

test("marks an exact quote", () => {
  expect(marked(highlightQuote("He said hello to Watson today.", "said hello to Watson"))).toEqual(["said hello to Watson"]);
});

test("matches across curly quotes, dashes and case", () => {
  const text = "“That girl that I spoke of was to have married me twenty years ago—she was forced.”";
  expect(marked(highlightQuote(text, '"that girl that I spoke of was to have married me twenty years ago-she'))).toHaveLength(1);
});

test("marks each ellipsis segment of at least three words", () => {
  const text = "guilty of the death of two human beings, a father and a daughter, and that they had forfeited their own lives.";
  const spans = highlightQuote(text, "...guilty of the death of two human beings ... they had forfeited their own lives.");
  expect(marked(spans)).toEqual(["guilty of the death of two human beings", "they had forfeited their own lives."]);
  expect(spans.map((s) => s.text).join("")).toBe(text);
});

test("an unfound quote marks nothing and keeps the text", () => {
  const spans = highlightQuote("plain text", "not here at all");
  expect(spans).toEqual([{ text: "plain text", mark: false }]);
});
```

`web/src/useAsk.test.tsx`:

```tsx
import { act, renderHook, waitFor } from "@testing-library/react";
import single from "./fixtures/single-part.json";
import type { AskEvent } from "./types";

vi.mock("./api", () => ({
  streamAsk: async function* () { for (const e of single as AskEvent[]) yield e; },
}));
Object.defineProperty(window, "matchMedia", { value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });

test("ask streams to done and lands in history; replay redraws without a request", async () => {
  const { useAsk } = await import("./useAsk");
  const { result } = renderHook(() => useAsk());
  act(() => result.current.ask("How is Jefferson Hope connected to John Ferrier?"));
  await waitFor(() => expect(result.current.state.status).toBe("done"));
  expect(result.current.history).toHaveLength(1);
  const id = result.current.history[0].id;
  act(() => result.current.replay(id));
  await waitFor(() => expect(result.current.state.status).toBe("done"));
  expect(result.current.current).toBe(id);
});
```

- [ ] **Step 2: Run to verify failure.** `cd web && npx vitest run src/messages.test.ts src/highlight.test.ts src/useAsk.test.tsx`. Expected: FAIL.

- [ ] **Step 3: Implement `messages.ts`**

```ts
import type { ApiError, StepName } from "./types";

export interface Msg { text: string; retry?: boolean; command?: string }
export const OLLAMA_START = "CUDA_VISIBLE_DEVICES=-1 OLLAMA_VULKAN=0 ~/.local/ollama/bin/ollama serve";

export const STEP_LABEL: Record<StepName, string> = {
  split: "Splitting the question", seeds: "Finding starting entities", search: "Searching the graph",
  rank: "Ranking passages", write: "Writing the answer", check: "Checking claims against passages",
};

export const UI = {
  askPlaceholder: "Ask about your books…",
  noBooks: "Pick at least one book to see its part of the graph.",
  refusal: "The books don't contain enough to answer that. The graph shows where LORE looked.",
  partial: (parts: string[]) => `Not found in the books: ${parts.join("; ")}`,
  showingEdges: (shown: number, total: number) => `Showing ${shown} of ${total} relationships`,
  ingestHint: "Adding books from the browser comes next. For now, run this in a terminal:",
  mcpNotRunning: "HTTP server not running",
};

function until(resetsAt: string | undefined, now: Date): string {
  if (!resetsAt) return "";
  const mins = Math.max(0, Math.round((new Date(resetsAt).getTime() - now.getTime()) / 60000));
  return ` (in about ${Math.floor(mins / 60)}h ${mins % 60}m)`;
}

export function errorMessage(error: ApiError, ctx: { showInternals: boolean; resetsAt?: string; now?: Date }): Msg {
  const i = ctx.showInternals;
  switch (error.code) {
    case "budget_exhausted":
      return { text: `Today's ${i ? "Gemini requests are" : "question allowance is"} used up. `
        + `${i ? "They reset" : "It resets"} at midnight Pacific${until(ctx.resetsAt, ctx.now ?? new Date())}.` };
    case "upstream_unavailable":
      return { text: `${i ? "Gemini" : "The answer service"} is busy right now. Try again in a minute.`, retry: true };
    case "ollama_unavailable":
      return i ? { text: "LORE can't reach Ollama. Start it, then ask again.", command: OLLAMA_START, retry: true }
        : { text: "LORE's search service isn't running. Start it, then ask again.", retry: true };
    case "busy":
      return { text: "LORE is still answering the previous question." };
    case "interrupted":
      return { text: "The connection dropped before the answer finished.", retry: true };
    case "unreachable":
      return { text: "LORE isn't reachable. Check that `lore ui` is still running.", retry: true };
    case "no_graph":
      return { text: error.message || "There's no graph yet. Process a book first." };
    case "bad_request":
      return { text: error.message || "That question couldn't be sent. Questions are 1 to 2,000 characters." };
    default:
      return { text: error.message || "Something went wrong.", retry: true };
  }
}
```

- [ ] **Step 4: Implement `highlight.ts`**

```ts
export interface Span { text: string; mark: boolean }

// One-for-one character folds, so indexes in the folded text are indexes in the original.
const fold = (s: string) => s.toLowerCase().replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/[–—]/g, "-");
// Normalise a quote the way the backend's verify does: no quote marks, single spaces.
const normQuote = (s: string) => fold(s).replace(/["]/g, "").replace(/\s+/g, " ").trim();

function findLoose(text: string, needle: string, from: number): [number, number] | null {
  // Match needle in text ignoring quote marks and collapsing whitespace, returning original indexes.
  const f = fold(text);
  const words = needle.split(" ").filter(Boolean);
  if (!words.length) return null;
  const pattern = words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(`["\\s]+`);
  const re = new RegExp(`["]?${pattern}`, "g");
  re.lastIndex = from;
  const m = re.exec(f);
  return m ? [m.index + (m[0].startsWith('"') ? 1 : 0), m.index + m[0].length] : null;
}

export function highlightQuote(text: string, quote: string): Span[] {
  const segments = quote.split(/\.\.\.|…/).map(normQuote).filter((s) => s.split(" ").length >= 3);
  const ranges: [number, number][] = [];
  let from = 0;
  for (const seg of segments) {
    const r = findLoose(text, seg, from);
    if (r) { ranges.push(r); from = r[1]; }
  }
  if (!ranges.length) return [{ text, mark: false }];
  const spans: Span[] = [];
  let at = 0;
  for (const [a, b] of ranges) {
    if (a > at) spans.push({ text: text.slice(at, a), mark: false });
    spans.push({ text: text.slice(a, b), mark: true });
    at = b;
  }
  if (at < text.length) spans.push({ text: text.slice(at), mark: false });
  return spans;
}
```

- [ ] **Step 5: Implement `useAsk.ts`**

```ts
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { streamAsk } from "./api";
import { askReducer, initialState, type AskState } from "./askReducer";
import { addToHistory, loadHistory, saveHistory, type HistoryItem } from "./history";
import { createScheduler, type Scheduler } from "./scheduler";
import type { AskEvent } from "./types";

type Action = { kind: "reset"; question: string } | { kind: "event"; event: AskEvent };
function reducer(state: AskState, action: Action): AskState {
  return action.kind === "reset" ? initialState(action.question) : askReducer(state, action.event);
}
const reduceMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

export function useAsk() {
  const [state, dispatch] = useReducer(reducer, initialState());
  const [history, setHistory] = useState<HistoryItem[]>(() => loadHistory());
  const [current, setCurrent] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const scheduler = useRef<Scheduler | null>(null);
  const last = useRef<string>("");

  const stop = () => { abort.current?.abort(); scheduler.current?.stop(); };
  useEffect(() => stop, []);

  const ask = useCallback((question: string) => {
    stop();
    last.current = question;
    dispatch({ kind: "reset", question });
    const controller = new AbortController();
    abort.current = controller;
    const sched = createScheduler((event) => dispatch({ kind: "event", event }), { reduceMotion: reduceMotion() });
    scheduler.current = sched;
    const id = `${Date.now()}`;
    setCurrent(id);
    (async () => {
      const events: AskEvent[] = [];
      for await (const event of streamAsk(question, controller.signal)) { events.push(event); sched.push(event); }
      if (controller.signal.aborted) return;
      const answer = events.find((e) => e.type === "answer");
      if (answer && answer.type === "answer") {
        setHistory((h) => { const next = addToHistory(h, { id, question, events, answer: answer.result, askedAt: new Date().toISOString() }); saveHistory(next); return next; });
      }
    })();
  }, []);

  const replay = useCallback((id: string) => {
    const item = history.find((h) => h.id === id);
    if (!item) return;
    stop();
    setCurrent(id);
    dispatch({ kind: "reset", question: item.question });
    const sched = createScheduler((event) => dispatch({ kind: "event", event }), { reduceMotion: reduceMotion(), replay: !reduceMotion() });
    scheduler.current = sched;
    item.events.forEach((e) => sched.push(e));
  }, [history]);

  const retry = useCallback(() => { if (last.current) ask(last.current); }, [ask]);
  return { state, history, current, ask, replay, retry, busy: state.status === "running" };
}
```

- [ ] **Step 6: Run the tests.** `cd web && npx vitest run src/messages.test.ts src/highlight.test.ts src/useAsk.test.tsx`. Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git config user.email
git add web/src/messages.ts web/src/messages.test.ts web/src/highlight.ts web/src/highlight.test.ts web/src/useAsk.ts web/src/useAsk.test.tsx
git commit -m "feat: one message table (internals switchable), quote highlighting, and the useAsk hook"
```

---

### Task 12: Top bar: books dropdown, theme, settings, add book, quota

**Files:**
- Create: `web/src/components/TopBar.tsx`, `BooksDropdown.tsx`, `SettingsMenu.tsx`, `AddBookDialog.tsx`, `QuotaChip.tsx`, `web/src/theme.ts`
- Test: `web/src/components/BooksDropdown.test.tsx`, `web/src/components/QuotaChip.test.tsx`, `web/src/theme.test.ts`

**Interfaces:**
- Consumes: `api`, `isError`, `Book`, `Quota`, `McpInfo` (Task 8); `UI`, `errorMessage` (Task 11).
- Produces:
  - `<TopBar books={Book[]} hiddenBooks={Set<string>} onBooksChange={(s: Set<string>) => void} showInternals={boolean} />`
  - `<BooksDropdown books hiddenBooks onChange />`
  - `<SettingsMenu showInternals />`
  - `<AddBookDialog open onClose />`
  - `<QuotaChip quota={Quota | null} />`
  - `theme.ts`: `type ThemeMode = "system" | "light" | "dark"`, `applyTheme(mode)`, `loadTheme(): ThemeMode`, `isDark(): boolean`.

Behaviour copies the mock (`docs/superpowers/specs/assets/2026-10-07-phase-5a-mock.html`):
- **BooksDropdown.** The button label reads "All books (N)", "No books", the book title when one is selected, or "k of N books". The popover has a filter box, Select all and Clear, and one checkbox per book with its author and status. A book whose status is not `graphed` is shown disabled with the meta text "not graphed yet". Escape or a click outside closes it.
- **SettingsMenu** (rendered only when `showInternals`). Claude Code and HTTP tabs, the commands from `/api/mcp`, Copy buttons (`navigator.clipboard.writeText`, falling back to selecting the text), and a status dot. Appearance is a System/Light/Dark segmented control. When `showInternals` is false, the gear menu shows only Appearance.
- **AddBookDialog.** A drop zone plus a file input (`.epub,.pdf`). A wrong file type shows a message. After a valid pick it shows `UI.ingestHint` and the copyable command `lore ingest "<file name>"`, since upload itself is 5b. Esc and a click on the backdrop close it.
- **QuotaChip** (hidden unless `showInternals` and `quota.provider === "gemini"`). Shows "Gemini: R / L left", summing remaining and limit over models. The class is `warn` under 10% and `danger` at 0.

- [ ] **Step 1: Write the failing tests**

`web/src/components/BooksDropdown.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BooksDropdown } from "./BooksDropdown";
import type { Book } from "../types";

const books: Book[] = Array.from({ length: 12 }, (_, i) => ({ id: `b${i}`, title: `Book ${i}`, author: i % 2 ? "Doyle" : "Austen",
  status: i === 11 ? "chunked" : "graphed", chunk_count: 10, entity_count: 5 }));

test("filters a long library and toggles books", async () => {
  const onChange = vi.fn();
  render(<BooksDropdown books={books} hiddenBooks={new Set()} onChange={onChange} />);
  expect(screen.getByRole("button", { name: /All books \(11\)/ })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /All books/ }));
  await userEvent.type(screen.getByRole("searchbox"), "Book 1");
  expect(screen.getAllByRole("checkbox")).toHaveLength(3);   // Book 1, 10, 11
  expect(screen.getByRole("checkbox", { name: /Book 11/ })).toBeDisabled();
  await userEvent.click(screen.getByRole("checkbox", { name: /Book 10/ }));
  expect(onChange).toHaveBeenCalledWith(new Set(["b10"]));
});

test("label summarises the selection", () => {
  const { rerender } = render(<BooksDropdown books={books} hiddenBooks={new Set(books.slice(0, 10).map((b) => b.id))} onChange={() => {}} />);
  expect(screen.getByRole("button", { name: "Book 10" })).toBeInTheDocument();
  rerender(<BooksDropdown books={books} hiddenBooks={new Set(["b1"])} onChange={() => {}} />);
  expect(screen.getByRole("button", { name: /10 of 11 books/ })).toBeInTheDocument();
});
```

`web/src/components/QuotaChip.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { QuotaChip } from "./QuotaChip";

const q = (remaining: number) => ({ provider: "gemini" as const, resets_at: "x",
  models: [{ model: "a", count: 500 - remaining, limit: 500, remaining }, { model: "b", count: 500, limit: 500, remaining: 0 }] });

test("shows remaining over limit and warns when low", () => {
  render(<QuotaChip quota={q(40)} showInternals />);
  const chip = screen.getByText("Gemini: 40 / 1000 left");
  expect(chip).toHaveClass("warn");
});

test("hidden for local providers or without internals", () => {
  const { container, rerender } = render(<QuotaChip quota={{ provider: "local", models: [], resets_at: "" }} showInternals />);
  expect(container).toBeEmptyDOMElement();
  rerender(<QuotaChip quota={q(400)} showInternals={false} />);
  expect(container).toBeEmptyDOMElement();
});
```

`QuotaChip` takes `showInternals` too; adjust the TopBar interface accordingly.

`web/src/theme.test.ts`:

```ts
import { applyTheme, loadTheme } from "./theme";

test("system removes data-theme; light and dark set it and persist", () => {
  applyTheme("dark");
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(loadTheme()).toBe("dark");
  applyTheme("system");
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  expect(loadTheme()).toBe("system");
});
```

- [ ] **Step 2: Run to verify failure.** `cd web && npx vitest run src/components src/theme.test.ts`. Expected: FAIL.

- [ ] **Step 3: Implement `theme.ts`**

```ts
export type ThemeMode = "system" | "light" | "dark";
const KEY = "lore-theme";

export function applyTheme(mode: ThemeMode): void {
  if (mode === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
  try { localStorage.setItem(KEY, mode); } catch { /* storage blocked: the choice lasts this visit */ }
}

export function loadTheme(): ThemeMode {
  try { const v = localStorage.getItem(KEY); if (v === "light" || v === "dark" || v === "system") return v; } catch { /* blocked */ }
  return "system";
}

export function isDark(): boolean {
  const t = document.documentElement.getAttribute("data-theme");
  return t ? t === "dark" : window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}
```

- [ ] **Step 4: Implement the components**

`web/src/components/BooksDropdown.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import type { Book } from "../types";

export function BooksDropdown({ books, hiddenBooks, onChange }: { books: Book[]; hiddenBooks: Set<string>; onChange: (s: Set<string>) => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const graphed = books.filter((b) => b.status === "graphed");
  const on = graphed.filter((b) => !hiddenBooks.has(b.id));
  const label = on.length === graphed.length ? `All books (${graphed.length})` : on.length === 0 ? "No books"
    : on.length === 1 ? on[0].title : `${on.length} of ${graphed.length} books`;
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  const shown = books.filter((b) => !q || `${b.title} ${b.author ?? ""}`.toLowerCase().includes(q.toLowerCase()));
  const toggle = (id: string, checked: boolean) => { const next = new Set(hiddenBooks); checked ? next.delete(id) : next.add(id); onChange(next); };
  return (
    <div className="pop" ref={root}>
      <button className="tbtn" type="button" aria-haspopup="true" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span>{label}</span><span className="caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="menu books-menu">
          <input type="search" placeholder="Filter books…" aria-label="Filter books" value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
          <div className="linkrow">
            <button type="button" onClick={() => onChange(new Set())}>Select all</button>
            <button type="button" onClick={() => onChange(new Set(graphed.map((b) => b.id)))}>Clear</button>
          </div>
          <ul className="booklist">
            {shown.map((b) => {
              const ok = b.status === "graphed";
              return (
                <li key={b.id}><label className={ok ? "" : "off"}>
                  <input type="checkbox" disabled={!ok} checked={ok && !hiddenBooks.has(b.id)} onChange={(e) => toggle(b.id, e.target.checked)} />
                  <span>{b.title}<span className="meta">{[b.author, ok ? `${b.entity_count ?? "?"} entities` : "not graphed yet"].filter(Boolean).join(" · ")}</span></span>
                </label></li>
              );
            })}
            {!shown.length && <li className="hint">No book matches “{q}”.</li>}
          </ul>
          <p className="hint">Only graphed books can be searched. The rest are still processing.</p>
        </div>
      )}
    </div>
  );
}
```

`web/src/components/QuotaChip.tsx`:

```tsx
import type { Quota } from "../types";

export function QuotaChip({ quota, showInternals }: { quota: Quota | null; showInternals: boolean }) {
  if (!showInternals || !quota || quota.provider !== "gemini" || !quota.models.length) return null;
  const left = quota.models.reduce((a, m) => a + m.remaining, 0);
  const limit = quota.models.reduce((a, m) => a + m.limit, 0);
  const cls = left === 0 ? "quota danger" : left / limit < 0.1 ? "quota warn" : "quota";
  return <span className={cls} title="Gemini requests left today">{`Gemini: ${left} / ${limit} left`}</span>;
}
```

Add to `theme.css`: `.quota.warn{color:var(--warn)} .quota.danger{color:var(--danger);font-weight:600}`.

`web/src/components/SettingsMenu.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import { api, isError } from "../api";
import { UI } from "../messages";
import { applyTheme, loadTheme, type ThemeMode } from "../theme";
import type { McpInfo } from "../types";

function Snip({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const copy = () => {
    const select = () => { const r = document.createRange(); r.selectNodeContents(ref.current!); const s = getSelection(); s?.removeAllRanges(); s?.addRange(r); };
    try { navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1400); }, select); } catch { select(); }
  };
  return <div className="snip"><span ref={ref}>{text}</span><button className="copy" type="button" onClick={copy}>{copied ? "Copied" : "Copy"}</button></div>;
}

export { Snip };

export function SettingsMenu({ showInternals }: { showInternals: boolean }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"stdio" | "http">("stdio");
  const [mcp, setMcp] = useState<McpInfo | null>(null);
  const [mode, setMode] = useState<ThemeMode>(loadTheme());
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => { if (open && showInternals) api.mcp().then((r) => { if (!isError(r)) setMcp(r); }); }, [open, showInternals]);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  const choose = (m: ThemeMode) => { applyTheme(m); setMode(m); };
  return (
    <div className="pop" ref={root}>
      <button className="tbtn icon" type="button" aria-label="Settings" aria-haspopup="true" aria-expanded={open} onClick={() => setOpen(!open)}>⚙</button>
      {open && (
        <div className="menu">
          {showInternals && (<>
            <h4>Connect an MCP client</h4>
            <div className="tabs" role="tablist">
              <button type="button" role="tab" aria-selected={tab === "stdio"} onClick={() => setTab("stdio")}>Claude Code</button>
              <button type="button" role="tab" aria-selected={tab === "http"} onClick={() => setTab("http")}>HTTP</button>
            </div>
            {tab === "stdio" && mcp && (<>
              <p className="hint">Run once in a terminal. Claude Code starts LORE itself when it needs it.</p>
              <Snip text={mcp.stdio_command} />
            </>)}
            {tab === "http" && mcp && (<>
              <p className="hint">For clients that connect over HTTP. Start the server, then point the client at the address. Loopback only.</p>
              <Snip text={mcp.http_command} /><Snip text={mcp.http_url} />
              <span className={`status${mcp.http_running ? " on" : ""}`}><i />{mcp.http_running ? "HTTP server running" : UI.mcpNotRunning}</span>
            </>)}
          </>)}
          <h4>Appearance</h4>
          <div className="seg" role="group" aria-label="Theme">
            {(["system", "light", "dark"] as ThemeMode[]).map((m) => (
              <button key={m} type="button" aria-pressed={mode === m} onClick={() => choose(m)}>{m[0].toUpperCase() + m.slice(1)}</button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
```

`web/src/components/AddBookDialog.tsx`:

```tsx
import { useEffect, useState } from "react";
import { UI } from "../messages";
import { Snip } from "./SettingsMenu";

export function AddBookDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [msg, setMsg] = useState("");
  const [over, setOver] = useState(false);
  useEffect(() => { if (!open) { setFile(null); setMsg(""); } }, [open]);
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [open, onClose]);
  if (!open) return null;
  const pick = (f?: File) => {
    if (!f) return;
    if (!/\.(epub|pdf)$/i.test(f.name)) { setFile(null); setMsg(`${f.name} is not an EPUB or PDF. Choose a .epub or .pdf file.`); return; }
    setFile(f); setMsg("");
  };
  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="add-title">
        <h2 id="add-title">Add a book</h2>
        <label className={`drop${over ? " over" : ""}`} htmlFor="add-file"
          onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files[0]); }}>
          <strong>Drop an EPUB or PDF here</strong><br />or click to choose a file
        </label>
        <input id="add-file" type="file" accept=".epub,.pdf" hidden onChange={(e) => pick(e.target.files?.[0])} />
        {msg && <p className="hint" role="alert">{msg}</p>}
        {file ? (<><p className="hint">{UI.ingestHint}</p><Snip text={`lore ingest "${file.name}"`} /></>)
          : <p className="hint">LORE splits the book into passages, then extracts characters, places and relationships.</p>}
        <div className="row-end"><button className="tbtn" type="button" onClick={onClose}>Close</button></div>
      </div>
    </div>
  );
}
```

`web/src/components/TopBar.tsx`:

```tsx
import { useEffect, useState } from "react";
import { api, isError } from "../api";
import { applyTheme, isDark } from "../theme";
import type { Book, Quota } from "../types";
import { AddBookDialog } from "./AddBookDialog";
import { BooksDropdown } from "./BooksDropdown";
import { QuotaChip } from "./QuotaChip";
import { SettingsMenu } from "./SettingsMenu";

export function TopBar({ books, hiddenBooks, onBooksChange, showInternals, refreshKey }:
  { books: Book[]; hiddenBooks: Set<string>; onBooksChange: (s: Set<string>) => void; showInternals: boolean; refreshKey: number }) {
  const [adding, setAdding] = useState(false);
  const [quota, setQuota] = useState<Quota | null>(null);
  useEffect(() => { api.quota().then((q) => { if (!isError(q)) setQuota(q); }); }, [refreshKey]);
  return (
    <header>
      <span className="mark">LORE<small>Reader</small></span>
      <span className="spacer" />
      <div className="tools">
        <BooksDropdown books={books} hiddenBooks={hiddenBooks} onChange={onBooksChange} />
        <button className="tbtn primary" type="button" onClick={() => setAdding(true)}>＋ Add book</button>
        <QuotaChip quota={quota} showInternals={showInternals} />
        <button className="tbtn icon" type="button" aria-label="Switch light or dark" onClick={() => applyTheme(isDark() ? "light" : "dark")}>◐</button>
        <SettingsMenu showInternals={showInternals} />
      </div>
      <AddBookDialog open={adding} onClose={() => setAdding(false)} />
    </header>
  );
}
```

`refreshKey` lets the App refresh the quota after each answer.

- [ ] **Step 5: Run the tests.** `cd web && npx vitest run src/components src/theme.test.ts`. Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git config user.email
git add web/src/components/ web/src/theme.ts web/src/theme.test.ts web/src/theme.css
git commit -m "feat: top bar with books dropdown, add-book dialog, quota, theme and settings menu"
```

---

### Task 13: Chat pane, graph pane and the App

**Files:**
- Create: `web/src/components/ChatPane.tsx`, `StepList.tsx`, `Claims.tsx`, `HowPanel.tsx`, `AskBox.tsx`, `HistoryBar.tsx`, `GraphPane.tsx`, `GraphCanvas.tsx`, `EntityCard.tsx`
- Modify: `web/src/App.tsx`, `web/src/App.test.tsx`
- Test: `web/src/components/Claims.test.tsx`, `web/src/components/ChatPane.test.tsx`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `<ChatPane ask={ReturnType<typeof useAsk>} showInternals resetsAt? onHoverChunk={(c: string | null) => void} />`
  - `<GraphPane state={AskState} hiddenBooks hoverChunk noBooks={boolean} />`
  - `<GraphCanvas view={GraphView} positions={Positions} onNodeClick={(id: string) => void} />` (Sigma; not unit-tested; jsdom has no WebGL)
  - `<EntityCard id={string} onClose onExpand={(data: SubgraphData) => void} />`

Component behaviour, matching the mock:
- **StepList.** One row per step: ○ pending, ● running, ✓ done, – skipped. Running steps get the `now` class. Each done row shows its detail and `(doneAt - startedAt)` seconds.
- **Claims.** Each claim is a paragraph in the book font, followed by a citation button. The button's label is the book's short title, the chapter and `[n]`. Clicking it expands a `<blockquote class="passage">` with `highlightQuote` marks. Hovering a claim calls `onHoverChunk(chunk_id)`.
- **HowPanel** (`<details>`). Parts, the graph search template and relationship count, starting entities, passages read (and how many cited), and models, which appear only when `showInternals`.
- **ChatPane.** HistoryBar on top, then the thread: the question bubble, StepList, Claims or an error message (from `errorMessage`, with a Retry button and a copyable command when the message has one), the `UI.partial` line when `result.note` names parts not found, and the HowPanel. AskBox sits at the bottom and is disabled while `busy`.
- **GraphPane.** The StageBar shows the current running step's label, or "Answered in Xs · cited relationships glow", plus `UI.showingEdges` when `shownEdges < totalEdges`. Then the Legend, with the part colours labelled by sub-question when there are several. GraphCanvas fills the rest. EntityCard opens on node click; "Expand neighbours" calls `api.subgraph(id)` and adds the result to `expansions`. When `noBooks` is true it shows `UI.noBooks` instead of the canvas.
- **GraphCanvas.** Create one graphology `Graph` and one `Sigma` instance per mount. On each `view`/`positions` change, upsert nodes (x, y, size 10 for seeds or 7 otherwise, color from the CSS var for the kind; a seed node's border colour is its part colour, done through the `borderColor` attribute if the default node program supports it, otherwise a ring drawn by giving seeds a larger size and the part colour) and edges (`type: "arrow"`, color by state: cited→`--glow`, hot→`--glow` at size 4, cut→`--edge-faint`, faint→`--edge-faint`, shown→`--edge`, part colour when `part !== null` and the state is `shown`; size 2.6 when cited). Remove nodes and edges no longer in the view, then `sigma.refresh()`. Read CSS colours with `getComputedStyle(document.documentElement)`, and re-read them when `data-theme` changes or the system theme flips (a `MutationObserver` on `<html>` plus a `matchMedia` listener). Set `renderEdgeLabels: true`, and `labelColor` from `--ink`. Call `sigma.on("clickNode", ({ node }) => onNodeClick(node))`. Call `sigma.kill()` on unmount.
- **App.** Loads `api.settings()`, `api.library()`, and `useAsk()`. It holds `hiddenBooks` (initially empty), `hoverChunk`, and `refreshKey`, which it bumps when `state.status` becomes `done` so the quota refreshes. The layout is `.app > TopBar + .panes > (ChatPane, GraphPane)`, the mock's grid.

- [ ] **Step 1: Write the failing tests**

`web/src/components/Claims.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { Claims } from "./Claims";
import type { SearchResult } from "../types";

const result = { answer: "", mode: "graph", template: "neighbours", note: null, model: null, selection_model: null, sub_questions: ["q"],
  claims: [{ text: "Hope was to marry Lucy.", passage: 3, quote: "was to have married me twenty years ago" }],
  passages: [{ n: 3, chunk_id: "c3", book_id: "b1", book_title: "A Study in Scarlet", chapter_seq: 17,
    text: "That girl that I spoke of was to have married me twenty years ago." }], edges: [], seeds: [] } as SearchResult;

test("a citation opens its passage with the quote marked; hover reports the chunk", () => {
  const onHover = vi.fn();
  render(<Claims result={result} onHoverChunk={onHover} />);
  fireEvent.mouseEnter(screen.getByText("Hope was to marry Lucy.").closest("li")!);
  expect(onHover).toHaveBeenCalledWith("c3");
  fireEvent.click(screen.getByRole("button", { name: /ch\.17/ }));
  expect(screen.getByText("was to have married me twenty years ago").tagName).toBe("MARK");
});
```

`web/src/components/ChatPane.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import single from "../fixtures/single-part.json";
import { budget } from "../fixtures/synthetic";
import { askReducer, initialState } from "../askReducer";
import { ChatPane } from "./ChatPane";
import type { AskEvent } from "../types";

const askWith = (events: AskEvent[], extra = {}) => ({
  state: events.reduce(askReducer, initialState("q")), history: [], current: null,
  ask: vi.fn(), replay: vi.fn(), retry: vi.fn(), busy: false, ...extra });

test("renders steps, claims and the how panel for a recorded answer", () => {
  render(<ChatPane ask={askWith(single as AskEvent[])} showInternals onHoverChunk={() => {}} />);
  expect(screen.getByText("Splitting the question")).toBeInTheDocument();
  expect(screen.getByText("How this was answered")).toBeInTheDocument();
  expect(screen.getAllByRole("listitem").length).toBeGreaterThan(3);
});

test("an error shows its message and a retry for retryable codes", () => {
  render(<ChatPane ask={askWith([{ type: "error", t: 0, error: { code: "upstream_unavailable", message: "" } }])} showInternals onHoverChunk={() => {}} />);
  expect(screen.getByText(/busy right now/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
});

test("the budget message has no retry", () => {
  render(<ChatPane ask={askWith(budget)} showInternals onHoverChunk={() => {}} />);
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
});

test("models are hidden without internals", () => {
  render(<ChatPane ask={askWith(single as AskEvent[])} showInternals={false} onHoverChunk={() => {}} />);
  expect(screen.queryByText(/gemini/i)).toBeNull();
});
```

Update `web/src/App.test.tsx` to mock the API (`vi.mock("./api", ...)` returning `{ show_internals: true }`, `{ books: [] , note: null }`, and an empty quota) and assert that the Ask box renders, by placeholder "Ask about your books…".

- [ ] **Step 2: Run to verify failure.** `cd web && npx vitest run src/components/Claims.test.tsx src/components/ChatPane.test.tsx src/App.test.tsx`. Expected: FAIL.

- [ ] **Step 3: Implement the chat components**

`web/src/components/StepList.tsx`:

```tsx
import type { StepView } from "../askReducer";
import { STEP_LABEL } from "../messages";

const DOT = { pending: "○", running: "●", done: "✓", skipped: "–" } as const;

export function StepList({ steps }: { steps: StepView[] }) {
  return (
    <ol className="steps">
      {steps.map((s) => (
        <li key={s.name} className={s.state === "running" ? "now" : s.state}>
          <span className="dot" aria-hidden="true">{DOT[s.state]}</span>
          <span>{STEP_LABEL[s.name]}</span>
          <span className="t">{s.state === "done" ? [s.detail, s.startedAt !== null && s.doneAt !== null ? `${(s.doneAt - s.startedAt).toFixed(1)}s` : null].filter(Boolean).join(" · ") : ""}</span>
        </li>
      ))}
    </ol>
  );
}
```

`web/src/components/Claims.tsx`:

```tsx
import { useState } from "react";
import { highlightQuote } from "../highlight";
import type { SearchResult } from "../types";

const short = (title: string | null) => (title ?? "Book").replace(/^(A|An|The) /, "").split(" ")[0];

export function Claims({ result, onHoverChunk }: { result: SearchResult; onHoverChunk: (c: string | null) => void }) {
  const [open, setOpen] = useState<number | null>(null);
  const byN = new Map(result.passages.map((p) => [p.n, p]));
  return (
    <ul className="claims">
      {result.claims.map((c, i) => {
        const p = byN.get(c.passage);
        return (
          <li key={i} className="claim" onMouseEnter={() => onHoverChunk(p?.chunk_id ?? null)} onMouseLeave={() => onHoverChunk(null)}>
            <p>{c.text}
              {p && <button className="cite" type="button" aria-expanded={open === i} onClick={() => setOpen(open === i ? null : i)}>
                {`${short(p.book_title)} ch.${p.chapter_seq} · [${c.passage}]`}</button>}
            </p>
            {p && open === i && (
              <blockquote className="passage">
                <span className="src">{`${p.book_title ?? p.book_id} · chapter ${p.chapter_seq} · ${p.chunk_id}`}</span>
                {highlightQuote(p.text, c.quote).map((s, j) => s.mark ? <mark key={j}>{s.text}</mark> : <span key={j}>{s.text}</span>)}
              </blockquote>
            )}
          </li>
        );
      })}
    </ul>
  );
}
```

`web/src/components/HowPanel.tsx`:

```tsx
import type { AskState } from "../askReducer";

export function HowPanel({ state, showInternals }: { state: AskState; showInternals: boolean }) {
  const r = state.result;
  if (!r) return null;
  const byN = new Map(r.passages.map((p) => [p.n, p.chunk_id]));
  const cited = new Set(r.claims.map((c) => byN.get(c.passage)).filter(Boolean));
  const seeds = [...new Map(state.seeds.flat().map((s) => [s.id, s.name])).values()];
  const relationships = state.edges.reduce((a, pe) => a + (pe?.total ?? 0), 0);
  return (
    <details className="how">
      <summary>How this was answered</summary>
      <dl>
        <dt>Parts</dt><dd>{(state.subQuestions.length ? state.subQuestions : r.sub_questions).map((s) => <div key={s}>{s}</div>)}</dd>
        <dt>Graph search</dt><dd><code>{r.template ?? "none"}</code>{` · ${relationships} relationships`}</dd>
        <dt>Starting entities</dt><dd>{seeds.join(", ") || "none"}</dd>
        <dt>Passages read</dt><dd>{`${r.passages.length} (${r.claims.length} claims cite ${cited.size})`}</dd>
        {showInternals && <><dt>Models</dt><dd><code>{r.selection_model ?? "none"}</code> chose the search; <code>{r.model ?? "none"}</code> wrote the answer</dd></>}
      </dl>
    </details>
  );
}
```

`web/src/components/AskBox.tsx`:

```tsx
import { useState } from "react";
import { UI } from "../messages";

export function AskBox({ onAsk, busy }: { onAsk: (q: string) => void; busy: boolean }) {
  const [q, setQ] = useState("");
  return (
    <form className="ask" onSubmit={(e) => { e.preventDefault(); const v = q.trim(); if (v && !busy) { onAsk(v); setQ(""); } }}>
      <input id="ask-input" autoComplete="off" maxLength={2000} placeholder={UI.askPlaceholder} aria-label="Question" value={q} onChange={(e) => setQ(e.target.value)} />
      <button type="submit" disabled={busy || !q.trim()}>{busy ? "Answering…" : "Ask"}</button>
    </form>
  );
}
```

`web/src/components/HistoryBar.tsx`:

```tsx
import type { HistoryItem } from "../history";

export function HistoryBar({ items, current, onPick }: { items: HistoryItem[]; current: string | null; onPick: (id: string) => void }) {
  if (!items.length) return null;
  return (
    <nav className="history" aria-label="Earlier questions">
      {items.map((h) => <button key={h.id} type="button" title={h.question} aria-current={h.id === current} onClick={() => onPick(h.id)}>{h.question}</button>)}
    </nav>
  );
}
```

`web/src/components/ChatPane.tsx`:

```tsx
import type { useAsk } from "../useAsk";
import { errorMessage, UI } from "../messages";
import { Claims } from "./Claims";
import { HistoryBar } from "./HistoryBar";
import { HowPanel } from "./HowPanel";
import { AskBox } from "./AskBox";
import { StepList } from "./StepList";
import { Snip } from "./SettingsMenu";

export function ChatPane({ ask, showInternals, resetsAt, onHoverChunk }:
  { ask: ReturnType<typeof useAsk>; showInternals: boolean; resetsAt?: string; onHoverChunk: (c: string | null) => void }) {
  const { state } = ask;
  const r = state.result;
  const err = state.error ? errorMessage(state.error, { showInternals, resetsAt }) : null;
  const notFound = r?.note?.match(/not found: (.*)/)?.[1]?.split("; not found: ") ?? [];
  return (
    <section className="pane chat" aria-label="Conversation">
      <HistoryBar items={ask.history} current={ask.current} onPick={ask.replay} />
      <div className="thread" aria-live="polite">
        {state.question && <div className="q">{state.question}</div>}
        {state.question && (
          <div className="a">
            <StepList steps={state.steps} />
            {err && (
              <div className="note" role="alert">
                <p>{err.text}</p>
                {err.command && <Snip text={err.command} />}
                {err.retry && <button className="tbtn" type="button" onClick={ask.retry}>Retry</button>}
              </div>
            )}
            {r && r.claims.length === 0 && <p className="note">{UI.refusal}</p>}
            {r && r.claims.length > 0 && <Claims result={r} onHoverChunk={onHoverChunk} />}
            {notFound.length > 0 && <p className="note">{UI.partial(notFound)}</p>}
            <HowPanel state={state} showInternals={showInternals} />
          </div>
        )}
      </div>
      <AskBox onAsk={ask.ask} busy={ask.busy} />
    </section>
  );
}
```

- [ ] **Step 4: Implement the graph components and App**

`web/src/components/GraphCanvas.tsx`:

```tsx
import { useEffect, useRef } from "react";
import Graph from "graphology";
import Sigma from "sigma";
import type { GraphView } from "../graphView";
import type { Positions } from "../layout";

const KIND_VAR: Record<string, string> = { Character: "--k-character", Place: "--k-place", Faction: "--k-faction", Object: "--k-object" };

function palette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  return { v, ink: v("--ink"), edge: v("--edge"), faint: v("--edge-faint"), glow: v("--glow") };
}

export function GraphCanvas({ view, positions, onNodeClick }: { view: GraphView; positions: Positions; onNodeClick: (id: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma | null>(null);
  const graph = useRef(new Graph({ multi: false, type: "directed" }));
  const click = useRef(onNodeClick);
  click.current = onNodeClick;
  const latest = useRef({ view, positions });
  latest.current = { view, positions };

  const paint = () => {
    const g = graph.current;
    const { view: v, positions: pos } = latest.current;
    const p = palette();
    const keepNodes = new Set(v.nodes.map((n) => n.id));
    g.forEachNode((id) => { if (!keepNodes.has(id)) g.dropNode(id); });
    for (const n of v.nodes) {
      const at = pos.get(n.id) ?? { x: 500, y: 350 };
      const color = n.seedPart !== null ? p.v(`--part-${n.seedPart % 4}`) : p.v(KIND_VAR[n.kind] ?? "--k-other");
      const attrs = { x: at.x, y: -at.y, size: n.seedPart !== null ? 10 : 7, label: n.label.replace(/^Mr\. /, ""), color,
        hidden: false, zIndex: n.state === "dim" ? 0 : 1 };
      if (g.hasNode(n.id)) g.mergeNodeAttributes(n.id, attrs); else g.addNode(n.id, attrs);
      if (n.state === "dim") g.setNodeAttribute(n.id, "color", p.faint);
    }
    const keepEdges = new Set(v.links.map((l) => l.id));
    g.forEachEdge((id) => { if (!keepEdges.has(id)) g.dropEdge(id); });
    for (const l of v.links) {
      if (!g.hasNode(l.source) || !g.hasNode(l.target)) continue;
      const color = l.hot || l.state === "cited" ? p.glow : l.state === "shown" ? (l.part !== null ? p.v(`--part-${l.part % 4}`) : p.edge) : p.faint;
      const attrs = { type: "arrow", label: l.state === "faint" || l.state === "cut" ? "" : l.label, color, size: l.hot ? 4 : l.state === "cited" ? 2.6 : 1.4 };
      if (g.hasEdge(l.id)) g.mergeEdgeAttributes(l.id, attrs); else g.addEdgeWithKey(l.id, l.source, l.target, attrs);
    }
    sigma.current?.setSetting("labelColor", { color: p.ink });
    sigma.current?.setSetting("edgeLabelColor", { color: p.v("--muted") });
    sigma.current?.refresh();
  };

  useEffect(() => {
    sigma.current = new Sigma(graph.current, box.current!, { renderEdgeLabels: true, labelFont: "Public Sans", labelSize: 12, edgeLabelSize: 10,
      defaultEdgeType: "arrow", labelRenderedSizeThreshold: 0 });
    sigma.current.on("clickNode", ({ node }) => click.current(node));
    const obs = new MutationObserver(paint);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", paint);
    return () => { obs.disconnect(); mq.removeEventListener("change", paint); sigma.current?.kill(); sigma.current = null; };
  }, []);
  useEffect(paint, [view, positions]);
  return <div ref={box} className="gcanvas" role="img" aria-label="Entities and relationships used for the answer" />;
}
```

Add to `theme.css`: `.gcanvas{position:absolute;inset:0}`.

`web/src/components/EntityCard.tsx`:

```tsx
import { useEffect, useState } from "react";
import { api, isError } from "../api";
import type { EntityCardData, SubgraphData } from "../types";

export function EntityCard({ id, onClose, onExpand }: { id: string; onClose: () => void; onExpand: (d: SubgraphData) => void }) {
  const [card, setCard] = useState<EntityCardData | null>(null);
  const [problem, setProblem] = useState("");
  const [expanding, setExpanding] = useState(false);
  useEffect(() => { setCard(null); setProblem(""); api.entity(id).then((r) => isError(r) ? setProblem(r.error.message) : setCard(r)); }, [id]);
  const expand = async () => {
    setExpanding(true);
    const r = await api.subgraph(id);
    setExpanding(false);
    if (isError(r)) setProblem(r.error.message); else onExpand(r);
  };
  const rels = card ? [...card.edges_out.map((e) => `${e.predicate.toLowerCase().replace(/_/g, " ")} → ${e.object}`),
    ...card.edges_in.map((e) => `${e.subject} → ${e.predicate.toLowerCase().replace(/_/g, " ")}`)].slice(0, 8) : [];
  return (
    <aside className="card">
      <button className="close" type="button" aria-label="Close" onClick={onClose}>×</button>
      {problem && <p className="note">{problem}</p>}
      {card && (<>
        <span className="kind">{card.kind}</span>
        <h3>{card.name}</h3>
        {card.aliases.length > 0 && <span className="mono">{`Also: ${card.aliases.slice(0, 5).join(", ")}`}</span>}
        <ul>{rels.map((r) => <li key={r}>{r}</li>)}</ul>
        <span className="mono">{`${card.edges_out_total + card.edges_in_total} relationships in the graph`}</span>
        <button className="btn" type="button" disabled={expanding} onClick={expand}>{expanding ? "Expanding…" : "Expand neighbours"}</button>
      </>)}
    </aside>
  );
}
```

`web/src/components/GraphPane.tsx`:

```tsx
import { useEffect, useMemo, useRef, useState } from "react";
import type { AskState } from "../askReducer";
import { deriveGraph } from "../graphView";
import { place, type Positions } from "../layout";
import { STEP_LABEL, UI } from "../messages";
import type { SubgraphData } from "../types";
import { EntityCard } from "./EntityCard";
import { GraphCanvas } from "./GraphCanvas";

export function GraphPane({ state, hiddenBooks, hoverChunk, noBooks }: { state: AskState; hiddenBooks: Set<string>; hoverChunk: string | null; noBooks: boolean }) {
  const [expansions, setExpansions] = useState<{ root: string; data: SubgraphData }[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const positions = useRef<Positions>(new Map());
  const lastQuestion = useRef(state.question);
  if (lastQuestion.current !== state.question) { lastQuestion.current = state.question; positions.current = new Map(); }
  useEffect(() => { setExpansions([]); setSelected(null); }, [state.question]);
  const view = useMemo(() => deriveGraph(state, { hiddenBooks, hoverChunk, expansions: expansions.map((e) => e.data) }), [state, hiddenBooks, hoverChunk, expansions]);
  const pos = useMemo(() => { positions.current = place(positions.current, view, expansions.at(-1)?.root ?? null); return positions.current; }, [view, expansions]);
  const running = state.steps.find((s) => s.state === "running");
  const stage = running ? `${STEP_LABEL[running.name]}…` : state.status === "done" ? `Answered in ${state.elapsed.toFixed(1)}s · cited relationships glow` : "Evidence graph";
  return (
    <section className="pane graphpane" aria-label="Evidence graph">
      <div className="gbar">
        <span className="stage">{stage}</span>
        {view.shownEdges < view.totalEdges && <span>{UI.showingEdges(view.shownEdges, view.totalEdges)}</span>}
        <div className="legend">
          {state.subQuestions.length > 1 ? state.subQuestions.map((q, i) => <span key={q} title={q}><i style={{ background: `var(--part-${i % 4})` }} />{`Part ${i + 1}`}</span>)
            : <span><i style={{ background: "var(--part-0)" }} />Starting entity</span>}
          <span><i style={{ background: "var(--k-character)" }} />Character</span>
          <span><i style={{ background: "var(--k-place)" }} />Place</span>
          <span><i style={{ background: "var(--k-faction)" }} />Group</span>
          <span><i style={{ background: "var(--k-object)" }} />Object</span>
          <span><i style={{ background: "var(--glow)", borderRadius: 2, height: 3, width: 14 }} />Cited</span>
        </div>
      </div>
      <div className="gwrap">
        {noBooks ? <p className="hint" style={{ padding: 16 }}>{UI.noBooks}</p>
          : <GraphCanvas view={view} positions={pos} onNodeClick={setSelected} />}
        {selected && <EntityCard id={selected} onClose={() => setSelected(null)}
          onExpand={(data) => setExpansions((e) => [...e, { root: selected, data }])} />}
      </div>
    </section>
  );
}
```

`expanded` marks the node `deriveGraph` treats as the root, which is the first node. Because `GraphPane` keeps `root` with each expansion, pass `{root}` through if Task 10's root note applies.

`web/src/App.tsx`:

```tsx
import { useEffect, useState } from "react";
import { api, isError } from "./api";
import { ChatPane } from "./components/ChatPane";
import { GraphPane } from "./components/GraphPane";
import { TopBar } from "./components/TopBar";
import { applyTheme, loadTheme } from "./theme";
import type { Book } from "./types";
import { useAsk } from "./useAsk";

export default function App() {
  const ask = useAsk();
  const [books, setBooks] = useState<Book[]>([]);
  const [showInternals, setShowInternals] = useState(true);
  const [hiddenBooks, setHiddenBooks] = useState<Set<string>>(new Set());
  const [hoverChunk, setHoverChunk] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [resetsAt, setResetsAt] = useState<string | undefined>();
  useEffect(() => { applyTheme(loadTheme()); }, []);
  useEffect(() => {
    api.settings().then((s) => { if (!isError(s)) setShowInternals(s.show_internals); });
    api.library().then((l) => { if (!isError(l)) setBooks(l.books); });
  }, []);
  useEffect(() => {
    if (ask.state.status !== "done" && ask.state.status !== "error") return;
    setRefreshKey((k) => k + 1);
    api.quota().then((q) => { if (!isError(q)) setResetsAt(q.resets_at); });
  }, [ask.state.status]);
  const graphed = books.filter((b) => b.status === "graphed");
  const noBooks = graphed.length > 0 && graphed.every((b) => hiddenBooks.has(b.id));
  return (
    <div className="app">
      <TopBar books={books} hiddenBooks={hiddenBooks} onBooksChange={setHiddenBooks} showInternals={showInternals} refreshKey={refreshKey} />
      <div className="panes">
        <ChatPane ask={ask} showInternals={showInternals} resetsAt={resetsAt} onHoverChunk={setHoverChunk} />
        <GraphPane state={ask.state} hiddenBooks={hiddenBooks} hoverChunk={hoverChunk} noBooks={noBooks} />
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Run all frontend tests and the build**

Run: `cd web && npm test && npm run build`
Expected: all tests PASS, the build succeeds (with no TypeScript errors), and `lore/web/static/index.html` exists.

- [ ] **Step 6: Manual check against the live library**

Start Ollama CPU-only, then `.venv/bin/lore ui`, and open http://127.0.0.1:8766. Ask "Where was Joseph Stangerson's body found, and how did he come to be killed?". Expected:
- the steps tick through with timings, and the graph shows two part colours;
- edges fan out, cut ones fade, and cited ones glow amber once the answer lands;
- hovering a claim lights its edges, and a citation opens the passage with the quote marked;
- clicking a node opens its card, and Expand neighbours adds nodes without moving the existing ones;
- the history bar replays the question without a new request (the quota chip doesn't change);
- the books dropdown dims a book, the theme toggle switches both palettes (including the graph colours), and at 400px width the graph stacks above the chat.

Stop the server.

- [ ] **Step 7: Commit**

```bash
git config user.email
git add web/src/ lore/web/static/.gitkeep
git commit -m "feat: chat and graph panes: live steps, cited claims, staged Sigma graph, entity cards"
git push -q origin phase-5a-web-ui
```

---

### Task 14: Opt-in end-to-end test and docs

**Files:**
- Create: `web/playwright.config.ts`, `web/e2e/ask.spec.ts`
- Modify: `README.md` (a "Web UI" section)

**Interfaces:**
- Consumes: the running `lore ui` (built frontend), the live library, Gemini and Ollama.

- [ ] **Step 1: Write the test**

`web/playwright.config.ts`:

```ts
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 180_000,
  use: { baseURL: "http://127.0.0.1:8766", viewport: { width: 1280, height: 800 } },
});
```

`web/e2e/ask.spec.ts`:

```ts
import { expect, test } from "@playwright/test";

// Opt-in: spends ~6 Gemini requests. Needs `lore ui` running on 8766 (built
// frontend), Ollama running CPU-only, and LORE_E2E=1.
test.skip(!process.env.LORE_E2E, "set LORE_E2E=1 to run (spends Gemini requests)");

test("ask, watch, read, explore", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Question").fill("How is Jefferson Hope connected to John Ferrier?");
  await page.getByRole("button", { name: "Ask" }).click();
  await expect(page.locator(".steps li.now").first()).toBeVisible();
  await expect(page.getByText("How this was answered")).toBeVisible({ timeout: 150_000 });
  const cite = page.locator(".cite").first();
  await cite.click();
  await expect(page.locator(".passage mark").first()).toBeVisible();
  await expect(page.locator(".gcanvas canvas").first()).toBeVisible();
});
```

- [ ] **Step 2: Run it once**

```bash
cd web && npx playwright install chromium
# in another terminal, from the worktree root: .venv/bin/lore ui
LORE_E2E=1 npx playwright test
```

Expected: 1 PASS. Without `LORE_E2E`, `npx playwright test` reports 1 skipped.

- [ ] **Step 3: Document**

Add to `README.md`:

````markdown
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
````

- [ ] **Step 4: Full verification**

Run: `.venv/bin/python -m pytest -q tests` (all PASS), then `cd web && npm test && npm run build` (all PASS, build OK).

- [ ] **Step 5: Commit and push**

```bash
git config user.email
git add web/playwright.config.ts web/e2e/ README.md
git commit -m "test: opt-in end-to-end check of the web UI; docs for lore ui"
git push -q origin phase-5a-web-ui
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §3 architecture, `lore ui`, thin layer, deps, static build | Tasks 4, 6, 7 |
| §3.1 endpoints | Tasks 4, 5 |
| §3.2 security | Tasks 3, 5 (JSON-only POST) |
| §4.1 hook | Task 2 |
| §4.2 events | Tasks 2, 5 |
| §4.3 ids | Tasks 1, 4 |
| §4.4 busy, cancel, best-effort | Task 5 |
| §5.1 components | Tasks 12, 13 |
| §5.2 Sigma rendering | Task 13 |
| §5.3 reducer and derived state | Tasks 9, 10 |
| §5.4 history | Tasks 9, 11 |
| §5.5 client | Task 8 |
| §5.6 look | Task 7 |
| §6 errors, message table, `show_internals` | Tasks 3, 4, 11, 12, 13 |
| §7 testing | every task; e2e in Task 14 |
| Done-when | Task 13 step 6, plus Task 14 |

**Placeholder scan:** no TBDs. A few steps tell the implementer to check a name in existing code before writing (the judgement schema classes, the Ollama error code, `LinkOut`'s fields, `entity()`'s cap constant, `TemplateCall` and `Answer` constructors). Each names the file and what to align. Those facts live in code this plan does not restate, and guessing them would be worse.

**Type consistency:** `edgeKey` (TS) and `edge_key` (Py) share one format. `AskEvent` matches the wire format from Task 5 (agent fields minus `type`, plus `t`; `answer.result`, `error.error`). `StepName` and `STEP_ORDER` match the backend names. `HistoryItem`, `AskState`, `GraphView` and `Positions` are used with the same field names across Tasks 9-13.
