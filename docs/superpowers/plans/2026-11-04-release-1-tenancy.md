# Release step 1: the tenancy core

This step comes first in `docs/superpowers/specs/2026-11-03-release-design.md` (§2, §3, §9). Read that spec before starting.

**Goal:** LORE can serve many users from one deployment. Each user gets an isolated library in its own directory. A central Postgres holds the users and the job queue. Book processing runs in a separate worker process. Auth is not part of this step (it is step 2). Here a request's user comes from one pluggable resolver, which local mode and tests fill in.

**Constraints.**
- Work in `/home/jatin/LORE-release` (branch `release`), using its own `.venv`. Add dependencies with `uv add` (sqlalchemy>=2, alembic, psycopg[binary]).
- Use TDD. Make no Gemini or network calls; `tests/conftest.py` blocks them.
- **Do not run the full test suite.** A long eval is running on this machine and memory is tight. Run only the targeted test files.
- Git email is jatinroopchandani3007@gmail.com. Commits carry NO trailers. Stage files by path. Do not push.
- Never use `pkill -f` or `pgrep -f`. Do not touch `~/LORE-r7`, `~/LORE-prose` or `/home/jatin/LORE/data`.
- Keep everything general, and match the existing code style.

## Task 1: the central database (SQLAlchemy and Alembic)

- **Package:** add `lore/central/`.
  - `db.py`: the engine and session factory. The URL comes from `LORE_DATABASE_URL`; the default is a SQLite file for local mode.
  - `models.py`: tables for `users`, `jobs`, `waitlist` and `invites`. Create `jobs` now; `waitlist` and `invites` are schema only for later steps. The fields follow spec §3 and §8a.
  - An Alembic environment and an initial migration.
- The schema uses portable types only, so the same migrations run on SQLite (local mode and tests) and Postgres (prod).
- **Tests:**
  - migrations apply on SQLite;
  - basic create and read for users and jobs.

  Also add a test marked `postgres` that runs the migrations against a throwaway Postgres container (`docker run postgres:16`). It is skipped when docker is unavailable or `LORE_TEST_POSTGRES` is not set. Do not run it by default.

## Task 2: per-user libraries

- **`lore/tenancy.py`:**
  - `user_data_dir(root, user_id)` returns `<root>/users/<user_id>`. Validate the id strictly: a UUID or a safe slug only, never a raw path. Create the directory on first use.
  - `config_for(base: LoreConfig, user_id) -> LoreConfig` returns a copy whose `data_dir` is that directory. Everything else is inherited.
  - A request-scoped current user, `CurrentUser`, behind a single `resolve_user(request)` hook.
    - **Local mode** (the default): one fixed local user, whose library is today's `LORE_DATA_DIR`. This keeps the existing single-user setup working unchanged.
    - **Multi mode** (`LORE_MULTI_USER=1`): the user comes from the request. Step 2 fills this in; for now it comes from a test header that is ONLY enabled in tests.
- **Web app (`lore/web/app.py`):** every route resolves the current user first, then opens stores from `config_for(base, user)`. Route through one helper; no route may build a data path from request input. Watch for any module-level or app-level cached stores (Kuzu, Lance, sqlite connections) that would leak across users, and make them per-user.
- **MCP:** stays local-mode only for the beta. Document it in a comment.
- **Tests:**
  - two users' libraries are isolated: a book ingested for user A is invisible to user B on every read endpoint (library, overview, entity, search, ask with a fake model);
  - path-traversal ids are rejected;
  - local mode is byte-identical to today, so existing web tests still pass.

## Task 3: the worker process and the job queue

- **`lore/worker.py`:** `lore worker` is a CLI command that polls the central `jobs` table.
  - Claim jobs atomically with `UPDATE ... WHERE state='queued' ... RETURNING`, or `SELECT ... FOR UPDATE SKIP LOCKED` on Postgres. Fall back to a simple atomic update on SQLite.
  - Run at most one job per user at a time.
  - Each job runs the existing `run_book` pipeline with `config_for(base, user)`.
  - Report progress through the per-user `book_jobs` row, as today.
- **Web:** `POST /api/books/{id}/continue` and `/move` **enqueue** a central job in multi mode and return 202, instead of running in a background thread. Local mode keeps today's in-process behaviour, so nothing changes for single-user use.
- **Recovery:** stale claimed jobs (no heartbeat) are requeued. Reuse the existing heartbeat logic where possible.
- **Tests:**
  - enqueue, claim and run, using fake stages;
  - two workers never take the same job;
  - per-user serialisation;
  - stale requeue;
  - local mode unchanged.

## Finish

- Write a short `docs/deploy/README.md` section on running web and worker with `LORE_DATABASE_URL` and `LORE_MULTI_USER`.
- The controller reviews the work and runs the full suite after the eval finishes.
