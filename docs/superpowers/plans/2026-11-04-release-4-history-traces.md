# Release step 4: server-side history and the trace store

Read `docs/superpowers/specs/2026-11-03-release-design.md` §3 (`chats`, `messages`, `traces`), §6 (History) and §7 (Monitoring), plus the step 1–3 reports in `.superpowers/sdd/`.

There are two goals:
- **History:** a user's chats live on the server and follow them across devices. Today they sit in browser localStorage (`web/src/history.ts`).
- **Traces:** every question leaves a complete trace keyed by a request id, so a user's report ("this answer was wrong") can be investigated. Release requirement: "trace a user's report".

**Constraints**
- Work in `/home/jatin/LORE-release` on branch `release`, using its own `.venv`.
- Use TDD and make no network calls; fake the models.
- **Run targeted test files only.** A Gemini eval is running on this machine. One full `pytest -q -m "not live"` run at the very end is allowed, after everything is committed, and takes about 30 minutes.
- Commits: git email jatinroopchandani3007@gmail.com, NO trailers (no Co-Authored-By, no Claude-Session), stage files by path, do not push.
- Never use `pkill -f` or `pgrep -f`. Don't touch `~/LORE-r8`, `~/LORE-prose`, `~/.config/lore` or `/home/jatin/LORE/data`. Never print or log API keys.
- Local mode keeps working with no sign-in. In local mode, history and traces go into a local central DB (SQLite), so the same code path runs everywhere.

## Task 0: merge the current app into `release` first

`release` branched from `main` at dea0006. Since then `prose-answers` has gained many commits:
- the reading position;
- story recap;
- front and back matter detection;
- codex identity and the spoiler-safe intro;
- the graph layout;
- PROSE_FACTUAL;
- run_pair changes;
- migrations 13–15 in `lore/db/sqlite.py`.

Do this first:
1. Run `git merge prose-answers` into `release`. The branch is local in the same repository, at `/home/jatin/LORE-prose`'s refs.
2. Resolve conflicts carefully. Expect them in:
   - `lore/web/app.py`: release routes everything through the per-user config helper, and prose-answers added `/api/reading-position`, `/api/entity/{id}/intro`, `?spoilers=1` and others;
   - `web/src/api.ts`: release adds CSRF headers;
   - `messages.ts`, `types.ts`, `App.tsx`, `TopBar`;
   - the MCP tools and CLI.
3. Rules for the resolution:
   - every new prose-answers route must go through the release per-user config helper, `config_for`, and the CSRF rules;
   - the new model call (the entity intro) must use the user's key, through step 3's `key_scope`;
   - reading-position storage is per-user, because it lives in each library's `lore.db`;
   - nothing may build a data path from request input.
4. Run the targeted tests of both sides: web, tenancy, auth, keys, reading position, recap, matter, codex intro and explore. Commit the merge.

Report every conflict and how you resolved it. Note: `prose-answers` is still receiving commits (a writer-fidelity fix is in progress). Merge its current tip; a later merge will bring the rest.

## Task 1: chats and messages on the server

- **Central DB, Alembic migration 0004:**
  - `chats(id, user_id, world, title, created_at, updated_at, deleted_at)`;
  - `messages(id, chat_id, role, question, answer_json, depth, world, request_id, created_at)`.
  - `answer_json` holds the final answer payload the UI renders (sections, claims, passages and position note) so a chat reopens exactly, with no model call.
- **API, all per user, CSRF on mutations:**
  - `GET /api/chats` lists recent chats, newest first, paginated;
  - `GET /api/chats/{id}` returns one chat with its messages;
  - `POST /api/chats`;
  - `PATCH /api/chats/{id}` renames;
  - `DELETE /api/chats/{id}` soft-deletes;
  - `/api/ask` takes an optional `chat_id`, appends the question and the final answer when the stream completes, and creates a chat when there is none, titled from the question.
  - A user can never read or write another user's chat. Return 404, not 403, so ids can't be probed.
- **Web:**
  - `history.ts` reads from and writes to the API. It keeps a small localStorage cache for instant first paint, with the server as the source of truth.
  - The history drawer and the Ctrl+K quick switcher use it, and their UI is unchanged.
  - **One-time import:** on first sign-in, if localStorage has chats and the server has none, offer "Import your N saved chats from this browser". It's a one-click POST of each chat, idempotent by a client-generated id.
- **Limits:** at most 500 chats per user (the oldest soft-deleted first), and `answer_json` capped at 1 MB (store a truncated marker past that).

## Task 2: the trace store

- Every `/api/ask` gets a `request_id`, a UUID. It is returned in the first stream event and the response header `X-Request-Id`, and logged on every log line for that request through a contextvar.
- **Central DB, migration 0005:** `traces(request_id PK, user_id, chat_id, message_id, created_at, question, world, depth, decomposition_json, steps_json, calls_json, answer_json, error, timings_json, model_usage_json)`.
  - **`calls_json`:** every model call in order (stage, model, prompt, reply, latency, ok or error). Prompts and replies are capped at 64 KB each, with a truncated marker.
  - **`steps_json`:** the step events the UI shows.
  - **Secrets never enter a trace:** no API keys, no cookies, no session ids. Reuse step 3's redaction and add a test with a fake key flowing through a failing call.
- **Capture point:** wrap the per-request `QueryModels`, as `evals/answer_quality/run_eval.py::_traced` does. Build a shared helper in `lore/` that both evals and web use.
  - Capture is async-safe and per request, with no cross-request leaks under concurrent asks. Test this.
  - A failed answer is traced too, with `error`.
- **Retention:** 30 days, then delete, through a cheap cleanup in the worker loop or at startup. A user deleting their account or a chat deletes the traces linked to it.
- **"Report this answer" in the UI:** a small link under each answer opens a dialog with an optional note, and sends `POST /api/reports {request_id, note}`. Store it in `reports(id, user_id, request_id, note, created_at, status)`. The admin page lists reports and opens the full trace (admin only), rendered readably: the question, the decomposition, each call collapsible, then the answer.
- **Local mode:** traces are written to the local central DB, and `lore trace <request_id>` prints one, which helps debugging now.
- **Privacy:** traces hold book passages and questions. They are visible only to the user's own admin view and to admins. Say so in `/privacy` (step 7 writes the full page; leave a TODO line in the placeholder).

## Tests

- **Chats:** CRUD, ownership (404 across users), ask appending, pagination, limits, soft delete, the import (idempotent).
- **Traces:**
  - every model call is captured in order, including retries and failures;
  - caps;
  - redaction;
  - concurrency isolation, with two concurrent asks producing two separate complete traces;
  - retention cleanup;
  - cascade on chat or account delete.
- **Reports:** a user can report only their own request ids. The admin list works, and a non-admin gets 403.
- **Web:** history reads from the API; the cache paints first; the import prompt shows once; the report dialog. Run vitest on touched files, `tsc --noEmit`, and one `npm run build`.
- **Postgres:** migrations 0004 and 0005 under the opt-in `postgres` marker. Run them if docker is available, otherwise say so.
- **Local mode:** asking with no sign-in still works and now writes history and a trace locally.

## Finish

Write `.superpowers/sdd/release-4-report.md`, which is gitignored. Cover:
- the merge conflicts and their resolutions;
- the SHAs;
- test counts, including the full-suite result;
- the new routes and tables;
- concerns.
