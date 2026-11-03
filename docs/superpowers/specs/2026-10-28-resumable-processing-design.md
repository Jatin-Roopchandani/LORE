# Resumable book processing: saved progress and a Continue button

**Status:** draft, 2026-10-28. Decisions were taken in chat; this spec is pending the user's review.

**Goal:** when the Gemini daily allowance runs out while a book is being processed, nothing is lost. Processing stops cleanly at a saved point. The UI shows how far it got and when the allowance resets. A **Continue** button becomes active after the reset and resumes from exactly where processing stopped. Restarting from the beginning is never needed.

**Done when:**
- A book whose processing hits the daily limit at any stage can be finished across several days.
  - Every Gemini request is spent at most once, except the single request that was in flight when the limit hit.
- The web UI lists each book's processing state, with stage, progress, the reason it stopped and when it can continue.
  - The UI offers Continue, which is disabled until the reset and enabled after it.
- `lore process <book> --resume` from the CLI resumes the same saved state.
- Any future stage that spends Gemini requests gets this behaviour by using the same mechanism. The first such stage will be the story beats (separate spec).

---

## 1. Today

How a book is processed now:

- **`lore ingest`** chunks the book. It makes no Gemini requests.
- **`lore process`** runs extraction and then the load.
  - **Extraction** makes one Gemini request per chunk, grouped into work units of 10 chunks. Finished units are recorded in `jobs`, and `--resume` skips them.
  - When the allowance runs out, the run stops (`_UnitHalted`). The partly finished unit is redone in full on `--resume`, so up to 9 requests are paid twice. The graph is deliberately not loaded from a half-extracted book.
  - **The load** resolves entities, writes the graph and the vectors, asks the adjudication and speaker questions, and records the disambiguation questions. Each of those Gemini answers is cached individually in `data/adjudication`, `data/speakers` and `data/disambiguation`, so re-runs are free.
- **`lore disambiguate`** answers the recorded questions in one request. A further `lore process --skip-extract` then applies the answers.

**Gaps:**
1. Resuming is manual and CLI-only.
2. A partly finished extraction unit is paid for twice.
3. There is no single place recording "where is this book": `jobs` rows, cache files and pending files each hold part of the state.
4. There is no UI for any of it.

## 2. Decisions (taken in chat)

1. **Saved progress for every stage**, at the finest useful grain: per chunk, per question, and per chapter for the beats.
2. **A Continue button, not automatic resume.** It is disabled while the allowance is spent and enabled once the daily limit has reset. Nothing spends requests without the user asking.
3. **The CLI keeps working.** `lore process --resume` resumes the same state.
4. **Generic.** A stage declares its items and saves each result as it completes. The job runner handles stopping, the state and resuming.

## 3. Design

### 3.1 The book job

There is one **book job** per book: a row in a new `book_jobs` table, added by a migration.

| Field | Meaning |
|---|---|
| `book_id` | The book. |
| `stage` | The current stage. |
| `state` | `running`, `waiting_quota`, `failed`, `done` or `idle`. |
| `done_items`, `total_items` | Progress within the current stage. |
| `stopped_reason` | Why the job stopped, in plain text. |
| `resumable_at` | UTC time of the next quota reset. Set only when the state is `waiting_quota`. |
| `updated_at` | Last change. |

The stages run in a fixed order. Each one is idempotent and skips the items it has already done:

1. **chunk.** No Gemini requests. It is already idempotent.
2. **extract.** Items are chunks.
3. **load.** The graph, vectors, adjudication and speakers. Items are the questions; their answers are already cached one by one.
4. **disambiguate.** The one request, then the reload that applies the answers.
5. **beats.** Items are chapters. This stage arrives with spec B.
6. **ready.**

`books.status` stays as it is for the existing readers. The job row is the detail behind it.

### 3.2 Per-item saves

**extract.**
- Each chunk's triplets and embeddings are saved as soon as that chunk's request returns. They go into the unit's directory as per-chunk part files.
- The unit's final artifacts (`triplets.parquet`, `embeddings.parquet` and `manifest.json`) are assembled from the parts when every chunk in the unit is done. Existing readers stay unchanged.
- On resume, a unit with parts runs only its missing chunks.
- At most one in-flight request is lost.

**load and disambiguate.** These already cache per question. The change is that a stop caused by the quota now sets the job to `waiting_quota` and does not exit with an error.

**beats.** A per-chapter cache, keyed by the prompt. This is defined in spec B.

### 3.3 Stopping on quota

- **What triggers a stop:**
  - any stage whose Gemini call fails because **every model in the cascade** is out of daily quota (the existing budget ledger and `upstream` errors tell us this);
  - **or** the processing reserve is reached (see 3.4).
- **What a stop does:**
  - finishes the item in hand, if it can;
  - saves its state;
  - sets `state=waiting_quota`, writes a `stopped_reason` such as "Gemini daily limit reached during extraction (412/1,180 chunks)", and sets `resumable_at` to the next midnight Pacific (`gemini_budget.today`'s zone) in UTC.
- Transient errors (a 503 or a network failure) are not quota stops. They keep today's retry behaviour, and only a persistent failure becomes `failed`.

### 3.4 Answering stays usable

**The problem.** Processing a book can use up the whole day's allowance. If it did, questions in the UI would stop working until the reset.

**The fix.** Processing stops while it still leaves a reserve of the daily requests, `[lore.processing] answer_reserve`, default 60 (about 8 questions). Questions can use the reserve; processing cannot. This is configurable, and it is shown in the UI.

### 3.5 Continue

- **CLI:** `lore process <book> --resume` resumes from the saved stage and items.
  - Before the reset it refuses with "allowance resets at HH:MM (your time); try again then". `--force` overrides this, for a paid key or a quota that was raised.
- **Web:** `POST /api/books/{id}/continue` starts the job in a background task in the `lore ui` server. One job runs at a time across the server.
  - It returns 409 when `now < resumable_at` or when a job is already running.
- **Server-side progress:** the job row is updated as items complete. `GET /api/books` returns each book's job state, and the UI polls it every few seconds while any job is running.

### 3.6 UI

**A Library panel**, reached from the existing books dropdown or header. It lists each book with:

- **Title and status:** Ready, Processing (stage, a progress bar and done/total), Paused (daily limit), or Failed.
- **When paused:** the stop reason, plus "Continues after HH:MM (your time) — in 7h 12m". The Continue button stays disabled until then and enables itself when the time passes; the page timer re-checks.
- **When failed:** the error and a Retry button, which uses the same endpoint.
- **Today's Gemini use:** shown against the limit and the answer reserve.

All strings go in `messages.ts`. Model names appear only with show_internals.

**Out of scope:** uploading a book from the UI (Phase 5b). Books are still added with `lore ingest`. Starting processing from the UI for a book that has been ingested but not yet processed is in scope: its job starts at the extract stage and uses the same Continue endpoint, so the button is labelled "Process".

### 3.7 Concurrency and crashes

- **A crash or restart while running:** the row says `running` but no live job exists. On server start, a stale `running` row (no heartbeat for 2 minutes) becomes `idle` with the reason "interrupted". Continue then resumes it; it does not wait for a reset.
- **The same book in two places:** the CLI and the web server use the same database. A `running` row with a fresh heartbeat blocks a second runner, from either side.

## 4. Testing

- **Stage runner:** use fake stages and a fake Gemini that hits the quota after N requests. Check that:
  - the state becomes `waiting_quota` and `resumable_at` is the next midnight Pacific;
  - a resume runs only the missing items;
  - across stop and resume, each item's request is made exactly once, except the one in flight.
- **Extract:**
  - per-chunk parts are saved;
  - a unit with parts resumes only the missing chunks;
  - the assembled unit matches the artifacts of a single uninterrupted run.
- **Reserve:** processing stops at limit − reserve, and answering still gets requests.
- **CLI:** `--resume` refuses before the reset, and `--force` overrides.
- **API:** `continue` returns 409 before the reset and when a job is already running, and 202 otherwise. `GET /api/books` returns the job state.
- **Stale-run recovery:** a stale `running` row is recovered on server start.
- **Frontend (Vitest):**
  - Continue is disabled with a countdown before the reset, and enabled after it;
  - the progress bar renders;
  - Retry appears on failure.
- No real Gemini or network calls in tests.

## 5. Risks

- **Clock and zone.** The reset is midnight Pacific. All of this uses `gemini_budget`'s zone handling and stores UTC.
- **Real quota differing from the ledger.** The ledger is our count, and Google's count may differ. A 429 is always treated as a quota stop, whatever the ledger says.
- **Part files left by older runs.** Units extracted before this change have no parts. They are treated as all-or-nothing, as they are today.
