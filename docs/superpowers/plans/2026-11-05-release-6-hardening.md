# Release step 6: hardening

Read `docs/superpowers/specs/2026-11-03-release-design.md` §4–§8 and every step report in `.superpowers/sdd/` (release-1 to release-5). Each report lists concerns, and this step closes the ones marked below. Everything here applies to **multi mode**; local mode stays as it is unless a task says otherwise.

**Constraints**
- Work in `/home/jatin/LORE-release` on branch `release`, using its own `.venv`. TDD, no network calls.
- **Run targeted tests only.** A Gemini eval runs from `~/LORE-r8`. One full `pytest -q -m "not live"` run is allowed at the very end, after everything is committed.
- Git email jatinroopchandani3007@gmail.com. NO trailers: no Co-Authored-By, no Claude-Session. Stage files by path. Do not push; a guarded auto-push does that.
- Never use `pkill -f` or `pgrep -f`. Don't touch `~/LORE-r8`, `~/LORE-prose`, `~/LORE-home`, `~/.config/lore` or `/home/jatin/LORE/data`. Don't start or stop any `lore ui`; the user's UI runs from this worktree. One `npm run build` at the end is fine. Never print API keys.
- **Another agent is building the homepage at the same time,** in a separate worktree (`~/LORE-home`, branch `homepage`). It owns the public pages: `/`, `/privacy` and `/terms`. Don't edit those page components or their routes beyond what a task here needs.

## Task 1: hide internals

- In multi mode, `show_internals` is forced off.
  - **No model names, prompts or raw provider errors:** none in the UI, in API errors or in stream events (`step` details, `note`s, error messages).
  - **Friendly errors only:** every exception is mapped to a friendly code and message, with a request id the user can quote ("Something went wrong. Reference: <request_id>").
  - **No stack traces:** none in any response.
- The MCP server is off in multi mode. Document this, and refuse to start it with `LORE_MULTI_USER=1`.
- **Tests:** force failures from each layer (provider, parser, Kuzu, unexpected exceptions). Responses must hold no model names, file paths, tracebacks or key fragments.

## Task 2: rate limits and concurrency

Per user, configurable through env vars, with sensible defaults:
- questions per minute (6), and at most 1 question in flight at once;
- `/api/entity/{id}/intro` model calls per hour (60);
- report submissions per hour (20).

On top of that:
- a global cap on questions in flight across all users (8) and on uploads being parsed at once (2);
- past a limit, a 429 response with `retry_after` and a friendly message;
- the existing central-DB counter style, safe across several web processes.

**Tests:** each limit, its reset, isolation between users and the global caps.

## Task 3: security headers and sessions

- **Content-Security-Policy:**
  - `default-src 'self'`;
  - fonts, images and styles only as the app needs them (`data:` images, `'self'` styles; check the built app for inline styles and adapt);
  - no inline scripts (check the Vite build);
  - `frame-ancestors 'none'`.
- **Other headers:** `Referrer-Policy: strict-origin-when-cross-origin`, `X-Content-Type-Options: nosniff`, `Permissions-Policy` (off for camera, microphone and geolocation), and HSTS when `LORE_BASE_URL` is https.
- **Session lifetime:** an absolute cap (30 days from creation, whatever the sliding expiry says) and an idle timeout (7 days). Rotate the session id on sign-in.
- **Tests:** the headers are present on HTML, API and static responses, and both session caps are enforced.

## Task 4: admin controls and account deletion

- **Admin actions:** disable a user, which revokes all their sessions at once, refuses sign-in and pauses their jobs; re-enable a user; revoke one user's sessions. Each has an admin API route and buttons on `/admin`, and every admin action is logged in an `admin_events` table.
- **Account deletion:**
  - a "Delete my account" action in the account menu, behind a typed confirmation;
  - it deletes the user's data directory, central rows (sessions, identities, keys, chats, messages, traces, reports, usage, jobs) and their encrypted key;
  - it is idempotent and safe to resume if interrupted, and is also available to admins;
  - waitlist rows matching that email are anonymised.
- **Tests:** a cascade test covering every table, the files gone, other users untouched, and a non-admin getting 403.

## Task 5: closing earlier concerns

Each item comes from an earlier step's report.
1. **Magic links (step 2).** `GET /auth/email/verify` shows a "Continue signing in" page. Only its `POST` consumes the token, so mail scanners that prefetch links can't spend it.
2. **Invites tied to the approved email (step 2).** An invite created by approving a waitlist entry works only for that email. Admin-created hand-out invites stay unbound unless an email is given.
3. **Sandboxed parsing (step 5).** EPUB and PDF parsing on upload runs in a subprocess with resource limits: CPU seconds, address space (for example 1 GB) and a wall-clock timeout. Failures map to `not_a_book` or `too_complex`. The web process never parses untrusted files itself. Keep the step 5 tests passing and add timeout and memory-limit tests.
4. **The trace quota (step 4).** Each user keeps at most N traces (for example 2,000), oldest first, on top of the 30-day expiry.
5. **Postgres.** Run the opt-in `postgres` tests (steps 1–6 migrations, claims, keys). Docker has the `postgres:16` image cached; don't pull anything new. Report the result.

## Finish

Write `.superpowers/sdd/release-6-report.md` (gitignored), covering:
- the SHAs;
- every limit and its env var;
- the CSP as shipped;
- test counts, including the full suite and Postgres;
- remaining concerns.
