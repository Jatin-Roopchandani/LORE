# Release step 5: uploading books in the browser

Read `docs/superpowers/specs/2026-11-03-release-design.md` §6 ("Adding books") and the step 1–4 reports in `.superpowers/sdd/`.

**Today:** "+ Add book" opens `web/src/components/AddBookDialog.tsx`. It picks a file and a world, suggests a world, then shows a `lore ingest <file> --world <name>` command for the user to run in a terminal. Processing runs through `lore/ingest/registry.py::ingest_book` and the resumable `book_jobs` stages. In multi mode, those stages run in the worker (`lore/worker.py`) through the central job queue.

**Goal:** the user uploads the file in the browser and processing starts. Progress shows in the Library panel, using the existing per-book job state and Continue button. Local mode gets the same upload too.

**Constraints**
- Work in `/home/jatin/LORE-release` on branch `release`, using its own `.venv`.
- TDD. No network calls; fake the models.
- **Run targeted tests only.** A Gemini eval runs from `~/LORE-r8`. One full `pytest -q -m "not live"` run is allowed at the very end, after everything is committed.
- Git email jatinroopchandani3007@gmail.com. NO trailers: no Co-Authored-By, no Claude-Session. Stage files by path. Do not push; a guarded auto-push does that.
- Never use `pkill -f` or `pgrep -f`. Don't touch `~/LORE-r8`, `~/LORE-prose`, `~/.config/lore` or `/home/jatin/LORE/data`. Don't start or stop any `lore ui`, because the user's UI runs from this worktree. Never print API keys.
- **Processing costs Gemini requests.** Uploading must never start extraction silently past the user's budget. Reuse the existing budget checks and the "Continue after the reset" behaviour.

## Task 1: the upload endpoint

- `POST /api/books/upload` takes multipart form data: `file` and `world` (an existing world id, or a new world name). It is CSRF-protected, per-user (`library_of(user)`) and rate-limited. Go through step 4's `lore/web/limits.py` with a route-specific limit of 50 MB; every other route keeps its small default.
- **Validation, before anything is stored for real:**
  - **Extension and sniffing:** the file must be `.epub` or `.pdf`, sniffed by magic bytes as well, not just by name or by the Content-Type header.
  - **EPUB:** the file must be a valid zip with the `mimetype` entry. Guard against zip bombs: cap the entry count, the total uncompressed size (for example 300 MB) and the compression ratio. Reject entries with absolute paths, `..` or symlinks. Reject encrypted entries. Never extract to disk with entry names; read through the existing epub parser, or verify the parser does not write entry paths.
  - **PDF:** it must have a `%PDF-` header and open with the existing PDF reader. Cap the page count, and reject encrypted PDFs with a friendly message.
- **Storage:** stream the body to a temporary file inside the user's own `inbox/` (`config_for(base, user).data_dir / "inbox"`), never to a path built from the uploaded filename. The stored name is a server-generated id; the original filename is kept only as display metadata, after sanitising.
  - **Duplicates:** compute the content hash. If the same book is already in the user's library, return 409 `already_added` with its book id, matching the existing `ingest_book` duplicate behaviour.
- **Then:** register the book with `ingest_book` and queue processing.
  - **Local mode:** use the existing in-process runner (the same path the Library Continue button uses).
  - **Multi mode:** enqueue a central job and return 202 with the book id.
  - **If the budget is spent,** the book is registered and waits, with a clear `waiting` state and the reset time.
- **Quotas per user, in multi mode:** at most 10 uploads per day and at most 30 books in the library, both configurable. Errors use friendly codes such as `too_large`, `not_a_book`, `encrypted`, `too_many_books` and `daily_limit`.

## Task 2: the dialog and progress

- **AddBookDialog becomes a real upload:**
  - a drop zone plus a file picker;
  - the world picker, kept with its suggestion;
  - an Upload button with a progress bar for the bytes (XHR, or fetch with a progress fallback);
  - then "Processing started", plus a link that opens the Library panel at that book.
  - Every error code maps to a friendly message.
  - Keep the CLI command as a small "Or add it from the terminal" disclosure, local mode only.
- **The Library panel** already shows per-book stages, progress and Continue. Make sure a freshly uploaded book appears at once (refetch or optimistic insert) and its progress updates while open.
- All strings go in `messages.ts`. Use theme tokens only.
- Show the upload limits in the dialog: 50 MB, EPUB or PDF, books you own.

## Tests

- **Endpoint:**
  - happy path for EPUB and PDF;
  - wrong extension and wrong magic bytes;
  - too large, checked by Content-Length and by streamed bytes;
  - a zip bomb (high ratio, too many entries, too large in total);
  - path traversal (`../`) and absolute entry names, plus symlink entries;
  - an encrypted EPUB and an encrypted PDF;
  - a duplicate (409);
  - CSRF missing (403);
  - signed out (401) in multi mode;
  - per-user isolation (A's upload is never in B's library);
  - quotas;
  - local mode runs in-process and multi mode enqueues;
  - the budget-spent path waits.

  Build the bad files in the tests.
- **Web:** the drop zone, the picker, progress, each error message, the success link, and the CLI disclosure only in local mode. Run vitest on touched files, `tsc --noEmit`, and one `npm run build` at the end.

## Finish

Write `.superpowers/sdd/release-5-report.md` (gitignored), covering:
- the SHAs;
- the validation rules and their limits;
- test counts, including the full suite;
- the new env vars;
- concerns.
