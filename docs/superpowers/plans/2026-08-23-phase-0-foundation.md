# LORE Phase 0 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A uv-managed Python scaffold where one command (`lore doctor`) proves every embedded store opens, local Ollama answers a structured prompt, and a programmatic Kaggle GPU round trip (push dataset → run kernel → poll → pull output) completes — then the repo is pushed to a **private** GitHub repo on the **personal** account.

**Architecture:** Thin vertical slices of every Phase-0 subsystem from SPEC.md §9 Phase 0: config loader, SQLite migration runner, Kuzu graph store, LanceDB vector store, `InferenceProvider` protocol with `OllamaProvider`, a Kaggle client wrapping the `kaggle` CLI, and a `lore doctor` CLI that composes all health checks. Everything embedded — no Docker, no servers.

**Tech Stack:** Python 3.12, uv, pydantic v2, httpx, kuzu, lancedb, pyarrow, typer, pytest, kaggle CLI, Ollama.

**Spec:** `SPEC.md` (repo root) — this plan implements §9 "Phase 0 — Foundation".

## Global Constraints

- Python **3.12**, managed by **uv** (`uv run`, `uv add` — never bare pip).
- Embedding dimension is **768** everywhere (nomic-embed-text-v1.5) — LanceDB schemas must use it.
- All runtime state lives under `data/` (gitignored). Tests use `tmp_path`, never `data/`.
- No paid services. Kaggle free tier + local Ollama only.
- **Git identity (HARD GATE, from user):** every commit in this repo must be authored as the PERSONAL account: `user.email = jatinroopchandani3007@gmail.com` set via **local** `git config` (not `--global`). Before every `git commit`, if `git config user.email` does not print exactly that address, STOP and fix — never commit with a work identity. The GitHub remote must use the `github-personal` SSH host alias, and the repo is created **private**. Never use the work account, no matter what.
- Ollama default model: `qwen2.5:3b-instruct-q4_K_M`; Ollama host `http://localhost:11434`.
- Commit messages: conventional commits (`feat:`, `test:`, `chore:`), each ending with the two Claude attribution lines used in Task 1 Step 6.

---

### Task 0: Environment Prerequisites (no commits)

**Files:** none — host setup and verification only.

**Interfaces:**
- Consumes: nothing.
- Produces: working `uv`, `ollama` (with model pulled), and `~/.kaggle/kaggle.json` credentials that every later task assumes.

- [ ] **Step 1: Verify or install uv**

Run: `uv --version || curl -LsSf https://astral.sh/uv/install.sh | sh`
Expected: a version string like `uv 0.4.x` (after opening a new shell if just installed).

- [ ] **Step 2: Verify or install Ollama (WSL2)**

Run: `ollama --version || curl -fsSL https://ollama.com/install.sh | sh`
Then ensure the server is up: `ollama list` (starts/uses the service). If `ollama list` errors with connection refused, run `ollama serve &` and retry.
Expected: `ollama list` prints a (possibly empty) table.

- [ ] **Step 3: Pull the query-time model (~2GB disk)**

Run: `ollama pull qwen2.5:3b-instruct-q4_K_M`
Expected: download completes; `ollama list` shows the model.

- [ ] **Step 4: Smoke the model**

Run: `ollama run qwen2.5:3b-instruct-q4_K_M "Reply with exactly: OK"`
Expected: a short reply. (Content may vary; success = no error.)

- [ ] **Step 5: Verify Kaggle credentials**

Check: `ls ~/.kaggle/kaggle.json`. If missing, tell the user: log into kaggle.com with their personal account → Settings → API → "Create New Token", save the downloaded file to `~/.kaggle/kaggle.json`, then `chmod 600 ~/.kaggle/kaggle.json`. **This is a user-action blocker — pause the task until the file exists.**
Expected: file exists with mode 600.

- [ ] **Step 6: Verify the github-personal SSH alias resolves to the personal account**

Run: `ssh -T git@github-personal 2>&1 | head -1`
Expected: `Hi <personal-username>! You've successfully authenticated...`. **Record `<personal-username>` — later tasks call it `PERSONAL_GH_USER`.** If the alias is missing from `~/.ssh/config` or greets a work username, STOP and ask the user to fix their SSH config before proceeding.

---

### Task 1: Git Repository + Identity Guard

**Files:**
- Create: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: an initialized repo on branch `main` with local identity `jatinroopchandani3007@gmail.com`; all later tasks commit into it.

- [ ] **Step 1: Initialize the repo**

```bash
cd /home/jatin/LORE
git init -b main
```

- [ ] **Step 2: Set LOCAL identity to the personal account**

```bash
git config user.email "jatinroopchandani3007@gmail.com"
git config user.name "Jatin Roopchandani"
```
(Local config only — no `--global`; this must not touch other repos.)

- [ ] **Step 3: HARD GATE — verify identity before the first commit**

Run: `git config user.email && git config user.name`
Expected: exactly `jatinroopchandani3007@gmail.com` on the first line. If anything else appears (especially a work address), STOP — do not commit — and repeat Step 2.

- [ ] **Step 4: Write `.gitignore`**

```gitignore
# runtime state — never commit
data/

# python
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
dist/
*.egg-info/

# secrets — belt and braces
.env
kaggle.json

# editors / os
.vscode/
.idea/
.DS_Store
```

- [ ] **Step 5: Stage the existing docs and gitignore**

```bash
git add .gitignore SPEC.md docs/
git status
```
Expected: only `.gitignore`, `SPEC.md`, and files under `docs/` staged.

- [ ] **Step 6: Commit**

```bash
git commit -m "chore: initialize LORE repo with spec and phase 0 plan

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017yr9NkKfFPPU4kWXNRtrUU"
```
Then verify authorship: `git log -1 --format='%ae'` must print `jatinroopchandani3007@gmail.com`.

---

### Task 2: uv Project Scaffold + Test Harness

**Files:**
- Create: `pyproject.toml`, `.python-version`, `README.md`, `lore/__init__.py`, `tests/__init__.py`, `tests/test_package.py`

**Interfaces:**
- Consumes: Task 1's repo.
- Produces: importable `lore` package with `lore.__version__: str`; `uv run pytest` works; dependencies `pydantic`, `httpx`, `kuzu`, `lancedb`, `pyarrow`, `typer` and dev-dependency `pytest` installed for all later tasks.

- [ ] **Step 1: Scaffold with uv**

```bash
cd /home/jatin/LORE
echo "3.12" > .python-version
uv init --name lore --package --no-readme || true
```
If `uv init` refuses because files exist, create `pyproject.toml` manually in Step 2 instead.

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "lore"
version = "0.1.0"
description = "LORE - Local Orchestration & Relational Extraction: a local-first literary knowledge graph"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.7",
    "httpx>=0.27",
    "kuzu>=0.6",
    "lancedb>=0.13",
    "pyarrow>=17.0",
    "typer>=0.12",
]

[project.scripts]
lore = "lore.cli:app"

[dependency-groups]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["lore"]

[tool.pytest.ini_options]
markers = ["live: tests that hit real local services (ollama); deselect with -m 'not live'"]
```

- [ ] **Step 3: Create the package and a failing test**

`lore/__init__.py`:
```python
__version__ = "0.1.0"
```

`tests/__init__.py`: empty file.

`tests/test_package.py`:
```python
import lore


def test_version() -> None:
    assert lore.__version__ == "0.1.0"
```

- [ ] **Step 4: Install and run tests**

Run: `uv sync && uv run pytest -v`
Expected: `test_version PASSED` (1 passed). This also proves `kuzu`, `lancedb`, `pyarrow` wheels install on this machine — if any fails to resolve, surface the error rather than pinning blindly.

- [ ] **Step 5: Write `README.md`**

```markdown
# LORE — Local Orchestration & Relational Extraction

A local-first literary knowledge graph: e-books in, entities/relationships out
(extracted on free Kaggle GPUs or fully locally), served from embedded
databases (Kuzu + LanceDB) with a hybrid graph-vector query agent, an MCP
server, and a graph UI.

See `SPEC.md` for the full design. Phase 0 status: foundation scaffold.

## Quickstart (Phase 0)

    uv sync
    uv run lore doctor          # checks config, stores, ollama, kaggle auth
    uv run lore doctor --kaggle # + full free-tier GPU round trip (slow)
```

- [ ] **Step 6: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add pyproject.toml .python-version uv.lock README.md lore/ tests/
git commit -m "feat: uv project scaffold with package and test harness

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017yr9NkKfFPPU4kWXNRtrUU"
```

---

### Task 3: Config System

**Files:**
- Create: `lore/config.py`, `lore.toml`, `tests/test_config.py`

**Interfaces:**
- Consumes: Task 2's package.
- Produces: `LoreConfig.load(path: Path | None = None) -> LoreConfig` with fields `data_dir: Path`, `extraction_provider: str`, `ollama: OllamaConfig(host: str, model: str)`, `kaggle: KaggleConfig(username: str, kernel_slug: str, dataset_slug: str, poll_interval_s: int, poll_timeout_s: int)`, and method `ensure_dirs() -> None`. Later tasks construct stores from `config.data_dir`.

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:
```python
from pathlib import Path

from lore.config import LoreConfig


def test_defaults_when_no_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # no lore.toml here
    cfg = LoreConfig.load()
    assert cfg.data_dir == Path("data")
    assert cfg.extraction_provider == "kaggle"
    assert cfg.ollama.model == "qwen2.5:3b-instruct-q4_K_M"
    assert cfg.ollama.host == "http://localhost:11434"


def test_load_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "lore.toml"
    toml.write_text(
        '[lore]\ndata_dir = "elsewhere"\nextraction_provider = "ollama"\n'
        '[lore.ollama]\nmodel = "llama3.2:3b"\n'
        '[lore.kaggle]\nusername = "someuser"\n'
    )
    cfg = LoreConfig.load(toml)
    assert cfg.data_dir == Path("elsewhere")
    assert cfg.extraction_provider == "ollama"
    assert cfg.ollama.model == "llama3.2:3b"
    assert cfg.kaggle.username == "someuser"


def test_env_overrides_data_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "envdata"))
    cfg = LoreConfig.load()
    assert cfg.data_dir == tmp_path / "envdata"


def test_ensure_dirs_creates_layout(tmp_path: Path) -> None:
    cfg = LoreConfig.load()
    cfg = cfg.model_copy(update={"data_dir": tmp_path / "d"})
    cfg.ensure_dirs()
    for sub in ("kuzu", "lancedb", "inbox"):
        assert (tmp_path / "d" / sub).is_dir()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lore.config'`.

- [ ] **Step 3: Implement `lore/config.py`**

```python
"""Configuration: lore.toml + LORE_* env overrides, defaults from SPEC.md."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class OllamaConfig(BaseModel):
    host: str = "http://localhost:11434"
    model: str = "qwen2.5:3b-instruct-q4_K_M"


class KaggleConfig(BaseModel):
    username: str = ""
    kernel_slug: str = "lore-hello"
    dataset_slug: str = "lore-hello-data"
    poll_interval_s: int = 30
    poll_timeout_s: int = 1800


class LoreConfig(BaseModel):
    data_dir: Path = Path("data")
    extraction_provider: str = "kaggle"  # "kaggle" | "ollama"
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    kaggle: KaggleConfig = Field(default_factory=KaggleConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> "LoreConfig":
        candidate = path if path is not None else Path("lore.toml")
        raw: dict = {}
        if candidate.is_file():
            with candidate.open("rb") as f:
                raw = tomllib.load(f).get("lore", {})
        cfg = cls.model_validate(raw)
        if env_dir := os.environ.get("LORE_DATA_DIR"):
            cfg = cfg.model_copy(update={"data_dir": Path(env_dir)})
        return cfg

    @property
    def kuzu_dir(self) -> Path:
        return self.data_dir / "kuzu"

    @property
    def lancedb_dir(self) -> Path:
        return self.data_dir / "lancedb"

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "lore.db"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.kuzu_dir, self.lancedb_dir, self.inbox_dir):
            d.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 4 passed.

- [ ] **Step 5: Write the repo-root `lore.toml`**

```toml
# LORE configuration — see lore/config.py for all keys and defaults.
[lore]
data_dir = "data"
# "kaggle" (free T4 burst, default) | "ollama" (fully local, zero-egress)
extraction_provider = "kaggle"

[lore.ollama]
host = "http://localhost:11434"
model = "qwen2.5:3b-instruct-q4_K_M"

[lore.kaggle]
username = ""            # personal Kaggle username (required for doctor --kaggle)
kernel_slug = "lore-hello"
dataset_slug = "lore-hello-data"
poll_interval_s = 30
poll_timeout_s = 1800
```

- [ ] **Step 6: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/config.py lore.toml tests/test_config.py
git commit -m "feat: config system with toml loading and env overrides

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017yr9NkKfFPPU4kWXNRtrUU"
```

---

### Task 4: SQLite Store + Migration Runner

**Files:**
- Create: `lore/db/__init__.py`, `lore/db/sqlite.py`, `tests/test_sqlite.py`

**Interfaces:**
- Consumes: nothing from other tasks (takes a plain `Path`).
- Produces: `open_db(path: Path) -> sqlite3.Connection` (applies pending migrations, enables WAL + foreign keys) and `schema_version(conn) -> int`. Tables after v1: `schema_migrations(version, applied_at)`, `books(id TEXT PK, title, author, source, file_path, status, created_at)`, `jobs(id INTEGER PK, kind, book_id FK, state, detail, created_at, updated_at)`, `quota_ledger(id INTEGER PK, run_ref, gpu_hours_est REAL, gpu_hours_actual REAL, week TEXT, created_at)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_sqlite.py`:
```python
import sqlite3
from pathlib import Path

from lore.db.sqlite import open_db, schema_version


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_open_db_creates_schema(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    assert {"schema_migrations", "books", "jobs", "quota_ledger"} <= _tables(conn)
    assert schema_version(conn) == 1
    conn.close()


def test_open_db_is_idempotent(tmp_path: Path) -> None:
    open_db(tmp_path / "lore.db").close()
    conn = open_db(tmp_path / "lore.db")  # second open must not re-apply v1
    assert schema_version(conn) == 1
    conn.close()


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    try:
        conn.execute(
            "INSERT INTO jobs (kind, book_id, state) VALUES ('ingest', 'nope', 'queued')"
        )
        assert False, "expected FK violation"
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sqlite.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lore.db'`.

- [ ] **Step 3: Implement**

`lore/db/__init__.py`: empty file.

`lore/db/sqlite.py`:
```python
"""SQLite catalog/job store with a linear, idempotent migration runner."""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE books (
            id         TEXT PRIMARY KEY,          -- content hash of source file
            title      TEXT NOT NULL,
            author     TEXT,
            source     TEXT NOT NULL DEFAULT 'upload',
            file_path  TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'registered',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE jobs (
            id         INTEGER PRIMARY KEY,
            kind       TEXT NOT NULL,
            book_id    TEXT REFERENCES books(id),
            state      TEXT NOT NULL DEFAULT 'queued',
            detail     TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE quota_ledger (
            id               INTEGER PRIMARY KEY,
            run_ref          TEXT NOT NULL,
            gpu_hours_est    REAL NOT NULL,
            gpu_hours_actual REAL,
            week             TEXT NOT NULL,       -- ISO week, e.g. 2026-W34
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),
]


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    current = schema_version(conn)
    for version, sql in MIGRATIONS:
        if version > current:
            with conn:  # transaction per migration
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
                )
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sqlite.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/db/ tests/test_sqlite.py
git commit -m "feat: sqlite store with idempotent migration runner

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017yr9NkKfFPPU4kWXNRtrUU"
```

---

### Task 5: Kuzu Graph Store

**Files:**
- Create: `lore/db/kuzu_store.py`, `tests/test_kuzu_store.py`

**Interfaces:**
- Consumes: a plain `Path` (callers pass `config.kuzu_dir`).
- Produces: `class KuzuStore` with `__init__(self, path: Path)` (opens/creates DB and applies the SPEC §6 schema idempotently), `execute(self, cypher: str, params: dict | None = None)` returning a `kuzu.QueryResult`, `table_names(self) -> set[str]`, and `close(self) -> None`. Node tables: `Book, Chapter, Chunk, Entity`; rel tables: `HAS_CHAPTER, HAS_CHUNK, MENTIONS, ALIAS_OF, REL` (typed rels like `ALLEGIANCE_TO` arrive in Phase 2 with the extractor — YAGNI here).

- [ ] **Step 1: Write the failing tests**

`tests/test_kuzu_store.py`:
```python
from pathlib import Path

from lore.db.kuzu_store import KuzuStore


def test_creates_schema(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kuzu")
    assert {"Book", "Chapter", "Chunk", "Entity", "HAS_CHAPTER", "HAS_CHUNK",
            "MENTIONS", "ALIAS_OF", "REL"} <= store.table_names()
    store.close()


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    KuzuStore(tmp_path / "kuzu").close()
    store = KuzuStore(tmp_path / "kuzu")  # must not fail on existing tables
    assert "Entity" in store.table_names()
    store.close()


def test_write_and_traverse(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kuzu")
    store.execute(
        "CREATE (:Entity {id: 'e1', name: 'Voldemort', kind: 'Character'})"
    )
    store.execute(
        "CREATE (:Entity {id: 'e2', name: 'The Dark Lord', kind: 'Character'})"
    )
    store.execute(
        "MATCH (a:Entity {id:'e2'}), (b:Entity {id:'e1'}) "
        "CREATE (a)-[:ALIAS_OF]->(b)"
    )
    result = store.execute(
        "MATCH (a:Entity)-[:ALIAS_OF]->(b:Entity) RETURN a.name, b.name"
    )
    rows = [tuple(r) for r in result]
    assert rows == [("The Dark Lord", "Voldemort")]
    store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_kuzu_store.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.db.kuzu_store`.

- [ ] **Step 3: Implement `lore/db/kuzu_store.py`**

```python
"""Embedded Kuzu graph store — SPEC.md §6 schema, applied idempotently."""
from __future__ import annotations

from pathlib import Path

import kuzu

_SCHEMA: list[str] = [
    "CREATE NODE TABLE Book (id STRING, title STRING, author STRING, PRIMARY KEY (id))",
    "CREATE NODE TABLE Chapter (id STRING, book_id STRING, seq INT64, title STRING, PRIMARY KEY (id))",
    "CREATE NODE TABLE Chunk (id STRING, book_id STRING, chapter_seq INT64, PRIMARY KEY (id))",
    "CREATE NODE TABLE Entity (id STRING, name STRING, kind STRING, aliases STRING[], PRIMARY KEY (id))",
    "CREATE REL TABLE HAS_CHAPTER (FROM Book TO Chapter)",
    "CREATE REL TABLE HAS_CHUNK (FROM Chapter TO Chunk)",
    "CREATE REL TABLE MENTIONS (FROM Chunk TO Entity)",
    "CREATE REL TABLE ALIAS_OF (FROM Entity TO Entity)",
    (
        "CREATE REL TABLE REL (FROM Entity TO Entity, predicate STRING, "
        "evidence STRING, confidence DOUBLE, chunk_id STRING, book_id STRING, "
        "chapter_seq INT64, run_id STRING)"
    ),
]


class KuzuStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = kuzu.Database(str(path))
        self._conn = kuzu.Connection(self._db)
        existing = self.table_names()
        for ddl in _SCHEMA:
            table = ddl.split()[3]  # CREATE NODE|REL TABLE <name> ...
            if table not in existing:
                self._conn.execute(ddl)

    def execute(self, cypher: str, params: dict | None = None) -> kuzu.QueryResult:
        return self._conn.execute(cypher, parameters=params or {})

    def table_names(self) -> set[str]:
        result = self._conn.execute("CALL show_tables() RETURN name")
        return {row[0] for row in result}

    def close(self) -> None:
        self._conn.close()
        self._db.close()
```

Note for the implementer: `ddl.split()[3]` relies on every `_SCHEMA` string starting `CREATE NODE TABLE <name>` / `CREATE REL TABLE <name>` — keep that shape if you edit the schema. If the installed kuzu version's `show_tables()` returns a different column name, adapt `table_names()` (check with `CALL show_tables() RETURN *`), not the tests.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_kuzu_store.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/db/kuzu_store.py tests/test_kuzu_store.py
git commit -m "feat: embedded kuzu graph store with idempotent spec schema

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017yr9NkKfFPPU4kWXNRtrUU"
```

---

### Task 6: LanceDB Vector Store

**Files:**
- Create: `lore/db/lance_store.py`, `tests/test_lance_store.py`

**Interfaces:**
- Consumes: a plain `Path` (callers pass `config.lancedb_dir`).
- Produces: `EMBED_DIM = 768`; `class LanceStore` with `__init__(self, path: Path)` (creates tables `chunks` and `entities` if missing), `add_chunks(self, rows: list[dict]) -> None` (each row: `vector: list[float]`, `chunk_id: str`, `book_id: str`, `chapter_seq: int`, `text: str`, `entity_ids: list[str]`), `search_chunks(self, vector: list[float], limit: int = 5) -> list[dict]`, `table_names(self) -> set[str]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_lance_store.py`:
```python
from pathlib import Path

from lore.db.lance_store import EMBED_DIM, LanceStore


def test_creates_tables(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    assert {"chunks", "entities"} <= store.table_names()


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    LanceStore(tmp_path / "lancedb")
    store = LanceStore(tmp_path / "lancedb")
    assert "chunks" in store.table_names()


def test_add_and_search_chunks(tmp_path: Path) -> None:
    store = LanceStore(tmp_path / "lancedb")
    near = [1.0] + [0.0] * (EMBED_DIM - 1)
    far = [0.0] * (EMBED_DIM - 1) + [1.0]
    store.add_chunks([
        {"vector": near, "chunk_id": "b1:1:0", "book_id": "b1",
         "chapter_seq": 1, "text": "the fall of faction A", "entity_ids": ["e1"]},
        {"vector": far, "chunk_id": "b1:9:4", "book_id": "b1",
         "chapter_seq": 9, "text": "unrelated scene", "entity_ids": []},
    ])
    hits = store.search_chunks(near, limit=1)
    assert hits[0]["chunk_id"] == "b1:1:0"
    assert hits[0]["entity_ids"] == ["e1"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_lance_store.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.db.lance_store`.

- [ ] **Step 3: Implement `lore/db/lance_store.py`**

```python
"""Embedded LanceDB vector store. EMBED_DIM matches nomic-embed-text-v1.5."""
from __future__ import annotations

from pathlib import Path

import lancedb
import pyarrow as pa

EMBED_DIM = 768

_CHUNKS_SCHEMA = pa.schema([
    pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
    pa.field("chunk_id", pa.string()),
    pa.field("book_id", pa.string()),
    pa.field("chapter_seq", pa.int64()),
    pa.field("text", pa.string()),
    pa.field("entity_ids", pa.list_(pa.string())),
])

_ENTITIES_SCHEMA = pa.schema([
    pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
    pa.field("entity_id", pa.string()),
    pa.field("name", pa.string()),
    pa.field("kind", pa.string()),
])


class LanceStore:
    def __init__(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(path))
        existing = self.table_names()
        if "chunks" not in existing:
            self._db.create_table("chunks", schema=_CHUNKS_SCHEMA)
        if "entities" not in existing:
            self._db.create_table("entities", schema=_ENTITIES_SCHEMA)

    def table_names(self) -> set[str]:
        return set(self._db.table_names())

    def add_chunks(self, rows: list[dict]) -> None:
        self._db.open_table("chunks").add(rows)

    def search_chunks(self, vector: list[float], limit: int = 5) -> list[dict]:
        table = self._db.open_table("chunks")
        return table.search(vector).limit(limit).to_list()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_lance_store.py -v`
Expected: 3 passed. (If `to_list()` returns `entity_ids` as a numpy array on this lancedb version, normalize to `list` inside `search_chunks` — the interface promises Python lists.)

- [ ] **Step 5: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/db/lance_store.py tests/test_lance_store.py
git commit -m "feat: embedded lancedb vector store with 768-dim chunk/entity tables

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017yr9NkKfFPPU4kWXNRtrUU"
```

---

### Task 7: InferenceProvider Protocol + OllamaProvider

**Files:**
- Create: `lore/inference/__init__.py`, `lore/inference/base.py`, `lore/inference/ollama.py`, `tests/test_ollama_provider.py`

**Interfaces:**
- Consumes: `OllamaConfig` from Task 3 (`host`, `model`).
- Produces: `InferenceProvider` protocol with `async def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel`; `class OllamaProvider(config: OllamaConfig)` implementing it via Ollama's `/api/chat` structured-output (`format` = JSON schema), plus `async def healthy(self) -> bool` (GET `/api/tags`). This is the exact seam SPEC §3 requires for the one-line provider switch.

- [ ] **Step 1: Write the failing tests (mocked transport + one live-marked test)**

`tests/test_ollama_provider.py`:
```python
import json

import httpx
import pytest
from pydantic import BaseModel

from lore.config import OllamaConfig
from lore.inference.ollama import OllamaProvider


class Answer(BaseModel):
    city: str


def _provider_with(handler) -> OllamaProvider:
    transport = httpx.MockTransport(handler)
    provider = OllamaProvider(OllamaConfig())
    provider._client = httpx.AsyncClient(
        transport=transport, base_url=provider._client.base_url
    )
    return provider


@pytest.mark.asyncio
async def test_generate_parses_structured_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "qwen2.5:3b-instruct-q4_K_M"
        assert body["format"]["properties"]["city"]["type"] == "string"
        assert body["stream"] is False
        return httpx.Response(
            200, json={"message": {"role": "assistant",
                                   "content": '{"city": "Paris"}'}}
        )

    provider = _provider_with(handler)
    result = await provider.generate("Capital of France as JSON.", Answer)
    assert result == Answer(city="Paris")


@pytest.mark.asyncio
async def test_healthy_false_when_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = _provider_with(handler)
    assert await provider.healthy() is False


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_roundtrip() -> None:
    provider = OllamaProvider(OllamaConfig())
    if not await provider.healthy():
        pytest.skip("ollama not running")
    result = await provider.generate(
        "What is the capital of France? Respond as JSON.", Answer
    )
    assert "paris" in result.city.lower()
```

Also: `uv add --dev pytest-asyncio`, and append to `[tool.pytest.ini_options]` in `pyproject.toml`: `asyncio_mode = "auto"`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ollama_provider.py -m 'not live' -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.inference.ollama`.

- [ ] **Step 3: Implement**

`lore/inference/__init__.py`: empty file.

`lore/inference/base.py`:
```python
"""The provider seam (SPEC §3): all LLM calls cross this interface."""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class InferenceProvider(Protocol):
    async def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        """Run one structured-output completion, validated against `schema`."""
        ...
```

`lore/inference/ollama.py`:
```python
"""Local Ollama provider — structured output via /api/chat `format` schema."""
from __future__ import annotations

import httpx
from pydantic import BaseModel

from lore.config import OllamaConfig


class OllamaProvider:
    def __init__(self, config: OllamaConfig) -> None:
        self._model = config.model
        self._client = httpx.AsyncClient(base_url=config.host, timeout=120.0)

    async def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        response = await self._client.post(
            "/api/chat",
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "format": schema.model_json_schema(),
                "stream": False,
            },
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        return schema.model_validate_json(content)

    async def healthy(self) -> bool:
        try:
            response = await self._client.get("/api/tags", timeout=5.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run mocked tests, then the live test**

Run: `uv run pytest tests/test_ollama_provider.py -m 'not live' -v`
Expected: 2 passed.
Run: `uv run pytest tests/test_ollama_provider.py -m live -v`
Expected: 1 passed (Ollama is running from Task 0; a skip means it isn't — start it and re-run).

- [ ] **Step 5: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/inference/ tests/test_ollama_provider.py pyproject.toml uv.lock
git commit -m "feat: InferenceProvider protocol and OllamaProvider with structured output

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017yr9NkKfFPPU4kWXNRtrUU"
```

---

### Task 8: Kaggle Client Wrapper

**Files:**
- Create: `lore/kaggle_api/__init__.py`, `lore/kaggle_api/client.py`, `tests/test_kaggle_client.py`

**Interfaces:**
- Consumes: `KaggleConfig` from Task 3.
- Produces: `class KaggleClient(config: KaggleConfig)` wrapping the `kaggle` CLI via `subprocess.run`, with `authenticated() -> bool` (checks `~/.kaggle/kaggle.json` exists), `push_dataset(dir: Path) -> None` (create-or-version), `push_kernel(dir: Path) -> None`, `kernel_status() -> str` (one of `"running" | "complete" | "error" | "unknown"`), `pull_output(dest: Path) -> None`, and `run_and_wait(kernel_dir: Path, dest: Path, on_tick: Callable[[str], None] | None = None) -> Path` which pushes, polls until terminal state or `poll_timeout_s`, pulls, and returns `dest`. Raises `KaggleRunError` on `"error"` state or timeout. (Named `kaggle_api` not `kaggle` to avoid shadowing the `kaggle` PyPI package.)

- [ ] **Step 1: Write the failing tests (all subprocess calls mocked)**

`tests/test_kaggle_client.py`:
```python
from pathlib import Path
from unittest.mock import patch

import pytest

from lore.config import KaggleConfig
from lore.kaggle_api.client import KaggleClient, KaggleRunError


def _cfg() -> KaggleConfig:
    return KaggleConfig(username="testuser", poll_interval_s=0, poll_timeout_s=1)


class FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def test_push_kernel_invokes_cli(tmp_path: Path) -> None:
    client = KaggleClient(_cfg())
    with patch("lore.kaggle_api.client.subprocess.run",
               return_value=FakeCompleted()) as run:
        client.push_kernel(tmp_path)
    args = run.call_args.args[0]
    assert args[:3] == ["kaggle", "kernels", "push"]
    assert str(tmp_path) in args


def test_kernel_status_parses_complete() -> None:
    client = KaggleClient(_cfg())
    fake = FakeCompleted(stdout='testuser/lore-hello has status "complete"')
    with patch("lore.kaggle_api.client.subprocess.run", return_value=fake):
        assert client.kernel_status() == "complete"


def test_kernel_status_parses_error() -> None:
    client = KaggleClient(_cfg())
    fake = FakeCompleted(stdout='testuser/lore-hello has status "error"')
    with patch("lore.kaggle_api.client.subprocess.run", return_value=fake):
        assert client.kernel_status() == "error"


def test_run_and_wait_raises_on_error(tmp_path: Path) -> None:
    client = KaggleClient(_cfg())
    fake = FakeCompleted(stdout='testuser/lore-hello has status "error"')
    with patch("lore.kaggle_api.client.subprocess.run", return_value=fake):
        with pytest.raises(KaggleRunError):
            client.run_and_wait(tmp_path, tmp_path / "out")


def test_authenticated_checks_credentials_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    client = KaggleClient(_cfg())
    assert client.authenticated() is False
    (tmp_path / ".kaggle").mkdir()
    (tmp_path / ".kaggle" / "kaggle.json").write_text("{}")
    assert client.authenticated() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_kaggle_client.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.kaggle_api`.

- [ ] **Step 3: Implement**

`lore/kaggle_api/__init__.py`: empty file.

`lore/kaggle_api/client.py`:
```python
"""Kaggle free-tier batch backend, driven through the official `kaggle` CLI.

Transport is CLI-over-subprocess (not the python API) so credentials are
only ever touched by Kaggle's own tooling and tests can mock one seam.
"""
from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from lore.config import KaggleConfig


class KaggleRunError(RuntimeError):
    pass


class KaggleClient:
    def __init__(self, config: KaggleConfig) -> None:
        self._cfg = config

    @property
    def _kernel_ref(self) -> str:
        return f"{self._cfg.username}/{self._cfg.kernel_slug}"

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise KaggleRunError(
                f"{' '.join(args)} failed rc={result.returncode}: {result.stderr}"
            )
        return result

    def authenticated(self) -> bool:
        return (Path.home() / ".kaggle" / "kaggle.json").is_file()

    def push_dataset(self, dir: Path) -> None:
        # `create` for the first push, `version` afterwards.
        result = subprocess.run(
            ["kaggle", "datasets", "create", "-p", str(dir), "--dir-mode", "zip"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0 or "already in use" in result.stdout + result.stderr:
            self._run([
                "kaggle", "datasets", "version", "-p", str(dir),
                "-m", "lore work unit", "--dir-mode", "zip",
            ])

    def push_kernel(self, dir: Path) -> None:
        self._run(["kaggle", "kernels", "push", "-p", str(dir)])

    def kernel_status(self) -> str:
        result = self._run(["kaggle", "kernels", "status", self._kernel_ref])
        match = re.search(r'"(\w+)"', result.stdout)
        status = match.group(1) if match else "unknown"
        return status if status in {"running", "complete", "error", "queued"} else "unknown"

    def pull_output(self, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        self._run(["kaggle", "kernels", "output", self._kernel_ref, "-p", str(dest)])

    def run_and_wait(
        self,
        kernel_dir: Path,
        dest: Path,
        on_tick: Callable[[str], None] | None = None,
    ) -> Path:
        self.push_kernel(kernel_dir)
        deadline = time.monotonic() + self._cfg.poll_timeout_s
        while True:
            status = self.kernel_status()
            if on_tick:
                on_tick(status)
            if status == "complete":
                self.pull_output(dest)
                return dest
            if status == "error":
                raise KaggleRunError(f"kernel {self._kernel_ref} finished with error")
            if time.monotonic() > deadline:
                raise KaggleRunError(
                    f"kernel {self._kernel_ref} timed out after "
                    f"{self._cfg.poll_timeout_s}s (last status: {status})"
                )
            time.sleep(self._cfg.poll_interval_s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_kaggle_client.py -v`
Expected: 5 passed.

- [ ] **Step 5: Install the kaggle CLI as a dev tool and verify auth**

Run: `uv add --dev kaggle`, then `uv run kaggle kernels list --mine -p 1 2>&1 | head -5`
Expected: a table (or "No kernels found") — NOT a 401/credentials error. A 401 means Task 0 Step 5 wasn't completed.

- [ ] **Step 6: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/kaggle_api/ tests/test_kaggle_client.py pyproject.toml uv.lock
git commit -m "feat: kaggle client wrapping official CLI with poll/pull round trip

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017yr9NkKfFPPU4kWXNRtrUU"
```

---

### Task 9: Hello Kernel + Dataset (the round-trip payload)

**Files:**
- Create: `kaggle/hello/kernel-metadata.json`, `kaggle/hello/hello_kernel.py`, `kaggle/hello_data/dataset-metadata.json`, `kaggle/hello_data/input.json`, `lore/kaggle_api/hello.py`, `tests/test_hello_roundtrip.py`

**Interfaces:**
- Consumes: `KaggleClient` from Task 8, `KaggleConfig` from Task 3.
- Produces: `run_hello_roundtrip(config: KaggleConfig, work_dir: Path, on_tick=None) -> dict` — materializes the kernel/dataset templates with the user's username, pushes the dataset, runs the kernel on GPU, pulls `output.json`, validates it, and returns the parsed dict `{"sum": 15, "cuda_available": true, "device_name": "..."}`.

- [ ] **Step 1: Write the kernel and dataset templates**

`kaggle/hello_data/input.json`:
```json
{"numbers": [1, 2, 3, 4, 5]}
```

`kaggle/hello_data/dataset-metadata.json` (placeholder `USERNAME` is substituted at runtime):
```json
{
  "title": "lore-hello-data",
  "id": "USERNAME/lore-hello-data",
  "licenses": [{"name": "CC0-1.0"}]
}
```

`kaggle/hello/hello_kernel.py`:
```python
"""LORE Phase 0 hello kernel: prove GPU + dataset transport end to end."""
import json
from pathlib import Path

import torch

data_path = Path("/kaggle/input/lore-hello-data/input.json")
numbers = json.loads(data_path.read_text())["numbers"]

tensor = torch.tensor(numbers, dtype=torch.float32)
if torch.cuda.is_available():
    tensor = tensor.cuda()

result = {
    "sum": float(tensor.sum().item()),
    "cuda_available": torch.cuda.is_available(),
    "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
}
Path("/kaggle/working/output.json").write_text(json.dumps(result))
print(json.dumps(result))
```

`kaggle/hello/kernel-metadata.json` (placeholder `USERNAME` substituted at runtime):
```json
{
  "id": "USERNAME/lore-hello",
  "title": "lore-hello",
  "code_file": "hello_kernel.py",
  "language": "python",
  "kernel_type": "script",
  "is_private": "true",
  "enable_gpu": "true",
  "enable_internet": "false",
  "dataset_sources": ["USERNAME/lore-hello-data"],
  "competition_sources": [],
  "kernel_sources": []
}
```

- [ ] **Step 2: Write the failing tests for the template materializer**

`tests/test_hello_roundtrip.py`:
```python
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from lore.config import KaggleConfig
from lore.kaggle_api.hello import materialize_templates, run_hello_roundtrip


def _cfg() -> KaggleConfig:
    return KaggleConfig(username="realuser", poll_interval_s=0, poll_timeout_s=1)


def test_materialize_substitutes_username(tmp_path: Path) -> None:
    kernel_dir, dataset_dir = materialize_templates(_cfg(), tmp_path)
    meta = json.loads((kernel_dir / "kernel-metadata.json").read_text())
    assert meta["id"] == "realuser/lore-hello"
    assert meta["dataset_sources"] == ["realuser/lore-hello-data"]
    ds_meta = json.loads((dataset_dir / "dataset-metadata.json").read_text())
    assert ds_meta["id"] == "realuser/lore-hello-data"
    assert (kernel_dir / "hello_kernel.py").is_file()
    assert (dataset_dir / "input.json").is_file()


def test_roundtrip_validates_output(tmp_path: Path) -> None:
    expected = {"sum": 15.0, "cuda_available": True, "device_name": "Tesla T4"}

    def fake_run_and_wait(kernel_dir: Path, dest: Path, on_tick=None) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "output.json").write_text(json.dumps(expected))
        return dest

    with patch("lore.kaggle_api.hello.KaggleClient") as cls:
        client = MagicMock()
        client.run_and_wait.side_effect = fake_run_and_wait
        cls.return_value = client
        result = run_hello_roundtrip(_cfg(), tmp_path)

    assert result == expected
    client.push_dataset.assert_called_once()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_hello_roundtrip.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.kaggle_api.hello`.

- [ ] **Step 4: Implement `lore/kaggle_api/hello.py`**

```python
"""Phase 0 hello-world round trip: dataset up, GPU kernel run, output down."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from lore.config import KaggleConfig
from lore.kaggle_api.client import KaggleClient, KaggleRunError

TEMPLATES = Path(__file__).resolve().parents[2] / "kaggle"


def materialize_templates(
    config: KaggleConfig, work_dir: Path
) -> tuple[Path, Path]:
    """Copy kernel/dataset templates into work_dir, substituting USERNAME."""
    kernel_dir = work_dir / "hello"
    dataset_dir = work_dir / "hello_data"
    shutil.copytree(TEMPLATES / "hello", kernel_dir, dirs_exist_ok=True)
    shutil.copytree(TEMPLATES / "hello_data", dataset_dir, dirs_exist_ok=True)
    for meta in (kernel_dir / "kernel-metadata.json",
                 dataset_dir / "dataset-metadata.json"):
        meta.write_text(meta.read_text().replace("USERNAME", config.username))
    return kernel_dir, dataset_dir


def run_hello_roundtrip(
    config: KaggleConfig, work_dir: Path, on_tick=None
) -> dict:
    if not config.username:
        raise KaggleRunError("kaggle.username is not set in lore.toml")
    kernel_dir, dataset_dir = materialize_templates(config, work_dir)
    client = KaggleClient(config)
    client.push_dataset(dataset_dir)
    out_dir = client.run_and_wait(kernel_dir, work_dir / "out", on_tick=on_tick)
    output_file = out_dir / "output.json"
    if not output_file.is_file():
        raise KaggleRunError(f"kernel completed but {output_file} missing")
    result = json.loads(output_file.read_text())
    if result.get("sum") != 15.0:
        raise KaggleRunError(f"unexpected kernel output: {result}")
    return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_hello_roundtrip.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add kaggle/ lore/kaggle_api/hello.py tests/test_hello_roundtrip.py
git commit -m "feat: hello kernel, dataset templates, and round-trip runner

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017yr9NkKfFPPU4kWXNRtrUU"
```

---

### Task 10: `lore doctor` CLI (the one command)

**Files:**
- Create: `lore/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: everything — `LoreConfig` (Task 3), `open_db`/`schema_version` (Task 4), `KuzuStore` (Task 5), `LanceStore` (Task 6), `OllamaProvider` (Task 7), `KaggleClient` + `run_hello_roundtrip` (Tasks 8–9).
- Produces: `app: typer.Typer` exposing `lore doctor [--kaggle]`. Prints one `[ ok ]`/`[fail]` line per check; exits non-zero if any check fails. This is SPEC Phase 0's "one command".

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:
```python
from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from lore.cli import app

runner = CliRunner()


def test_doctor_all_healthy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)  # defaults; no lore.toml
    with (
        patch("lore.cli.OllamaProvider") as ollama_cls,
        patch("lore.cli.KaggleClient") as kaggle_cls,
    ):
        ollama_cls.return_value.healthy = AsyncMock(return_value=True)
        ollama_cls.return_value.aclose = AsyncMock()
        kaggle_cls.return_value.authenticated.return_value = True
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    for check in ("config", "sqlite", "kuzu", "lancedb", "ollama", "kaggle-auth"):
        assert f"[ ok ] {check}" in result.output


def test_doctor_fails_when_ollama_down(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    with (
        patch("lore.cli.OllamaProvider") as ollama_cls,
        patch("lore.cli.KaggleClient") as kaggle_cls,
    ):
        ollama_cls.return_value.healthy = AsyncMock(return_value=False)
        ollama_cls.return_value.aclose = AsyncMock()
        kaggle_cls.return_value.authenticated.return_value = True
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "[fail] ollama" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError` for `lore.cli` (the `[project.scripts]` entry from Task 2 now gets a real target).

- [ ] **Step 3: Implement `lore/cli.py`**

```python
"""LORE CLI. Phase 0 ships a single command: `lore doctor`."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import typer

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.db.sqlite import open_db, schema_version
from lore.inference.ollama import OllamaProvider
from lore.kaggle_api.client import KaggleClient
from lore.kaggle_api.hello import run_hello_roundtrip

app = typer.Typer(no_args_is_help=True)


def _report(name: str, ok: bool, detail: str = "") -> bool:
    tag = "[ ok ]" if ok else "[fail]"
    typer.echo(f"{tag} {name}" + (f" — {detail}" if detail else ""))
    return ok


@app.command()
def doctor(kaggle: bool = typer.Option(False, "--kaggle",
                                       help="also run the free-tier GPU round trip (slow)")) -> None:
    """Verify every Phase 0 subsystem is alive."""
    ok = True

    try:
        config = LoreConfig.load()
        config.ensure_dirs()
        ok &= _report("config", True, f"data_dir={config.data_dir}")
    except Exception as exc:
        _report("config", False, str(exc))
        raise typer.Exit(1)

    try:
        conn = open_db(config.sqlite_path)
        ok &= _report("sqlite", True, f"schema v{schema_version(conn)}")
        conn.close()
    except Exception as exc:
        ok &= _report("sqlite", False, str(exc))

    try:
        store = KuzuStore(config.kuzu_dir / "graph")
        ok &= _report("kuzu", True, f"{len(store.table_names())} tables")
        store.close()
    except Exception as exc:
        ok &= _report("kuzu", False, str(exc))

    try:
        lance = LanceStore(config.lancedb_dir)
        ok &= _report("lancedb", True, f"tables={sorted(lance.table_names())}")
    except Exception as exc:
        ok &= _report("lancedb", False, str(exc))

    provider = OllamaProvider(config.ollama)
    try:
        healthy = asyncio.run(provider.healthy())
        ok &= _report("ollama", healthy,
                      config.ollama.model if healthy else f"no server at {config.ollama.host}")
    finally:
        asyncio.run(provider.aclose())

    client = KaggleClient(config.kaggle)
    ok &= _report("kaggle-auth", client.authenticated(),
                  "" if client.authenticated() else "~/.kaggle/kaggle.json missing")

    if kaggle:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = run_hello_roundtrip(
                    config.kaggle, Path(tmp),
                    on_tick=lambda s: typer.echo(f"       kaggle kernel: {s}"),
                )
            ok &= _report("kaggle-roundtrip", True,
                          f"sum={result['sum']} on {result['device_name']}")
        except Exception as exc:
            ok &= _report("kaggle-roundtrip", False, str(exc))

    raise typer.Exit(0 if ok else 1)
```

- [ ] **Step 4: Run tests, then the real thing**

Run: `uv run pytest tests/test_cli.py -v` → Expected: 2 passed.
Run: `uv run pytest -m 'not live' -v` → Expected: full suite green.
Run: `uv run lore doctor` → Expected: all six checks `[ ok ]`, exit 0. (Requires Ollama up and kaggle.json present — both done in Task 0.)

- [ ] **Step 5: Set the Kaggle username and run the FULL round trip**

Edit `lore.toml`: set `username = "<personal Kaggle username>"` (ask the user if unknown).
Run: `uv run lore doctor --kaggle`
Expected: `[ ok ] kaggle-roundtrip — sum=15.0 on Tesla T4` (may take several minutes queueing; the poll ticks print status). **This is the Phase 0 acceptance test from SPEC §9.** If the kernel errors, pull logs with `uv run kaggle kernels output <user>/lore-hello -p /tmp/klogs` and debug before proceeding.

- [ ] **Step 6: Commit**

```bash
git config user.email   # HARD GATE: must print jatinroopchandani3007@gmail.com
git add lore/cli.py tests/test_cli.py lore.toml
git commit -m "feat: lore doctor command proving all phase 0 subsystems

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017yr9NkKfFPPU4kWXNRtrUU"
```

---

### Task 11: Private GitHub Repo on the PERSONAL Account + Push

**Files:** none — git/GitHub operations only.

**Interfaces:**
- Consumes: the completed repo from Tasks 1–10; `PERSONAL_GH_USER` recorded in Task 0 Step 6.
- Produces: private repo `github.com/<PERSONAL_GH_USER>/LORE` with `main` pushed, remote over the `github-personal` SSH alias.

- [ ] **Step 1: HARD GATE — re-verify the local identity and full history**

```bash
git config user.email                 # must be jatinroopchandani3007@gmail.com
git log --format='%ae' | sort -u      # must list ONLY jatinroopchandani3007@gmail.com
```
If any other email appears in the log, STOP and fix with `git rebase` author rewrite before anything is pushed — pushing a work identity to the personal repo is the exact failure this gate exists to prevent.

- [ ] **Step 2: HARD GATE — re-verify the SSH alias is the personal account**

Run: `ssh -T git@github-personal 2>&1 | head -1`
Expected: `Hi <PERSONAL_GH_USER>!`. If it greets any other username, STOP and ask the user.

- [ ] **Step 3: Create the private repo via gh (switch to the personal account first)**

The user's `gh` CLI is logged into BOTH the work and personal accounts. The active one may be the work account, so ALWAYS switch before any repo operation:

```bash
gh auth status                                  # lists both accounts; note which is active
gh auth switch --user "<PERSONAL_GH_USER>"      # make the personal account active
gh auth status | grep -A2 "<PERSONAL_GH_USER>"  # HARD GATE: personal account now shows "Active account: true"
gh api user --jq .login                         # HARD GATE: must print <PERSONAL_GH_USER>
gh repo create LORE --private --disable-wiki    # create empty private repo (no --source, no --push)
```

Do NOT pass `--source`/`--push` to `gh repo create` — the push happens in Step 4 over the `github-personal` SSH alias, not over gh's own credentials. If `gh auth switch` fails or `gh api user` prints the work username, STOP — do not create the repo — and fall back to asking the user to create an **empty private repo named `LORE`** at github.com while logged into the personal account (no README/license).

After Step 5 completes, restore whatever account was active before, if it was the work one: `gh auth switch --user "<work-username>"` — leaving gh switched could surprise the user's other projects.

- [ ] **Step 4: Add the remote via the personal SSH alias and push**

```bash
git remote add origin "git@github-personal:<PERSONAL_GH_USER>/LORE.git"
git remote -v        # both lines must show github-personal, not github.com
git push -u origin main
```

- [ ] **Step 5: Verify the push landed under the personal account**

```bash
git ls-remote origin main            # returns the pushed commit hash
git log -1 --format='%H %ae'         # hash matches; email is the personal one
```
Also confirm with the user that `github.com/<PERSONAL_GH_USER>/LORE` shows as **Private**.

---

## Phase 0 Exit Criteria (mirror of SPEC §9 Phase 0 "Done when")

1. `uv run pytest -m 'not live'` — entire suite green.
2. `uv run lore doctor` — config, sqlite, kuzu, lancedb, ollama, kaggle-auth all `[ ok ]`.
3. `uv run lore doctor --kaggle` — the programmatic dataset→GPU-kernel→output round trip passes (`sum=15.0`, `cuda_available=true`).
4. Private repo `github.com/<PERSONAL_GH_USER>/LORE` exists with `main` pushed; every commit authored by `jatinroopchandani3007@gmail.com`; remote uses the `github-personal` alias.
