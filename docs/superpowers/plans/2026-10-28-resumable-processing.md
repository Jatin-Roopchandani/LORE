# Resumable processing: implementation plan

Spec: `docs/superpowers/specs/2026-10-28-resumable-processing-design.md`. Read it first. This plan only names the tasks, the order they run in, and the contracts between them.

**Constraints for every task**
- **Worktree and environment.** Work in `/home/jatin/LORE-prose` (branch `prose-answers`) with the venv `.venv/bin/python`. The web code is in `web/`.
- **Development.** TDD. No real Gemini or network calls in tests.
- **Commits.** Use the git email jatinroopchandani3007@gmail.com. Commit messages carry NO trailers. Do not push.
- **Staging.** Stage only your own files, by path.
- **Processes.** Never use `pkill -f` / `pgrep -f`. Do not stop the running `lore ui` on :8766. Do not touch `~/LORE-r6`.
- **Code style.** Match the code around you: its comment style, naming, and the existing module/CLI/API patterns.

## Task 1: the job store and stage runner (foundation)

- **Migration.** Add the `book_jobs` table from spec §3.1, following the existing migration pattern in `lore/db/sqlite.py`.
- **`lore/orchestrator/jobs.py`:**
  - `BookJob` dataclass.
  - `get_job`, `list_jobs`, `start_job(stage, total)`, `advance(done)`, `heartbeat`, `finish_stage(next_stage)`, `stop_quota(reason)`, `fail(reason)`, `done()`, `recover_stale(now, max_age=120s)`.
  - `next_reset_utc(now)`: next midnight Pacific, using `gemini_budget`'s zone handling.
  - `can_continue(job, now) -> (bool, reason)`.
- **Stage runner, `lore/orchestrator/pipeline.py`:**
  - Ordered `STAGES`: chunk, extract, load, disambiguate, beats, ready. Beats is a no-op placeholder until spec B.
  - A `Stage` protocol: `total(ctx)`, `run(ctx, job_ctl)`. The stage skips items that are already done.
  - `class QuotaStop(Exception)`.
  - `run_book(conn, config, book_id, stages, *, now, force)`: drives the job row.
    - A `QuotaStop` sets `waiting_quota` and `resumable_at`.
    - Any other exception sets `failed`.
    - It refuses before `resumable_at` unless `force` is set.
    - It refuses when a fresh `running` row exists.
- **Answer reserve.** Add the setting `[lore.processing] answer_reserve = 60` in config and lore.toml. Add the helper `processing_budget_left(conn, config, today)` from the gemini budget ledger. Stages call `job_ctl.spend_check()`, which raises `QuotaStop` once the remaining requests fall to `answer_reserve` or below.
- **Tests:** fake stages with a fake budget. Cover:
  - the stop sets `waiting_quota` and the right reset time;
  - a resume runs only the missing items;
  - each item runs exactly once across a stop and resume;
  - the reserve stop;
  - stale recovery;
  - the before-reset refusal, and `force`.

## Task 2: extraction saves per chunk (needs Task 1)

- **Partial results.** In `lore/orchestrator/local.py` / `run.py` (Gemini tier), write each chunk's result to `<unit dir>/parts/<chunk_id>.json` (or parquet) as soon as its request returns.
- **Assembly.** Once every chunk in a unit has a part (or is dead), assemble the unit's usual `triplets.parquet`, `embeddings.parquet` and `manifest.json`. The result must be byte-for-byte what an uninterrupted run would write; at minimum, readers must see identical rows.
- **Resume.** A unit that has parts runs only its missing chunks.
- **Old units.** Units without parts keep today's behaviour.
- **Wiring.** An `ExtractStage` for the runner. Items are chunks. It calls `spend_check` before each request. An all-models-exhausted halt becomes `QuotaStop`.
- **Tests:**
  - a quota hit mid-unit, then resume: each chunk is requested at most once, and the assembled artifacts equal those of a single run;
  - a legacy unit is redone whole.

## Task 3: load, disambiguate and the CLI (needs Task 1; parallel with Task 2, touching different files)

- **Stages.** Add `LoadStage` and `DisambiguateStage`, wrapping today's `_load_book_sync` and `disambiguate` flows.
  - Their per-question caches make re-runs free.
  - An all-models-exhausted or 429 failure inside them becomes `QuotaStop`.
- **`lore process <book>`.** Runs `run_book` with all stages.
  - `--resume` continues from the job row.
  - Before the reset it prints "allowance resets at HH:MM (your local time)" and exits 1.
  - `--force` overrides the refusal.
  - Keep `--skip-extract` working.
  - Keep today's printed summaries.
- **`lore books`** shows each book's job state and stage.
- **Tests:** CLI tests in the existing style, using fake stages and a fake budget.

## Task 4: web API (needs Tasks 1–3)

- **`GET /api/books`** returns each book with `{id, title, status, job: {stage, state, done, total, stopped_reason, resumable_at, can_continue}}` plus `gemini: {used_today, limit, answer_reserve}`. Extend the existing route if there is one.
- **`POST /api/books/{id}/continue`** returns:
  - 202 and starts `run_book` in a background task (one job at a time per server);
  - 409 with the reason before the reset or when a job is running;
  - 404 for an unknown book.
- **Stale recovery.** On server startup, recover stale jobs.
- **Tests:** in `tests/test_web_*.py`, using the existing app fixtures and fake stages.

## Task 5: Library panel (needs Task 4's contract; may start once Task 4's types are fixed)

- **Panel.** A Library panel, opened from the books dropdown or header. One row per book:
  - Ready;
  - Processing, with stage, progress bar and done/total;
  - Paused, with the reason, "Continues after HH:MM (your time) — in Xh Ym", and Continue disabled until `resumable_at`, then enabled automatically by the timer;
  - Failed, with the error and Retry;
  - an ingested but unprocessed book shows Process.
- **Gemini line.** Today's Gemini use shown against the limit and the answer reserve.
- **Polling.** Poll `GET /api/books` every 3 s while any job is running.
- **Text and internals.** Every string lives in `messages.ts`. No model names unless show_internals is on.
- **Vitest:**
  - the disabled→enabled countdown, using fake timers;
  - the progress bar;
  - Retry on failure;
  - Process on an unprocessed book;
  - Continue POSTs and then polls.

## Review

- **Per task.** The controller checks each task: commit hygiene, the tests it ran, and a diff skim. Logic-heavy tasks (1, 2) get a Sonnet task review.
- **Final.** One Sonnet whole-branch review of the feature range, then the full suite and the web build.
