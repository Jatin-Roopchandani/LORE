# Phase 4 — MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose LORE's seven read-only tools over the Model Context Protocol, so Claude Code and the Phase 5 UI can both ask questions and explore the graph.

**Architecture:** A thin MCP adapter over the existing query agent — `lore_search` is `answer_question` plus serialization, with no second implementation. Store handles open and close per request so graph rebuilds keep working. A shared SQLite ledger counts every Gemini call, whether it came from the CLI or the server.

**Tech Stack:** Python 3.12, `mcp>=2.2` (MCPServer), Kuzu (read-only), LanceDB, SQLite, pydantic v2, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-09-24-phase-4-mcp-server-design.md`

## Global Constraints

- **Python:** `>=3.12` (existing `requires-python`).
- **New dependency:** `mcp>=2.2` — the class is `MCPServer` from `mcp.server`. **`FastMCP` is the 1.x name and does not exist in 2.x.** Do not write `from mcp.server.fastmcp import FastMCP`.
- **Transports:** stdio and `streamable-http`. Never `sse` — SPEC was amended away from it.
- **Every tool is read-only.** No tool writes to Kuzu, LanceDB, or the books/chunks tables. The only write any tool performs is the Gemini ledger increment.
- **Kuzu is always opened `read_only=True`** in server code.
- **Never gate a commit on a piped pytest.** Capture the real exit code: `pytest ... ; echo "EXIT: $?"`. A `pytest | tail` hides failures — this shipped a broken commit once already.
- **Secrets:** the Gemini key lives in `~/.config/lore/gemini.env` (mode 600). Never print it, never put it in `lore.toml`, never commit it. No test may contain a real key.
- **Tests:** `pytest -m "not live"` must stay green. Baseline before this plan: **1113 passed**.
- **Git:** personal account only — `jatinroopchandani3007@gmail.com`, remote `github-personal`. Verify with `git log -1 --format='%an <%ae>'` after the first commit.

## Review Focus

Five input classes the spec implies but no task's tests exercise on their own. Each line's test is added to the task that owns the code.

1. **A banned token inside a legitimate string literal** — `MATCH (e:Entity) WHERE e.name = 'Set' RETURN e` is a real question about a character and the guard rejects it. The rejection must name the token and say why, so the user can tell a guard from a bug. (Task 4)
2. **`entity(name)` when the name is ambiguous** — the graph carries "Miss Mary Morstan" and "Miss Morstan" as separate nodes. One name can resolve to several ids. Returning an arbitrary one silently is the worst outcome; the tool must say the name was ambiguous and list the candidates. (Task 8)
3. **`subgraph(entity, depth)` with a large depth on a hub node** — Sherlock Holmes at depth 4 walks most of the graph. Depth must be capped, and the cap reported, not silently obeyed. (Task 8)
4. **`lore_search` when Gemini fails for a reason that is not the budget** — every model cooling, a transport error, a malformed response. These raise `GeminiError` subclasses, not a budget condition, and must not be reported as `budget_exhausted`. (Task 10)
5. **Two tool calls in flight at once** — each opens its own store handles and each may increment the ledger. The ledger's `(day, model)` upsert must be atomic, and a second reader must not fail because the first holds the graph. (Tasks 1 and 5)

---

### Task 1: The Gemini ledger

**Files:**
- Modify: `lore/db/sqlite.py:62-63` (append migration 4 to `MIGRATIONS`)
- Create: `lore/orchestrator/gemini_budget.py`
- Test: `tests/test_gemini_budget.py`

**Interfaces:**
- Consumes: `lore.db.sqlite.open_db(path) -> sqlite3.Connection`
- Produces:
  - `today(now: datetime | None = None) -> str` — local date, `YYYY-MM-DD`
  - `record_call(conn, model: str, day: str) -> None`
  - `calls_today(conn, model: str, day: str) -> int`
  - `day_report(conn, day: str) -> list[dict]` — `[{"model", "count", "limit", "remaining"}]`, one per model seen, model-sorted
  - `BudgetExceeded(RuntimeError)`
  - `check_budget(conn, model: str, day: str, headroom: int = 1) -> None` — raises `BudgetExceeded` when `calls_today + headroom > limit`

This mirrors `lore/orchestrator/quota.py` deliberately: same shape, same file neighbourhood, so the two ledgers read alike.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gemini_budget.py
import sqlite3
from datetime import datetime, timezone

import pytest

from lore.db.sqlite import open_db
from lore.orchestrator.gemini_budget import (
    BudgetExceeded, calls_today, check_budget, day_report, record_call, today,
)


@pytest.fixture
def conn(tmp_path):
    connection = open_db(tmp_path / "lore.db")
    yield connection
    connection.close()


def test_today_is_an_iso_date():
    assert today(datetime(2026, 9, 24, 13, 5, tzinfo=timezone.utc)) == "2026-09-24"


def test_unseen_model_has_no_calls(conn):
    assert calls_today(conn, "gemini-3.5-flash-lite", "2026-09-24") == 0


def test_record_call_accumulates(conn):
    for _ in range(3):
        record_call(conn, "gemini-3.5-flash-lite", "2026-09-24")
    assert calls_today(conn, "gemini-3.5-flash-lite", "2026-09-24") == 3


def test_models_and_days_are_counted_apart(conn):
    record_call(conn, "gemini-3.5-flash-lite", "2026-09-24")
    record_call(conn, "gemini-3.1-flash-lite", "2026-09-24")
    record_call(conn, "gemini-3.5-flash-lite", "2026-09-25")
    assert calls_today(conn, "gemini-3.5-flash-lite", "2026-09-24") == 1
    assert calls_today(conn, "gemini-3.1-flash-lite", "2026-09-24") == 1
    assert calls_today(conn, "gemini-3.5-flash-lite", "2026-09-25") == 1


def test_check_budget_passes_under_the_limit(conn):
    check_budget(conn, "gemini-3.5-flash-lite", "2026-09-24")


def test_check_budget_raises_at_the_limit(conn):
    from lore.inference.gemini import limits_for

    limit = limits_for("gemini-3.5-flash-lite").rpd
    conn.execute(
        "INSERT INTO gemini_ledger (day, model, count) VALUES (?, ?, ?)",
        ("2026-09-24", "gemini-3.5-flash-lite", limit),
    )
    conn.commit()
    with pytest.raises(BudgetExceeded) as excinfo:
        check_budget(conn, "gemini-3.5-flash-lite", "2026-09-24")
    assert "gemini-3.5-flash-lite" in str(excinfo.value)
    assert str(limit) in str(excinfo.value)


def test_day_report_lists_each_model_with_remaining(conn):
    record_call(conn, "gemini-3.5-flash-lite", "2026-09-24")
    rows = day_report(conn, "2026-09-24")
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == "gemini-3.5-flash-lite"
    assert row["count"] == 1
    assert row["remaining"] == row["limit"] - 1


# Review Focus 5: concurrent increments must not lose a count.
def test_concurrent_increments_do_not_lose_counts(tmp_path):
    path = tmp_path / "lore.db"
    open_db(path).close()  # migrate once, then open plain connections

    conns = [sqlite3.connect(path) for _ in range(4)]
    try:
        for _ in range(10):
            for c in conns:
                record_call(c, "gemini-3.5-flash-lite", "2026-09-24")
        checker = sqlite3.connect(path)
        try:
            assert calls_today(checker, "gemini-3.5-flash-lite", "2026-09-24") == 40
        finally:
            checker.close()
    finally:
        for c in conns:
            c.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_gemini_budget.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lore.orchestrator.gemini_budget'`

- [ ] **Step 3: Add migration 4**

In `lore/db/sqlite.py`, append to `MIGRATIONS` after the `(3, ...)` entry:

```python
    (
        4,
        [
            """
            CREATE TABLE gemini_ledger (
                id    INTEGER PRIMARY KEY,
                day   TEXT NOT NULL,        -- local date, e.g. 2026-09-24
                model TEXT NOT NULL,        -- gemini-3.5-flash-lite
                count INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE UNIQUE INDEX idx_gemini_ledger_day_model"
            " ON gemini_ledger(day, model)",
        ],
    ),
```

- [ ] **Step 4: Write the budget module**

```python
# lore/orchestrator/gemini_budget.py
"""Daily Gemini request budget, shared by every caller.

Sibling to quota.py, which does the same job for Kaggle GPU-hours. The
point of one ledger is that `lore ask` in a terminal and `lore_search`
over MCP spend the SAME daily allowance: a server-only counter reports a
number that is wrong on any day the CLI was also used, which is most days.

The day is LOCAL, not UTC. Google's free-tier day resets at midnight
Pacific, which this does not model; a local day is the honest
approximation and the report says which day it counted.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from lore.inference.gemini import limits_for


class BudgetExceeded(RuntimeError):
    """This call would exceed the model's free-tier daily allowance."""


def today(now: datetime | None = None) -> str:
    moment = now if now is not None else datetime.now()
    return moment.strftime("%Y-%m-%d")


def record_call(conn: sqlite3.Connection, model: str, day: str) -> None:
    """Count one request against (day, model).

    An upsert, not a read-then-write: two tool calls in flight would
    otherwise both read N and both write N+1, losing a request from the
    count. The UNIQUE index on (day, model) is what makes it atomic.
    """
    with conn:
        conn.execute(
            "INSERT INTO gemini_ledger (day, model, count) VALUES (?, ?, 1)"
            " ON CONFLICT(day, model) DO UPDATE SET count = count + 1",
            (day, model),
        )


def calls_today(conn: sqlite3.Connection, model: str, day: str) -> int:
    row = conn.execute(
        "SELECT count FROM gemini_ledger WHERE day = ? AND model = ?",
        (day, model),
    ).fetchone()
    return int(row[0]) if row else 0


def check_budget(
    conn: sqlite3.Connection, model: str, day: str, headroom: int = 1
) -> None:
    used = calls_today(conn, model, day)
    limit = limits_for(model).rpd
    if used + headroom > limit:
        raise BudgetExceeded(
            f"{model}: {used} of {limit} free-tier requests used on {day}."
            " The allowance resets at midnight Pacific."
        )


def day_report(conn: sqlite3.Connection, day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT model, count FROM gemini_ledger WHERE day = ? ORDER BY model",
        (day,),
    ).fetchall()
    report = []
    for model, count in rows:
        limit = limits_for(model).rpd
        report.append({
            "model": model,
            "count": int(count),
            "limit": limit,
            "remaining": max(0, limit - int(count)),
        })
    return report
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_gemini_budget.py -v ; echo "EXIT: $?"`
Expected: PASS, EXIT: 0

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q -m "not live" ; echo "EXIT: $?"`
Expected: EXIT: 0. The migration is additive, so the existing 1113 must all still pass.

- [ ] **Step 7: Commit**

```bash
git add lore/db/sqlite.py lore/orchestrator/gemini_budget.py tests/test_gemini_budget.py
git commit -m "feat: count every Gemini request against one daily ledger"
git log -1 --format='%an <%ae>'   # must be jatinroopchandani3007@gmail.com
```

---

### Task 2: Wire the ledger into the Gemini client, and show it

**Files:**
- Modify: `lore/inference/gemini.py:239-266` (constructor), `:282-288` (`_generate_one`)
- Modify: `lore/orchestrator/gemini_budget.py` (add `recorder_for`)
- Modify: **all four** `GeminiClient(...)` construction sites:
  - `lore/query/agent.py:185` (inside `make_query_models`'s `remote()`)
  - `lore/graph/adjudicate.py:195`
  - `lore/orchestrator/local.py:226`
  - `lore/cli.py:567` (disambiguate)
- Modify: `lore/cli.py:272-284` (`quota` command)
- Test: `tests/test_gemini_provider.py` (add), `tests/test_cli.py` (add), `tests/test_query_agent.py` (add)

**Interfaces:**
- Consumes: `lore.orchestrator.gemini_budget.record_call`, `day_report`, `today`
- Produces:
  - `GeminiClient(..., recorder: Callable[[str], None] | None = None)` — called with the model id once per HTTP attempt.
  - `recorder_for(sqlite_path: Path) -> Callable[[str], None]`
  - `make_query_models(config, api_key=None, *, ollama=None, gemini=None, recorder=None)` — the new keyword is threaded to `GeminiClient`.

**Why the hook goes here.** `_generate_one` is the single place every Gemini request passes through — extraction, disambiguation, adjudication, `lore ask`, `lore_search`. Counting anywhere else undercounts. It already maintains `self.attempts[model]`; the recorder sits beside that line.

**Why all four sites, not just the MCP one.** The decision was one shared ledger: `lore ask` in a terminal and `lore_search` over MCP spend the same daily allowance. Wiring only the server would produce exactly the MCP-only ledger that was rejected — a number that is wrong on any day the CLI was also used, which is most days. Extraction is the largest spender of all; if it does not count, the guard is decorative.

**Deliberate over-count:** the attempt is recorded *before* the HTTP response arrives, so a transport failure still counts. For a budget guard, over-counting is the safe direction — a request Google rejected with 429 definitely counted against the quota, and we cannot tell the two apart from here.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gemini_provider.py  (append)
def test_recorder_is_called_once_per_attempt():
    seen: list[str] = []
    client = _client_with_responses(         # existing helper in this file
        [_ok("{}"), _ok("{}")],
        recorder=seen.append,
    )
    asyncio.run(client.generate("hi"))
    asyncio.run(client.generate("hi again"))
    assert seen == [client.models[0], client.models[0]]


def test_recorder_counts_each_model_the_cascade_tries():
    seen: list[str] = []
    client = _client_with_responses(
        [_quota_refusal(), _ok("{}")],
        recorder=seen.append,
    )
    asyncio.run(client.generate("hi"))
    assert seen == [client.models[0], client.models[1]]


def test_no_recorder_still_works():
    client = _client_with_responses([_ok("{}")])
    assert asyncio.run(client.generate("hi")) == "{}"
```

```python
# tests/test_query_agent.py  (append)
def test_make_query_models_threads_the_recorder_to_the_client(tmp_path):
    """The CLI's own path must count too, or the shared ledger is a fiction."""
    seen: list[str] = []
    config = _gemini_config(tmp_path)          # selection+synthesis on gemini
    models = make_query_models(config, api_key="k", recorder=seen.append)
    try:
        client = models._gemini_for_test()     # or reach the client the test file already uses
        assert client._recorder is seen.append
    finally:
        asyncio.run(models.aclose())
```

If `QueryModels` exposes no handle on its clients, assert instead that a
`GeminiClient` built by `make_query_models(..., recorder=r)` calls `r` when
its transport is a stub — whichever this test file can already express.
The behaviour under test is "the recorder reaches the client", not the
accessor's name.

```python
# tests/test_cli.py  (append)
def test_quota_shows_the_gemini_section(tmp_path, monkeypatch):
    config = _config_in(tmp_path)          # existing helper in this file
    conn = open_db(config.sqlite_path)
    record_call(conn, "gemini-3.5-flash-lite", today())
    conn.close()

    result = CliRunner().invoke(app, ["quota"])
    assert result.exit_code == 0
    assert "gemini-3.5-flash-lite" in result.output
    assert "1" in result.output


def test_quota_without_gemini_calls_says_so(tmp_path, monkeypatch):
    _config_in(tmp_path)
    result = CliRunner().invoke(app, ["quota"])
    assert result.exit_code == 0
    assert "no Gemini requests" in result.output
```

If `_client_with_responses`, `_ok`, `_quota_refusal`, or `_config_in` do not exist under those names in the current test files, use whatever equivalent helper those files already define and keep the assertions identical. Do not invent a second helper alongside an existing one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_gemini_provider.py -k recorder tests/test_cli.py -k quota -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'recorder'`

- [ ] **Step 3: Add the recorder to the client**

In `lore/inference/gemini.py`, add the parameter to `__init__`:

```python
        now: Callable[[], float] = time.monotonic,
        recorder: Callable[[str], None] | None = None,
    ) -> None:
```

and store it beside the other fields:

```python
        # Called once per HTTP attempt with the model id, so a caller can
        # keep a durable daily count. Deliberately fired BEFORE the response
        # arrives: a 429 spent a request too, and from here a transport
        # failure is indistinguishable from one Google counted.
        self._recorder = recorder
```

In `_generate_one`, immediately after the existing `self.attempts[...]` line:

```python
        self.attempts[model] = self.attempts.get(model, 0) + 1
        if self._recorder is not None:
            self._recorder(model)
```

- [ ] **Step 4: Add `recorder_for`, and wire all four construction sites**

In `lore/orchestrator/gemini_budget.py`:

```python
def recorder_for(sqlite_path: Path) -> Callable[[str], None]:
    """A recorder that writes each attempt to the ledger at `sqlite_path`.

    Opens a connection per call. That is a fraction of a millisecond
    against an HTTP round-trip to Google, and it keeps the recorder safe
    to hand to a client that outlives any one caller's connection --
    a cached handle here would be a use-after-close waiting to happen.
    """
    def record(model: str) -> None:
        conn = sqlite3.connect(sqlite_path)
        try:
            record_call(conn, model, today())
        finally:
            conn.close()
    return record
```

Then give each of the four sites a recorder:

- `lore/query/agent.py:185` — add `recorder: Callable[[str], None] | None = None`
  to `make_query_models`'s keyword-only parameters and pass it through:
  `GeminiClient(api_key, models=models, recorder=recorder)`.
- `lore/graph/adjudicate.py:195` — `GeminiClient(key, models=config.adjudication.models, recorder=recorder_for(config.sqlite_path))`
- `lore/orchestrator/local.py:226` — same, on the `client=` argument.
- `lore/cli.py:567` — same, on the disambiguate client.
- `lore/cli.py` `ask` — pass `recorder=recorder_for(config.sqlite_path)` into
  its `make_query_models(...)` call.

Where a site already has a `LoreConfig` in scope, use `config.sqlite_path`. Do
not open `open_db` for this — the ledger table is created by migration 4, which
the normal CLI paths already run.

- [ ] **Step 5: Add the Gemini section to `lore quota`**

Replace the body of `quota()` in `lore/cli.py`:

```python
def quota() -> None:
    """Show this week's Kaggle GPU-hour budget and today's Gemini requests."""
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        report = week_report(conn, iso_week())
        day = today()
        gemini = day_report(conn, day)
    finally:
        conn.close()
    typer.echo(
        f"{report['week']}: {report['used_hours']:.2f}h used, "
        f"{report['remaining_hours']:.2f}h remaining across {report['runs']} run(s)"
    )
    if not gemini:
        typer.echo(f"{day}: no Gemini requests recorded")
        return
    typer.echo(f"{day}: Gemini requests")
    for row in gemini:
        typer.echo(
            f"  {row['model']}: {row['count']} of {row['limit']} used,"
            f" {row['remaining']} remaining"
        )
```

Add the import beside the existing quota import:

```python
from lore.orchestrator.gemini_budget import day_report, today
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_gemini_provider.py tests/test_cli.py tests/test_query_agent.py -v ; echo "EXIT: $?"`
Expected: PASS, EXIT: 0

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q -m "not live" ; echo "EXIT: $?"`
Expected: EXIT: 0 — four call sites changed, so a regression here is real.

- [ ] **Step 8: Commit**

```bash
git add lore/inference/gemini.py lore/orchestrator/gemini_budget.py \
        lore/query/agent.py lore/graph/adjudicate.py lore/orchestrator/local.py \
        lore/cli.py tests/test_gemini_provider.py tests/test_cli.py \
        tests/test_query_agent.py
git commit -m "feat: record every Gemini attempt, from the CLI as well as MCP"
```

---

### Task 3: Widen `Answer` so the structure survives

**Files:**
- Modify: `lore/query/models.py` (move `Claim` in, add three fields to `Answer`)
- Modify: `lore/query/synthesize.py:115-118` (import `Claim` instead of defining it)
- Modify: `lore/query/agent.py` (populate the new fields)
- Test: `tests/test_query_agent.py` (add), `tests/test_synthesize.py` (add)

**Interfaces:**
- Produces: `Answer.claims: tuple[Claim, ...]`, `Answer.edges: tuple[EdgeRow, ...]`, `Answer.seeds: tuple[SeedEntity, ...]` — all defaulting to `()`.
- `Claim` is now importable from `lore.query.models`. `lore.query.synthesize.Claim` keeps working as a re-export so nothing else breaks.

**Why.** `Claim{text, passage, quote}` is quote-checked and faithfulness-judged, then `_compose` (`synthesize.py:283`) joins the claims into one prose string and the structure is gone. `Evidence.edges` never reaches `Answer` at all. The UI has nothing to draw. These three fields are the whole reason Phase 5 can render a subgraph.

**`claims` carries survivors only.** Unsupported claims are dropped before composition, so every claim reaching `Answer` has passed both checks; the verdict is implicit, not a field. When the check could not run, the existing `note` already says so (`CHECK_SKIPPED_NOTE`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_query_agent.py  (append)
@pytest.mark.asyncio
async def test_answer_carries_the_claims_it_composed(fake_stores, fake_models):
    answer = await answer_question(
        fake_stores.conn, fake_stores.kuzu, fake_stores.lance,
        "Who killed Enoch Drebber?", fake_models,
    )
    assert answer.claims, "the composed claims must survive onto Answer"
    for claim in answer.claims:
        assert claim.quote
        assert claim.passage in {p.n for p in answer.passages}


@pytest.mark.asyncio
async def test_answer_carries_the_evidence_edges(fake_stores, fake_models):
    answer = await answer_question(
        fake_stores.conn, fake_stores.kuzu, fake_stores.lance,
        "Who killed Enoch Drebber?", fake_models,
    )
    assert answer.edges, "the subgraph the UI draws must survive onto Answer"
    assert all(edge.subject and edge.object for edge in answer.edges)


@pytest.mark.asyncio
async def test_answer_carries_the_seeds(fake_stores, fake_models):
    answer = await answer_question(
        fake_stores.conn, fake_stores.kuzu, fake_stores.lance,
        "Who killed Enoch Drebber?", fake_models,
    )
    assert answer.seeds
    assert all(seed.id and seed.name for seed in answer.seeds)


def test_answer_defaults_keep_old_constructions_working():
    answer = Answer(text="x", passages=(), mode="empty", template=None, note=None)
    assert answer.claims == ()
    assert answer.edges == ()
    assert answer.seeds == ()
```

```python
# tests/test_synthesize.py  (append)
def test_claim_is_importable_from_both_modules():
    from lore.query.models import Claim as FromModels
    from lore.query.synthesize import Claim as FromSynthesize

    assert FromModels is FromSynthesize
```

Use the fixtures those test files already define. If `fake_stores` / `fake_models` are named differently there, use the existing names — do not add parallel fixtures.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_query_agent.py -k "claims or edges or seeds or defaults" tests/test_synthesize.py -k importable -v`
Expected: FAIL — `AttributeError: 'Answer' object has no attribute 'claims'`

- [ ] **Step 3: Move `Claim` into models.py**

In `lore/query/models.py`, add the import and the class above `Answer`:

```python
from pydantic import BaseModel


class Claim(BaseModel):
    """One sentence of an answer, with the passage it came from and a
    word-for-word quote from that passage. The quote is verified in code,
    then judged against its passage by the faithfulness pass."""

    text: str
    passage: int
    quote: str
```

Add the three fields to `Answer`, after `sub_questions`:

```python
    # Task 3 (Phase 4): the structure the UI draws. Built during synthesis
    # and, before this, discarded -- _compose flattened the claims to prose
    # and the evidence edges never reached here at all.
    #
    # `claims` holds SURVIVORS only: unsupported claims are dropped before
    # composition, so a claim's presence is the verdict. When the check
    # could not run, `note` says so (CHECK_SKIPPED_NOTE).
    claims: tuple[Claim, ...] = ()
    edges: tuple[EdgeRow, ...] = ()
    seeds: tuple[SeedEntity, ...] = ()
```

- [ ] **Step 4: Re-export from synthesize.py**

In `lore/query/synthesize.py`, delete the `class Claim(BaseModel)` definition at line 115 and import it instead, beside the other `lore.query.models` imports:

```python
from lore.query.models import Claim, EdgeRow, Evidence, Passage
```

Keep `Claim` in that import line even if this module does not reference it directly — `from lore.query.synthesize import Claim` is used elsewhere and must keep working.

- [ ] **Step 5: Populate the fields in agent.py**

The three fields are set in two different places, because two different layers know them.

**`claims` is set in `synthesize.py`,** where the surviving claims exist. `synthesize()` and `synthesize_parts()` already build the `Answer` they return, and `_compose(claims)` is called right there — so attach the same list:

```python
    # inside synthesize() / synthesize_parts(), where _compose is called
    return Answer(
        text=_compose(kept),
        ...,
        claims=tuple(kept),      # the SAME list _compose just flattened
    )
```

Use the post-check list, not the pre-check one — `claims` carries survivors only.

**`edges` and `seeds` are set in `agent.py`,** where the evidence and the seeds are in hand. `_synthesize` returns an `Answer`, so attach them with `replace` at the call site rather than threading two more parameters through:

```python
from dataclasses import replace

answer = await _synthesize(question, evidence, models, ...)
answer = replace(answer, edges=evidence.edges, seeds=tuple(seeds))
```

Do **not** populate `edges`, `seeds`, or `claims` on the `vector-baseline` path — there is no graph evidence and no claim structure there, and `()` is the honest value. `_vector_answer` therefore stays exactly as it is.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_query_agent.py tests/test_synthesize.py -v ; echo "EXIT: $?"`
Expected: PASS, EXIT: 0

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q -m "not live" ; echo "EXIT: $?"`
Expected: EXIT: 0. This task touches Phase 3 code that 1113 tests cover — a regression here is a real regression, not a stale test.

- [ ] **Step 8: Commit**

```bash
git add lore/query/models.py lore/query/synthesize.py lore/query/agent.py tests/test_query_agent.py tests/test_synthesize.py
git commit -m "feat: keep the claims and edges an answer was built from"
```

---

### Task 4: Cypher guards

**Files:**
- Create: `lore/mcp/__init__.py` (empty), `lore/mcp/guards.py`
- Test: `tests/test_mcp_guards.py`

**Interfaces:**
- Produces:
  - `CypherRejected(ValueError)` — carries `.reason: str`
  - `validate_cypher(query: str) -> None` — raises `CypherRejected`, returns `None` when allowed
  - `ROW_CAP: int = 200`, `TIMEOUT_MS: int = 5000`
  - `run_guarded(kuzu, query: str, row_cap: int = ROW_CAP) -> tuple[list[str], list[list], bool]` — `(columns, rows, truncated)`

**This is the security control, not a nicety.** Measured on a `read_only=True` connection: `CREATE`, `SET`, and `DETACH DELETE` are all refused by the engine — but `COPY (MATCH (e:Entity) RETURN e.id, e.name) TO '/tmp/leak.csv'` **wrote the file**, and `LOAD FROM '/etc/hostname' (file_format='csv') RETURN *` **returned the file's contents as rows**. Kuzu's read-only mode protects the database, not the filesystem. Without this guard, `cypher_query` over the HTTP transport is an arbitrary file read and write primitive.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_guards.py
import pytest

from lore.mcp.guards import ROW_CAP, CypherRejected, run_guarded, validate_cypher


@pytest.mark.parametrize("query", [
    "MATCH (e:Entity) RETURN e.name LIMIT 5",
    "match (e:Entity) return e.name",
    "  OPTIONAL MATCH (a:Entity)-[r:REL]->(b:Entity) RETURN a.name, b.name",
    "WITH 1 AS x RETURN x",
    "UNWIND [1, 2, 3] AS n RETURN n",
    "RETURN 1",
    "CALL show_tables() RETURN *",
    "CALL table_info('REL') RETURN *",
    "CALL db_version() RETURN *",
])
def test_read_queries_are_allowed(query):
    validate_cypher(query)


# These two are the reason this module exists. Both SUCCEED against a
# read-only Kuzu connection; only this guard stops them.
def test_copy_to_a_file_is_rejected():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("COPY (MATCH (e:Entity) RETURN e.id, e.name) TO '/tmp/leak.csv'")
    assert "COPY" in str(excinfo.value)


def test_load_from_a_file_is_rejected():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("LOAD FROM '/etc/hostname' (file_format='csv') RETURN *")
    assert "LOAD" in str(excinfo.value)


@pytest.mark.parametrize("query", [
    "CREATE (n:Entity {id: 'x'})",
    "MATCH (e:Entity) SET e.name = 'x'",
    "MATCH (e:Entity) DETACH DELETE e",
    "MERGE (n:Entity {id: 'x'})",
    "DROP TABLE Entity",
    "ALTER TABLE REL ADD junk STRING",
    "ATTACH '/tmp/other.kz' AS other (dbtype kuzu)",
    "INSTALL httpfs",
    "EXPORT DATABASE '/tmp/dump'",
    "IMPORT DATABASE '/tmp/dump'",
])
def test_write_and_io_statements_are_rejected(query):
    with pytest.raises(CypherRejected):
        validate_cypher(query)


def test_an_unlisted_call_is_rejected():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("CALL some_other_procedure() RETURN *")
    assert "some_other_procedure" in str(excinfo.value)


def test_two_statements_are_rejected():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("MATCH (e:Entity) RETURN e.name; DROP TABLE Entity")
    assert "one statement" in str(excinfo.value).lower()


def test_a_trailing_semicolon_is_fine():
    validate_cypher("MATCH (e:Entity) RETURN e.name;")


def test_an_empty_query_is_rejected():
    with pytest.raises(CypherRejected):
        validate_cypher("   ")


# Review Focus 1: a banned token inside a legitimate string literal.
# This is a REAL question -- 'Set' could be a character name -- and the
# guard rejects it on purpose. The message must make that obvious, so the
# user can tell a guard from a bug.
def test_a_banned_word_in_a_string_literal_is_rejected_with_a_clear_reason():
    with pytest.raises(CypherRejected) as excinfo:
        validate_cypher("MATCH (e:Entity) WHERE e.name = 'Set' RETURN e.id")
    message = str(excinfo.value)
    assert "SET" in message
    assert "string literal" in message.lower() or "even inside" in message.lower()


def test_row_cap_truncates_and_says_so(live_kuzu):
    columns, rows, truncated = run_guarded(
        live_kuzu, "MATCH (a:Entity), (b:Entity) RETURN a.id, b.id", row_cap=10
    )
    assert columns == ["a.id", "b.id"]
    assert len(rows) == 10
    assert truncated is True


def test_a_small_result_is_not_truncated(live_kuzu):
    columns, rows, truncated = run_guarded(
        live_kuzu, "MATCH (e:Entity) RETURN e.id LIMIT 3"
    )
    assert len(rows) <= 3
    assert truncated is False
```

`live_kuzu` is a fixture giving a small in-memory-ish Kuzu store with a handful of `Entity` nodes. Build it with the helpers already in `tests/fixtures.py` — the same way `tests/test_loader.py` builds its graph.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_guards.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lore.mcp'`

- [ ] **Step 3: Write the guard module**

```python
# lore/mcp/guards.py
"""What a client may send to `cypher_query`, and what it gets back.

Kuzu's read-only mode protects the DATABASE, not the FILESYSTEM. Measured
on a connection opened read_only=True:

    CREATE / SET / DETACH DELETE   -> refused by the engine
    COPY (MATCH ...) TO '<path>'   -> ALLOWED, wrote the file
    LOAD FROM '<path>' (...)       -> ALLOWED, returned the file as rows

So the read-only connection is the backstop and this module is the actual
control. Without it, `cypher_query` reachable over HTTP is an arbitrary
file read and write primitive.

The rules fail closed. A rejected legitimate query is a clear error the
user can read; an accepted malicious one is not recoverable.
"""
from __future__ import annotations

import re

ROW_CAP = 200
TIMEOUT_MS = 5000

_ALLOWED_OPENINGS = ("OPTIONAL MATCH", "MATCH", "RETURN", "WITH", "UNWIND", "CALL")
_ALLOWED_CALLS = frozenset({"show_tables", "table_info", "db_version"})
_BANNED = (
    "COPY", "LOAD", "EXPORT", "IMPORT", "ATTACH", "INSTALL",
    "CREATE", "MERGE", "SET", "DELETE", "DROP", "ALTER",
)
_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_CALL_NAME = re.compile(r"^CALL\s+([A-Za-z_][A-Za-z_0-9]*)", re.IGNORECASE)


class CypherRejected(ValueError):
    """The query did not pass the read-only guard."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_cypher(query: str) -> None:
    """Raise CypherRejected unless `query` is a single read-only statement."""
    text = query.strip().rstrip(";").strip()
    if not text:
        raise CypherRejected("empty query")

    if ";" in text:
        raise CypherRejected(
            "only one statement per query: a ';' joining two statements is"
            " rejected, because vetting the first would say nothing about the"
            " second"
        )

    # Whole words, INCLUDING inside string literals. A query about a
    # character named 'Set' is rejected on purpose: the alternative is
    # trusting a hand-written string-literal parser to agree with Kuzu's,
    # and a disagreement there is exactly the gap an injection uses.
    words = {w.upper() for w in _WORD.findall(text)}
    for banned in _BANNED:
        if banned in words:
            raise CypherRejected(
                f"'{banned}' is not allowed in a cypher_query. This is matched"
                " as a whole word even inside a string literal, so a legitimate"
                f" query mentioning '{banned.title()}' is rejected too -- use"
                " the `entity` or `subgraph` tool for that instead."
            )

    upper = text.upper()
    opening = next((o for o in _ALLOWED_OPENINGS if upper.startswith(o)), None)
    if opening is None:
        first = text.split(None, 1)[0]
        raise CypherRejected(
            f"a query must begin with one of"
            f" {', '.join(_ALLOWED_OPENINGS)} -- got '{first}'"
        )

    if opening == "CALL":
        match = _CALL_NAME.match(text)
        name = match.group(1) if match else ""
        if name not in _ALLOWED_CALLS:
            raise CypherRejected(
                f"CALL {name or '?'} is not allowed; the permitted procedures"
                f" are {', '.join(sorted(_ALLOWED_CALLS))}"
            )


def run_guarded(kuzu, query: str, row_cap: int = ROW_CAP):
    """Validate, run, and read at most `row_cap` rows.

    Returns (columns, rows, truncated).

    The row cap is the real resource control, not the timeout: Kuzu
    streams, so `MATCH (a:Entity),(b:Entity),(c:Entity) RETURN ...` --
    175,616,000 rows on the live graph -- returns its first rows in 60ms,
    and reading 200 then stopping costs nothing.
    """
    validate_cypher(query)
    result = kuzu.execute(query)
    columns = list(result.get_column_names())
    rows: list[list] = []
    while result.has_next():
        if len(rows) >= row_cap:
            return columns, rows, True
        rows.append(list(result.get_next()))
    return columns, rows, False
```

`KuzuStore.execute` returns Kuzu's `QueryResult`, which has `get_column_names()`, `has_next()`, and `get_next()`. Confirm those names against the installed `kuzu` version before assuming; if they differ, adapt `run_guarded` and leave the tests unchanged.

- [ ] **Step 4: Set the query timeout where the connection is made**

`run_guarded` does not set the timeout — the connection does, once, in Task 5's `stores.py`. Note in a comment here that `TIMEOUT_MS` is applied there, so a reader does not go looking for it in this file.

The timeout is **insurance, not a demonstrated control**: it could not be made to fire on this corpus. 560 entities is too small to build a slow query, and Kuzu's optimizer folds `count(*)` over a triple product into arithmetic (30 ms). It is here for a larger library.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_guards.py -v ; echo "EXIT: $?"`
Expected: PASS, EXIT: 0

- [ ] **Step 6: Commit**

```bash
git add lore/mcp/__init__.py lore/mcp/guards.py tests/test_mcp_guards.py
git commit -m "feat: guard cypher_query, because read-only does not stop COPY TO"
```

---

### Task 5: Per-request store handles

**Files:**
- Create: `lore/mcp/stores.py`
- Test: `tests/test_mcp_stores.py`

**Interfaces:**
- Consumes: `lore.config.LoreConfig`, `lore.db.kuzu_store.KuzuStore`, `lore.db.lance_store.LanceStore`, `lore.db.sqlite.open_db`, `lore.mcp.guards.TIMEOUT_MS`
- Produces:
  - `StoresMissing(RuntimeError)`
  - `open_stores(config: LoreConfig) -> ContextManager[Stores]` where `Stores` is a frozen dataclass `(conn, kuzu, lance)`

**Why per-request, measured not assumed.** A handle held open makes `lore graph load` fail outright with `IO exception: Could not set lock on file`. A second *reader* opens fine. Opening read-only costs 21 ms median (93 ms max over 10 runs); LanceDB connect costs 5 ms. Against a `lore_search` that spends seconds in Gemini, that is free — and it means rebuilding the graph while the server runs keeps working.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_stores.py
import pytest

from lore.db.kuzu_store import KuzuStore
from lore.mcp.stores import StoresMissing, open_stores


def test_open_stores_yields_all_three(live_config):
    with open_stores(live_config) as stores:
        assert stores.conn is not None
        assert list(stores.kuzu.execute("MATCH (e:Entity) RETURN count(e)"))
        assert stores.lance.table_names()


def test_stores_are_closed_on_exit(live_config):
    with open_stores(live_config) as stores:
        kuzu = stores.kuzu
    # The graph lock must be released: a writer can open it afterwards.
    KuzuStore(live_config.kuzu_dir / "graph").close()


def test_missing_graph_raises_stores_missing(tmp_path):
    from lore.config import LoreConfig

    config = LoreConfig(data_dir=tmp_path)     # or the file's existing helper
    with pytest.raises(StoresMissing):
        with open_stores(config):
            pass


def test_the_graph_is_opened_read_only(live_config):
    with open_stores(live_config) as stores:
        with pytest.raises(RuntimeError, match="read-only"):
            stores.kuzu.execute("CREATE (n:Entity {id: 'x', name: 'x'})")


# Review Focus 5: two requests in flight at once.
def test_two_open_stores_coexist(live_config):
    with open_stores(live_config) as first:
        with open_stores(live_config) as second:
            assert list(first.kuzu.execute("MATCH (e:Entity) RETURN count(e)"))
            assert list(second.kuzu.execute("MATCH (e:Entity) RETURN count(e)"))


# This is the regression guard for the whole design decision.
def test_a_writer_can_open_the_graph_between_requests(live_config):
    with open_stores(live_config):
        pass
    writer = KuzuStore(live_config.kuzu_dir / "graph")
    writer.close()
```

`live_config` is a fixture building a small but real store triple (Kuzu graph with a few entities, a LanceDB dir, a migrated SQLite file) under `tmp_path`. Build it from `tests/fixtures.py`. If `KuzuStore` has no `close()` method, add one in this task — a context manager that never releases its handle defeats the entire point — and give it a test here.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_stores.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lore.mcp.stores'`

- [ ] **Step 3: Write the stores module**

```python
# lore/mcp/stores.py
"""One request, one set of store handles.

Measured, not assumed: a handle held open across requests makes
`lore graph load` fail with "IO exception: Could not set lock on file",
while a second READER opens fine. Opening read-only costs 21 ms median
against a lore_search that spends seconds in Gemini.

So the server opens per request and closes after, and rebuilding the graph
while the server is running keeps working. Do not "optimise" this into a
cached handle.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.db.sqlite import open_db
from lore.mcp.guards import TIMEOUT_MS


class StoresMissing(RuntimeError):
    """The library has not been built yet."""


@dataclass(frozen=True)
class Stores:
    conn: sqlite3.Connection
    kuzu: KuzuStore
    lance: LanceStore


@contextmanager
def open_stores(config: LoreConfig) -> Iterator[Stores]:
    """Open the three stores read-only, yield them, and close them.

    The same existence check `lore ask` makes (cli.py:692): a question must
    not create, migrate, or touch the library.
    """
    graph_path = config.kuzu_dir / "graph"
    if not (graph_path.exists()
            and config.sqlite_path.is_file()
            and config.lancedb_dir.is_dir()):
        raise StoresMissing(
            "no graph yet -- run `lore ingest`, `lore process`, then"
            " `lore graph load` before querying"
        )

    conn = None
    kuzu = None
    try:
        conn = sqlite3.connect(config.sqlite_path)
        kuzu = KuzuStore(graph_path, read_only=True)
        kuzu.set_query_timeout(TIMEOUT_MS)
        lance = LanceStore(config.lancedb_dir)
        yield Stores(conn=conn, kuzu=kuzu, lance=lance)
    finally:
        if kuzu is not None:
            kuzu.close()
        if conn is not None:
            conn.close()
```

Note `sqlite3.connect`, not `open_db`: `open_db` runs migrations, and a read-only tool must not migrate the user's library. The ledger's migration is applied by the CLI paths that already call `open_db`.

Add `close()` and `set_query_timeout(ms)` to `KuzuStore` in `lore/db/kuzu_store.py`:

```python
    def set_query_timeout(self, timeout_ms: int) -> None:
        self._conn.set_query_timeout(timeout_ms)

    def close(self) -> None:
        """Release the file lock, so a writer can open the graph again."""
        self._conn.close()
        self._db.close()
```

If `kuzu.Database` has no `close()` in the installed version, drop `self._db.close()` and delete `self._db` instead, then re-run `test_a_writer_can_open_the_graph_between_requests` — that test is the arbiter, not the API's shape.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_stores.py -v ; echo "EXIT: $?"`
Expected: PASS, EXIT: 0

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q -m "not live" ; echo "EXIT: $?"`
Expected: EXIT: 0 — `KuzuStore` gained methods, so its own tests must still pass.

- [ ] **Step 6: Commit**

```bash
git add lore/mcp/stores.py lore/db/kuzu_store.py tests/test_mcp_stores.py
git commit -m "feat: open store handles per request, so rebuilds still work"
```

---

### Task 6: The JSON shapes

**Files:**
- Create: `lore/mcp/shapes.py`
- Test: `tests/test_mcp_shapes.py`

**Interfaces:**
- Produces (all pydantic `BaseModel`):
  - `ToolError(code: str, message: str)` and `ErrorEnvelope(error: ToolError)`
  - `error(code: str, message: str) -> dict` — the envelope as a plain dict
  - `PassageOut(n, chunk_id, book_id, book_title, chapter_seq, text)`
  - `ClaimOut(text, passage, quote)`
  - `EdgeOut(subject, predicate, predicate_raw, object, evidence, confidence, book_id, chapter_seq, chunk_id)`
  - `NodeOut(id, name, kind, books)`
  - `SearchResult(answer, mode, template, note, model, selection_model, sub_questions, claims, passages, edges, seeds)`
  - `ChunkHit(chunk_id, book_id, book_title, chapter_seq, text, distance)`
  - `CypherResult(columns, rows, truncated)`
  - `EntityCard(id, name, kind, aliases, books, edges_out, edges_in)`
  - `SubgraphOut(nodes, edges, truncated, depth, note)` — `depth` is the depth actually walked after clamping, `note` explains a clamp
  - `BookOut(id, title, author, status, chunk_count, entity_count)`
  - `IngestStatus(book_id, title, status, jobs, dead_letters)`
  - `to_search_result(answer: Answer, titles: dict[str, str]) -> SearchResult`

The error codes are fixed: `no_graph`, `budget_exhausted`, `cypher_rejected`, `not_found`, `timeout`. No tool raises through the protocol; each returns an envelope so a client can branch on the code and a human can read the message.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_shapes.py
from lore.mcp.shapes import (
    ERROR_CODES, SearchResult, error, to_search_result,
)
from lore.query.models import Answer, Claim, EdgeRow, Passage, SeedEntity


def test_error_envelope_shape():
    payload = error("not_found", "no entity named 'Nobody'")
    assert payload == {"error": {"code": "not_found",
                                 "message": "no entity named 'Nobody'"}}


def test_every_code_is_declared():
    assert ERROR_CODES == frozenset(
        {"no_graph", "budget_exhausted", "cypher_rejected", "not_found", "timeout"}
    )


def _answer() -> Answer:
    return Answer(
        text="Jefferson Hope killed Enoch Drebber.",
        passages=(Passage(n=1, chunk_id="c1", book_id="b1", chapter_seq=3,
                          text="..."),),
        mode="graph",
        template="two_hop",
        note=None,
        model="gemini-3.5-flash-lite",
        selection_model="gemini-3.5-flash-lite",
        sub_questions=("Who killed Enoch Drebber?",),
        claims=(Claim(text="Jefferson Hope killed Enoch Drebber.",
                      passage=1, quote="Hope"),),
        edges=(EdgeRow(subject="Jefferson Hope", predicate="KILLS",
                       predicate_raw="killed", object="Enoch Drebber",
                       evidence="...", confidence=0.9, book_id="b1",
                       chapter_seq=3, chunk_id="c1"),),
        seeds=(SeedEntity(id="e1", name="Mr. Jefferson Hope", kind="Character",
                          books=("b1",), score=0.5),),
    )


def test_to_search_result_carries_the_structure():
    result = to_search_result(_answer(), {"b1": "A Study in Scarlet"})
    assert isinstance(result, SearchResult)
    assert result.answer.startswith("Jefferson Hope")
    assert result.mode == "graph"
    assert [c.quote for c in result.claims] == ["Hope"]
    assert [e.predicate for e in result.edges] == ["KILLS"]
    assert [s.name for s in result.seeds] == ["Mr. Jefferson Hope"]


def test_book_titles_are_resolved_for_the_reader():
    result = to_search_result(_answer(), {"b1": "A Study in Scarlet"})
    assert result.passages[0].book_title == "A Study in Scarlet"


def test_an_unknown_book_id_falls_back_to_the_id():
    result = to_search_result(_answer(), {})
    assert result.passages[0].book_title == "b1"


def test_a_baseline_answer_serialises_with_empty_structure():
    answer = Answer(text="...", passages=(), mode="vector-baseline",
                    template=None, note=None)
    result = to_search_result(answer, {})
    assert result.claims == []
    assert result.edges == []
    assert result.seeds == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_shapes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lore.mcp.shapes'`

- [ ] **Step 3: Write the shapes module**

Define every model listed in **Interfaces** above as a pydantic `BaseModel` with exactly those field names. `to_search_result` maps an `Answer` field-for-field, resolving `book_id` to `book_title` through the `titles` dict and falling back to the raw id when the book is unknown.

```python
# lore/mcp/shapes.py  (opening)
"""The JSON every tool returns.

Structured because the Phase 5 UI is a first-class consumer: it draws the
subgraph and makes each claim clickable, so prose it would have to re-parse
is not good enough.

No tool raises through the protocol. Each returns {"error": {...}} with one
of ERROR_CODES, so a client branches on the code and a human reads the
message.
"""
from __future__ import annotations

from pydantic import BaseModel

from lore.query.models import Answer

ERROR_CODES = frozenset(
    {"no_graph", "budget_exhausted", "cypher_rejected", "not_found", "timeout"}
)


class ToolError(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    error: ToolError


def error(code: str, message: str) -> dict:
    assert code in ERROR_CODES, f"undeclared error code: {code}"
    return ErrorEnvelope(error=ToolError(code=code, message=message)).model_dump()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_shapes.py -v ; echo "EXIT: $?"`
Expected: PASS, EXIT: 0

- [ ] **Step 5: Commit**

```bash
git add lore/mcp/shapes.py tests/test_mcp_shapes.py
git commit -m "feat: give every MCP tool a structured shape to return"
```

---

### Task 7: The library tools

**Files:**
- Create: `lore/mcp/tools.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: `open_stores`, `StoresMissing`, the shapes from Task 6, `lore.ingest.registry.list_books`
- Produces:
  - `library(config: LoreConfig) -> dict`
  - `ingest_status(config: LoreConfig, book_id: str) -> dict`

Both are plain functions taking a `LoreConfig`, so they are testable without a server. Task 11 decorates them.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_tools.py
import pytest

from lore.mcp.tools import ingest_status, library


def test_library_lists_books_with_counts(live_config):
    result = library(live_config)
    assert result["books"]
    book = result["books"][0]
    assert set(book) >= {"id", "title", "author", "status",
                         "chunk_count", "entity_count"}


def test_library_without_a_graph_returns_no_graph(empty_config):
    assert library(empty_config)["error"]["code"] == "no_graph"


def test_ingest_status_reports_jobs(live_config, a_book_id):
    result = ingest_status(live_config, a_book_id)
    assert result["book_id"] == a_book_id
    assert "jobs" in result
    assert "dead_letters" in result


def test_ingest_status_for_an_unknown_book(live_config):
    result = ingest_status(live_config, "no-such-book")
    assert result["error"]["code"] == "not_found"
    assert "no-such-book" in result["error"]["message"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lore.mcp.tools'`

- [ ] **Step 3: Implement both tools**

```python
@_guarded
def library(config: LoreConfig) -> dict:
    with open_stores(config) as stores:
        books = list_books(stores.conn)
        # One query, not one per book: the entity count per book id.
        counts: dict[str, int] = {}
        for row in stores.kuzu.execute(
            "MATCH (e:Entity) UNWIND e.books AS book RETURN book, count(e)"
        ):
            counts[row[0]] = int(row[1])
        return {"books": [
            BookOut(id=b["id"], title=b["title"], author=b["author"],
                    status=b["status"], chunk_count=b["chunk_count"],
                    entity_count=counts.get(b["id"], 0)).model_dump()
            for b in books
        ]}


@_guarded
def ingest_status(config: LoreConfig, book_id: str) -> dict:
    with open_stores(config) as stores:
        books = {b["id"]: b for b in list_books(stores.conn)}
        if book_id not in books:
            return error("not_found", f"no book with id {book_id!r}")
        jobs = [
            {"kind": r[0], "state": r[1], "detail": r[2], "updated_at": r[3]}
            for r in stores.conn.execute(
                "SELECT kind, state, detail, updated_at FROM jobs"
                " WHERE book_id = ? ORDER BY updated_at",
                (book_id,),
            )
        ]
        dead = [j for j in jobs if j["state"] == "dead"]
        return IngestStatus(book_id=book_id, title=books[book_id]["title"],
                            status=books[book_id]["status"], jobs=jobs,
                            dead_letters=dead).model_dump()
```

Confirm the `jobs` table's state value for a dead letter against
`lore/orchestrator/run.py` before writing `== "dead"` — use whatever string
that module actually sets, not this guess.

Wrap both bodies so `StoresMissing` becomes `error("no_graph", str(exc))`. Put that wrapping in one decorator in this module — every later tool needs it too, and repeating the try/except seven times is how one of them ends up different.

```python
def _guarded(fn):
    """Turn StoresMissing into the no_graph envelope, for every tool."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StoresMissing as exc:
            return error("no_graph", str(exc))
    return wrapper
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -v ; echo "EXIT: $?"`
Expected: PASS, EXIT: 0

- [ ] **Step 5: Commit**

```bash
git add lore/mcp/tools.py tests/test_mcp_tools.py
git commit -m "feat: add the library and ingest_status tools"
```

---

### Task 8: The graph tools

**Files:**
- Modify: `lore/mcp/tools.py`
- Test: `tests/test_mcp_tools.py` (add)

**Interfaces:**
- Consumes: `lore.query.seed.load_name_index`, `named_entity_ids`, `canonical_ids`, `live_entity_ids`
- Produces:
  - `entity(config: LoreConfig, name: str) -> dict`
  - `subgraph(config: LoreConfig, entity_name: str, depth: int = 1) -> dict`
  - `_resolve_one(kuzu, name: str) -> str` — the shared name→canonical-id step, raising `_Ambiguous` or `_NotFound`
  - `_entity_card(kuzu, entity_id: str) -> EntityCard` — reads the entity row plus its outgoing and incoming `REL` edges, each ordered by `(book_id, chapter_seq, chunk_id)` so the card is stable between runs
  - `_expand(kuzu, root: str, depth: int, node_cap: int) -> tuple[list[NodeOut], list[EdgeOut], bool]`
  - `MAX_DEPTH: int = 3`, `SUBGRAPH_NODE_CAP: int = 300`

The parameter is `entity_name`, not `entity`, because `entity` is a function in this same module and shadowing it inside `subgraph` is how the shared resolver accidentally stops being called.

`entity` resolves names through `NameIndex` and `canonical_ids` — the same path the query agent uses — so an alias finds its canonical identically. Do not write a second name-matching rule.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_tools.py  (append)
def test_entity_returns_a_card(live_config):
    result = entity(live_config, "Sherlock Holmes")
    assert result["name"]
    assert result["kind"]
    assert "aliases" in result and "books" in result
    assert "edges_out" in result and "edges_in" in result


def test_entity_resolves_an_alias_to_its_canonical(live_config, an_alias_and_canonical):
    alias, canonical = an_alias_and_canonical
    assert entity(live_config, alias)["name"] == canonical


def test_entity_not_found(live_config):
    result = entity(live_config, "Nobody At All")
    assert result["error"]["code"] == "not_found"


# Review Focus 2: one name, several entities. The graph really does carry
# "Miss Mary Morstan" and "Miss Morstan" as separate nodes. Picking one
# silently is the worst outcome.
def test_an_ambiguous_name_lists_the_candidates(live_config, an_ambiguous_name):
    result = entity(live_config, an_ambiguous_name)
    assert result["error"]["code"] == "not_found"
    assert "ambiguous" in result["error"]["message"].lower()
    assert result["error"]["message"].count(",") >= 1  # the candidates


def test_subgraph_returns_nodes_and_edges(live_config):
    result = subgraph(live_config, "Sherlock Holmes", depth=1)
    assert result["nodes"] and result["edges"]
    ids = {n["id"] for n in result["nodes"]}
    for edge in result["edges"]:
        assert edge["subject"] and edge["object"]
    assert result["truncated"] in (True, False)


# Review Focus 3: a hub node at depth 4 walks most of the graph.
def test_depth_is_capped_and_the_cap_is_reported(live_config):
    result = subgraph(live_config, "Sherlock Holmes", depth=99)
    assert result["depth"] == 3          # MAX_DEPTH, not 99
    assert "capped" in (result.get("note") or "").lower()


def test_depth_below_one_is_rejected(live_config):
    result = subgraph(live_config, "Sherlock Holmes", depth=0)
    assert result["error"]["code"] == "not_found" or result["depth"] == 1


def test_subgraph_node_cap_truncates(live_config):
    result = subgraph(live_config, "Sherlock Holmes", depth=3)
    assert len(result["nodes"]) <= 300


# Phase 3 Task 13 made the query agent return the same evidence every run
# by adding a total order before every LIMIT. These tools cap results too,
# so they inherit the same hazard: an unordered query plus a cap returns a
# DIFFERENT subset run to run, and a UI would redraw a different graph each
# time with nothing to blame.
def test_the_same_entity_card_comes_back_every_time(live_config):
    first = entity(live_config, "Sherlock Holmes")
    assert all(entity(live_config, "Sherlock Holmes") == first for _ in range(3))


def test_the_same_subgraph_comes_back_every_time(live_config):
    first = subgraph(live_config, "Sherlock Holmes", depth=2)
    assert all(subgraph(live_config, "Sherlock Holmes", depth=2) == first
               for _ in range(3))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -k "entity or subgraph" -v`
Expected: FAIL — `ImportError: cannot import name 'entity'`

- [ ] **Step 3: Implement both tools**

The shared resolver — both tools call this, so a name means the same thing to each:

```python
MAX_DEPTH = 3
SUBGRAPH_NODE_CAP = 300


class _NotFound(Exception):
    pass


class _Ambiguous(Exception):
    def __init__(self, candidates: list[tuple[str, str]]) -> None:
        # [(id, name), ...]
        self.candidates = candidates


def _resolve_one(kuzu, name: str) -> str:
    """One canonical entity id for `name`, or raise.

    Resolves through NameIndex and canonical_ids -- the SAME path the query
    agent uses -- so an alias finds its canonical identically. Do not write
    a second name-matching rule here; two rules that drift is how a tool
    and an answer start disagreeing about who someone is.
    """
    index = load_name_index(kuzu)
    ids = named_entity_ids(name, index)
    if not ids:
        raise _NotFound()
    canonical = live_entity_ids(kuzu, canonical_ids(kuzu, ids))
    if not canonical:
        raise _NotFound()
    distinct = list(dict.fromkeys(canonical))
    if len(distinct) > 1:
        rows = {
            row[0]: row[1]
            for row in kuzu.execute(
                "MATCH (e:Entity) WHERE e.id IN $ids RETURN e.id, e.name",
                {"ids": distinct},
            )
        }
        raise _Ambiguous([(i, rows.get(i, i)) for i in distinct])
    return distinct[0]
```

`entity(config, name)` opens stores, calls `_resolve_one`, and turns the two
exceptions into envelopes. **Never pick a candidate silently** — the graph really
does carry "Miss Mary Morstan" and "Miss Morstan" as separate nodes, and a tool
that quietly picks one gives a confident answer about the wrong person:

```python
@_guarded
def entity(config: LoreConfig, name: str) -> dict:
    with open_stores(config) as stores:
        try:
            entity_id = _resolve_one(stores.kuzu, name)
        except _NotFound:
            return error("not_found", f"no entity named {name!r}")
        except _Ambiguous as exc:
            listed = ", ".join(f"{n} ({i})" for i, n in exc.candidates)
            return error(
                "not_found",
                f"{name!r} is ambiguous -- it matches {len(exc.candidates)}"
                f" entities: {listed}. Ask again with one of those names.",
            )
        return _entity_card(stores.kuzu, entity_id).model_dump()
```

`subgraph(config, entity_name, depth)` clamps the depth and **says that it did**.
An obeyed-but-different depth with no word about it is how a UI draws the wrong
picture and nobody notices:

```python
@_guarded
def subgraph(config: LoreConfig, entity_name: str, depth: int = 1) -> dict:
    with open_stores(config) as stores:
        try:
            root = _resolve_one(stores.kuzu, entity_name)
        except _NotFound:
            return error("not_found", f"no entity named {entity_name!r}")
        except _Ambiguous as exc:
            listed = ", ".join(f"{n} ({i})" for i, n in exc.candidates)
            return error("not_found",
                         f"{entity_name!r} is ambiguous -- it matches: {listed}")

        clamped = max(1, min(depth, MAX_DEPTH))
        note = None
        if clamped != depth:
            note = (f"depth {depth} was capped to {clamped}:"
                    f" a hub entity at depth {depth} walks most of the graph")

        nodes, edges, truncated = _expand(stores.kuzu, root, clamped,
                                          SUBGRAPH_NODE_CAP)
        return SubgraphOut(nodes=nodes, edges=edges, truncated=truncated,
                           depth=clamped, note=note).model_dump()
```

`_expand` walks `REL` breadth-first from `root` for `clamped` hops, one batched
Kuzu query per hop (`WHERE e.id IN $frontier`), stopping once `node_cap` nodes
have been collected and returning `truncated=True` when it stopped early.

Add `depth: int` and `note: str | None` to `SubgraphOut` in `shapes.py` — Task 6
listed the model without them because the cap was only decided here.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -v ; echo "EXIT: $?"`
Expected: PASS, EXIT: 0

- [ ] **Step 5: Commit**

```bash
git add lore/mcp/tools.py tests/test_mcp_tools.py
git commit -m "feat: add the entity and subgraph tools"
```

---

### Task 9: `vector_search` and `cypher_query`

**Files:**
- Modify: `lore/mcp/tools.py`
- Test: `tests/test_mcp_tools.py` (add)

**Interfaces:**
- Consumes: `lore.query.seed.embed_question`, `lore.query.agent.make_query_models`, `lore.mcp.guards.run_guarded`, `CypherRejected`
- Produces:
  - `async vector_search(config, query: str, book_id: str | None = None, limit: int = 10) -> dict`
  - `cypher_query(config, query: str) -> dict`

`vector_search` embeds locally (Ollama), so it costs no Gemini budget and keeps working after the daily cap. That is the point of the cheap/expensive split.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_tools.py  (append)
@pytest.mark.asyncio
async def test_vector_search_returns_chunks(live_config, fake_embed):
    result = await vector_search(live_config, "a murder in a locked house",
                                 limit=3, embed=fake_embed)
    assert len(result["chunks"]) <= 3
    hit = result["chunks"][0]
    assert set(hit) >= {"chunk_id", "book_id", "book_title",
                        "chapter_seq", "text", "distance"}


@pytest.mark.asyncio
async def test_vector_search_filters_by_book(live_config, fake_embed, a_book_id):
    result = await vector_search(live_config, "murder", book_id=a_book_id,
                                 embed=fake_embed)
    assert all(hit["book_id"] == a_book_id for hit in result["chunks"])


@pytest.mark.asyncio
async def test_vector_search_with_an_unknown_book_id(live_config, fake_embed):
    result = await vector_search(live_config, "murder", book_id="nope",
                                 embed=fake_embed)
    assert result["error"]["code"] == "not_found"


def test_cypher_query_returns_rows(live_config):
    result = cypher_query(live_config, "MATCH (e:Entity) RETURN e.name LIMIT 3")
    assert result["columns"] == ["e.name"]
    assert len(result["rows"]) <= 3
    assert result["truncated"] is False


def test_cypher_query_rejects_a_write(live_config):
    result = cypher_query(live_config, "CREATE (n:Entity {id: 'x'})")
    assert result["error"]["code"] == "cypher_rejected"
    assert "CREATE" in result["error"]["message"]


def test_cypher_query_rejects_copy_to_a_file(live_config, tmp_path):
    target = tmp_path / "leak.csv"
    result = cypher_query(
        live_config, f"COPY (MATCH (e:Entity) RETURN e.id) TO '{target}'"
    )
    assert result["error"]["code"] == "cypher_rejected"
    assert not target.exists(), "the guard must run BEFORE Kuzu sees the query"


def test_cypher_query_rejects_load_from_a_file(live_config):
    result = cypher_query(
        live_config, "LOAD FROM '/etc/hostname' (file_format='csv') RETURN *"
    )
    assert result["error"]["code"] == "cypher_rejected"


def test_cypher_query_caps_rows(live_config):
    result = cypher_query(live_config, "MATCH (a:Entity), (b:Entity) RETURN a.id, b.id")
    assert len(result["rows"]) <= 200
    assert result["truncated"] is True
```

`test_cypher_query_rejects_copy_to_a_file` asserting `not target.exists()` is the point of the whole task: the guard must reject before Kuzu runs the statement, because Kuzu will happily write that file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -k "vector_search or cypher" -v`
Expected: FAIL — `ImportError: cannot import name 'vector_search'`

- [ ] **Step 3: Implement both tools**

```python
@_guarded
def cypher_query(config: LoreConfig, query: str) -> dict:
    """Run one read-only Cypher statement.

    validate_cypher runs BEFORE Kuzu sees the string. That ordering is the
    whole control: Kuzu on a read-only connection will happily execute
    `COPY (...) TO '<path>'` and write the file.
    """
    with open_stores(config) as stores:
        try:
            columns, rows, truncated = run_guarded(stores.kuzu, query)
        except CypherRejected as exc:
            return error("cypher_rejected", exc.reason)
        except RuntimeError as exc:
            if "timeout" in str(exc).lower():
                return error("timeout", f"the query exceeded {TIMEOUT_MS}ms")
            raise
        return CypherResult(columns=columns, rows=rows,
                            truncated=truncated).model_dump()
```

`vector_search` takes an optional `embed` parameter (default `None`) so tests
inject a fake; when `None`, build the embedder from `make_query_models(config).embed`.
Validate `book_id` against `list_books` **before** searching:

```python
        if book_id is not None:
            known = {book["id"] for book in list_books(stores.conn)}
            if book_id not in known:
                return error("not_found", f"no book with id {book_id!r}")
```

Filtering to nothing and returning an empty list looks identical to "no
passages matched," which is a different answer — the user would go on
believing the book holds nothing about their question.

Apply `QUERY_PREFIX` exactly once, by calling `embed_question(query, embed)`
rather than `embed([query])`. A query embedded with the document prefix
retrieves worse without ever erroring, which is the failure mode
`make_query_models`' docstring warns about.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_tools.py -v ; echo "EXIT: $?"`
Expected: PASS, EXIT: 0

- [ ] **Step 5: Commit**

```bash
git add lore/mcp/tools.py tests/test_mcp_tools.py
git commit -m "feat: add the vector_search and cypher_query tools"
```

---

### Task 10: `lore_search`, with the budget guard

**Files:**
- Modify: `lore/mcp/tools.py`
- Test: `tests/test_mcp_budget.py`

**Interfaces:**
- Consumes: `lore.query.agent.answer_question`, `make_query_models`, `lore.orchestrator.gemini_budget.check_budget`, `BudgetExceeded`, `record_call`, `today`, `lore.mcp.shapes.to_search_result`
- Produces: `async lore_search(config, query: str) -> dict`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_budget.py
import pytest

from lore.inference.gemini import GeminiQuotaError, GeminiTransportError, limits_for
from lore.mcp.tools import entity, library, lore_search, subgraph
from lore.orchestrator.gemini_budget import today


@pytest.mark.asyncio
async def test_lore_search_returns_a_structured_answer(live_config, fake_models):
    result = await lore_search(live_config, "Who killed Enoch Drebber?",
                               models=fake_models)
    assert result["answer"]
    assert "claims" in result and "edges" in result and "seeds" in result
    assert result["mode"] in {"graph", "vector-fallback", "empty"}


@pytest.mark.asyncio
async def test_lore_search_refuses_at_the_cap(live_config, fake_models, at_the_cap):
    result = await lore_search(live_config, "Who killed Enoch Drebber?",
                               models=fake_models)
    assert result["error"]["code"] == "budget_exhausted"
    assert "resets" in result["error"]["message"]


@pytest.mark.asyncio
async def test_the_cheap_tools_still_work_at_the_cap(live_config, at_the_cap):
    assert "books" in library(live_config)
    assert entity(live_config, "Sherlock Holmes").get("error") is None
    assert subgraph(live_config, "Sherlock Holmes").get("error") is None


@pytest.mark.asyncio
async def test_a_search_records_its_calls(live_config, fake_models, sqlite_conn):
    from lore.orchestrator.gemini_budget import calls_today

    before = calls_today(sqlite_conn, "gemini-3.5-flash-lite", today())
    await lore_search(live_config, "Who killed Enoch Drebber?", models=fake_models)
    after = calls_today(sqlite_conn, "gemini-3.5-flash-lite", today())
    assert after > before


# Review Focus 4: Gemini failing for a reason that is NOT the budget must
# not be reported as budget_exhausted -- that would send the user to wait
# for midnight over a network blip.
@pytest.mark.asyncio
async def test_a_transport_failure_is_not_reported_as_a_budget_problem(
    live_config, failing_models
):
    result = await lore_search(live_config, "Who killed Enoch Drebber?",
                               models=failing_models)
    assert result["error"]["code"] != "budget_exhausted"


@pytest.mark.asyncio
async def test_every_model_cooling_is_not_reported_as_a_budget_problem(
    live_config, all_models_refusing
):
    result = await lore_search(live_config, "Who killed Enoch Drebber?",
                               models=all_models_refusing)
    assert result["error"]["code"] != "budget_exhausted"
    assert "refused" in result["error"]["message"].lower()
```

`at_the_cap` is a fixture inserting `limits_for(model).rpd` into `gemini_ledger` for today. `failing_models` raises `GeminiTransportError`; `all_models_refusing` raises `GeminiQuotaError`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_budget.py -v`
Expected: FAIL — `ImportError: cannot import name 'lore_search'`

- [ ] **Step 3: Implement `lore_search`**

1. Open stores.
2. Build models via `make_query_models(config, api_key=...)`, passing a `recorder` that calls `record_call(conn, model, today())`.
3. Before answering, `check_budget(conn, model, today())` for the synthesis model; `BudgetExceeded` → `error("budget_exhausted", str(exc))`.
4. `answer_question(...)`, then `to_search_result(answer, titles)`.
5. Catch `GeminiError` (the base class) and return a **non-budget** code with the model's own message. A `GeminiQuotaError` from the cascade means *every model refused this request*, which is not the same as our daily ledger being full — do not map it to `budget_exhausted`.
6. `finally:` close the models (`await models.aclose()`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_budget.py -v ; echo "EXIT: $?"`
Expected: PASS, EXIT: 0

- [ ] **Step 5: Commit**

```bash
git add lore/mcp/tools.py tests/test_mcp_budget.py
git commit -m "feat: add lore_search, refusing only when the budget is truly spent"
```

---

### Task 11: The server and the `lore mcp` command

**Files:**
- Create: `lore/mcp/server.py`
- Modify: `lore/cli.py` (add the `mcp` command), `pyproject.toml` (add `mcp>=2.2`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Produces:
  - `build_server(config: LoreConfig) -> MCPServer` — all seven tools registered
  - `run_stdio(config) -> None`, `run_http(config, host: str, port: int) -> None`

**The API, read from the SDK's source — not recalled.** In `mcp>=2.2` the class is `MCPServer`, imported `from mcp.server import MCPServer`. `FastMCP` is the 1.x name and will not import. `run` takes `transport="stdio"` or `transport="streamable-http"` with `host`, `port`, and `transport_security` keyword arguments.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add to `dependencies`:

```toml
    "mcp>=2.2",
```

Run: `uv sync` (or `uv pip install -e .`), then confirm the import shape before writing code:

```bash
.venv/bin/python -c "from mcp.server import MCPServer; print(MCPServer)"
```

If that import fails, stop and read the installed package's own `__init__` rather than guessing a path.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_mcp_server.py
import pytest

from lore.mcp.server import build_server

EXPECTED = {
    "lore_search", "vector_search", "cypher_query",
    "entity", "subgraph", "library", "ingest_status",
}


@pytest.mark.asyncio
async def test_all_seven_tools_are_registered(live_config):
    server = build_server(live_config)
    names = {tool.name for tool in await server.list_tools()}
    assert names == EXPECTED


@pytest.mark.asyncio
async def test_every_tool_has_a_description(live_config):
    server = build_server(live_config)
    for tool in await server.list_tools():
        assert tool.description, f"{tool.name} has no description"


@pytest.mark.asyncio
async def test_a_tool_call_round_trips_in_memory(live_config):
    """The transport smoke test: a real client, no subprocess."""
    server = build_server(live_config)
    result = await server.call_tool("library", {})
    assert result is not None
```

Adapt `list_tools()` / `call_tool()` to whatever the installed SDK actually exposes on `MCPServer` — check with `dir()` before assuming. Keep the assertions.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lore.mcp.server'`

- [ ] **Step 4: Write the server**

```python
# lore/mcp/server.py
"""The seven tools, over stdio and Streamable HTTP.

Note the class is MCPServer, not FastMCP: FastMCP is the mcp 1.x name and
does not exist in 2.x. SPEC originally said SSE; SSE is the HTTP transport
the MCP spec superseded, and this serves Streamable HTTP instead.
"""
from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from lore.config import LoreConfig
from lore.mcp import tools


def build_server(config: LoreConfig) -> MCPServer:
    mcp = MCPServer("lore")

    @mcp.tool()
    async def lore_search(query: str) -> dict:
        """Answer a question from the knowledge graph, with quoted citations."""
        return await tools.lore_search(config, query)

    @mcp.tool()
    async def vector_search(query: str, book_id: str | None = None,
                            limit: int = 10) -> dict:
        """Find passages by meaning. No Gemini budget is spent."""
        return await tools.vector_search(config, query, book_id, limit)

    @mcp.tool()
    def cypher_query(query: str) -> dict:
        """Run a read-only Cypher query against the graph."""
        return tools.cypher_query(config, query)

    @mcp.tool()
    def entity(name: str) -> dict:
        """The canonical card for one entity: aliases, books, edges."""
        return tools.entity(config, name)

    @mcp.tool()
    def subgraph(entity: str, depth: int = 1) -> dict:
        """A bounded subgraph around one entity, as nodes and edges."""
        return tools.subgraph(config, entity, depth)

    @mcp.tool()
    def library() -> dict:
        """Every book in the library, with its status and counts."""
        return tools.library(config)

    @mcp.tool()
    def ingest_status(book_id: str) -> dict:
        """Pipeline state for one book: jobs and dead letters."""
        return tools.ingest_status(config, book_id)

    return mcp


def run_stdio(config: LoreConfig) -> None:
    build_server(config).run(transport="stdio")


def run_http(config: LoreConfig, host: str = "127.0.0.1", port: int = 8765) -> None:
    # Bound to loopback, plus the SDK's DNS-rebinding protection: without an
    # allowed-hosts allowlist a page in the user's browser can reach a
    # localhost port.
    security = TransportSecuritySettings(
        allowed_hosts=[f"{host}:{port}", f"localhost:{port}", f"127.0.0.1:{port}"],
    )
    build_server(config).run(
        transport="streamable-http", host=host, port=port,
        transport_security=security,
    )
```

- [ ] **Step 5: Add the CLI command**

```python
@app.command()
def mcp(
    http: bool = typer.Option(False, "--http",
                              help="serve Streamable HTTP instead of stdio"),
    host: str = typer.Option("127.0.0.1", help="HTTP bind address"),
    port: int = typer.Option(8765, help="HTTP port"),
) -> None:
    """Serve LORE's seven read-only tools over MCP.

    Read-only: nothing in the library is created or modified. Store handles
    open and close per request, so `lore graph load` keeps working while
    this is running.
    """
    config = LoreConfig.load()
    if http:
        typer.echo(f"lore mcp: streamable-http on http://{host}:{port}")
        run_http(config, host, port)
    else:
        run_stdio(config)
```

`run_stdio` must print nothing to stdout — stdout *is* the protocol channel. Any banner goes to stderr or nowhere.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -v ; echo "EXIT: $?"`
Expected: PASS, EXIT: 0

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q -m "not live" ; echo "EXIT: $?"`
Expected: EXIT: 0

- [ ] **Step 8: Commit**

```bash
git add lore/mcp/server.py lore/cli.py pyproject.toml uv.lock tests/test_mcp_server.py
git commit -m "feat: serve the seven tools over stdio and Streamable HTTP"
```

---

### Task 12: Acceptance — an external client answers a real question

**Files:**
- Modify: `.mcp.json` (add the `lore` server)
- Modify: `README.md` (a short "Ask LORE from an MCP client" section)

This task has no unit tests. Its deliverable is evidence that the thing works end to end, which is the spec's stated done-when.

- [ ] **Step 1: Register the server**

Add to `.mcp.json` beside the existing `graft` entry:

```json
    "lore": {
      "command": ".venv/bin/lore",
      "args": ["mcp"]
    }
```

- [ ] **Step 2: Verify stdio starts cleanly**

Run: `.venv/bin/lore mcp < /dev/null ; echo "EXIT: $?"`
Expected: it starts, reads EOF, and exits without a traceback. **Nothing on stdout but protocol frames.**

- [ ] **Step 3: Verify the HTTP transport binds**

Run: `.venv/bin/lore mcp --http --port 8765` in one shell; in another, `curl -sS -i http://127.0.0.1:8765/mcp | head -5`.
Expected: an HTTP response, not a connection refused. Stop the server afterwards.

- [ ] **Step 4: Ask a real question through an MCP client**

Restart Claude Code so it picks up `.mcp.json`, then ask a multi-hop question that `lore ask` is known to answer — e.g. *"Why did Jefferson Hope kill Enoch Drebber and Joseph Stangerson?"* — and confirm the `lore_search` tool answers it with claims and edges.

- [ ] **Step 5: Confirm a rebuild works while the server runs**

With the server running, run `.venv/bin/lore graph load` (or a dry equivalent) and confirm it does **not** fail with "Could not set lock on file". This is the live proof of the per-request-handles decision.

- [ ] **Step 6: Document it in the README**

A short section showing the `.mcp.json` entry, the two transports, and one sentence that every tool is read-only and only `lore_search` spends Gemini budget.

- [ ] **Step 7: Run the full suite one last time**

Run: `.venv/bin/python -m pytest -q -m "not live" ; echo "EXIT: $?"`
Expected: EXIT: 0

- [ ] **Step 8: Commit and push**

```bash
git add .mcp.json README.md
git commit -m "docs: register the LORE MCP server and document the two transports"
git log -1 --format='%an <%ae>'   # must be jatinroopchandani3007@gmail.com
git push origin main
```
