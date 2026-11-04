# Release step 3: each user's own Gemini key

Read `docs/superpowers/specs/2026-11-03-release-design.md` §3 and §5, and the step 1 and 2 reports in `.superpowers/sdd/` (`release-1-report.md`, `release-2-report.md`). Step 2 left a signed-in user with a session but no model access in multi mode. This step lets each user bring their own free Gemini key, and makes every model call for that user use that key and that user's budget.

**Constraints**
- Work in `/home/jatin/LORE-release` on branch `release`, using its own `.venv`. Add dependencies with `uv add` (`cryptography` for AES-GCM).
- Use TDD. Make no network calls. Fake the Gemini key check, and keep `tests/conftest.py` blocking the network.
- **Do not run the full suite.** An eval is running on this machine. Run only the targeted files.
- Commits: git email jatinroopchandani3007@gmail.com, NO trailers (no Co-Authored-By, no Claude-Session), stage files by path, do not push.
- Never use `pkill -f` or `pgrep -f`. Don't touch `~/LORE-r7`, `~/LORE-prose`, `~/.config/lore` or `/home/jatin/LORE/data`. Never print or log a real key.
- Local mode (the default) must stay exactly as today. It keeps using `~/.config/lore/gemini.env` and the existing per-library budget, with no onboarding.

## Task 1: encrypted key storage

- Add an `api_keys` table to the central DB through an Alembic migration (0003). Fields: user_id (unique, FK), provider (`gemini`), `ciphertext`, `nonce`, `key_version`, `last4`, `added_at`, `last_ok_at` and `last_error`.
- `lore/central/keys.py` encrypts with AES-256-GCM, binding the associated data to the user id so a ciphertext can't be moved to another user. The master key comes from `LORE_KEY_ENCRYPTION_KEY` (32 bytes, base64).
  - Support rotation: `key_version` selects the key, and `LORE_KEY_ENCRYPTION_KEY_OLD` decrypts the previous version. Add `lore admin rotate-keys` to re-encrypt.
  - Multi mode refuses to start without the master key, like the other secrets step 2 checks.
- The plaintext exists only in memory, for the lifetime of a client. It is never in logs, traces, error messages, exception reprs or API responses. Add a redaction test that checks for this, using a fake key and the log redaction step 2 added.

## Task 2: onboarding and the key API

- `GET /api/me/key` returns `{present, last4, added_at, last_ok_at, last_error}` and never the key.
- `PUT /api/me/key {key}` validates the format, then checks the key with one cheap request (a minimal `generateContent` on the cheaper model, or a models list). The check goes through an injectable `KeyChecker`, faked in tests.
  - On success, the key is encrypted and stored, and `last_ok_at` is set.
  - An invalid key gets 400 with a friendly message. A key whose check fails for quota or transient reasons is still stored, with a warning.
- `DELETE /api/me/key` removes the key.
- All three routes are CSRF-protected and rate-limited, at 10 PUTs per user per hour.
- **UI.** After sign-in, a user with no key sees a short onboarding screen inside `/app`.
  - It explains why a key is needed, links to Google AI Studio's key page, and says the free tier is about 1,000 requests a day, resetting at midnight Pacific.
  - It has a paste field and a "Check and save" button.
  - It notes that passages and questions go to Gemini under the user's own key.
  - Account settings, in the account menu step 2 added, show the key's last 4 characters and offer Replace and Remove.
  - All strings go in messages.ts.
- Until a key exists, asking and processing return a clear `key_required` error, and the UI routes the user to onboarding.

## Task 3: every model call uses the user's key

- One factory resolves the key for a `CurrentUser`. In local mode it is today's `resolve_api_key`. In multi mode it decrypts that user's key. The web ask path, explore features that call the model, and the worker's job pipeline all build their Gemini clients through it, from `config_for(base, user)` plus the user's key.
  - Search for every construction of the Gemini client: query models, the extractor, beats, codex, narrator, adjudication and the judge. Not one may fall back to the server's key file in multi mode. Add a test that fails if multi mode ever reads `gemini.env`.
- **Per-user budgets.** The existing `gemini_budget` recorder and the answer reserve already live in the per-user library's `lore.db`, because `config_for` points there. Confirm that, and test that two users' budgets are independent.
  - Also write a daily count to a central `usage` table (user_id, day, model, requests), through a migration. It powers a budget readout and admin visibility.
  - Show the user "requests used today / about 1,000" in the account menu.
- **Bad and expired keys.** If Gemini rejects the key (401 or 403) during an ask or a job:
  - set `last_error` on the key;
  - return `key_invalid` to the UI, which prompts the user to replace the key;
  - pause the user's queued jobs rather than failing them, so they can be resumed;
  - show no stack trace.
- **Quota exhaustion** keeps today's behaviour: friendly message, and the Continue button after the reset. It is now per user.

## Tests (all offline)

- Encryption round-trips. A ciphertext with the wrong user id or a tampered byte fails. Rotation works.
- The key API: present and absent states, last4 only, invalid key rejected, CSRF and rate limit enforced, delete.
- Redaction: a fake key never appears in logs, traces, responses or error text, including when the provider raises.
- Factory: local mode reads the key file, unchanged. Multi mode uses the user's key and never the file. User A's requests use A's key; B's use B's.
- Worker: a job builds its models with its own user's key. A 401 pauses that user's jobs only.
- Budgets: per user and independent. Rows are written to the `usage` table.
- Web: onboarding shows without a key and is skipped with one. The settings show last4. `key_required` and `key_invalid` route correctly. Run vitest only on the touched files, plus `tsc --noEmit`, and one `npm run build` at the end.
- Local mode: the existing targeted web and ask tests pass unchanged.

## Finish

Write `.superpowers/sdd/release-3-report.md`, which is gitignored. Cover the SHAs, the test counts, every model-client construction site found and how it is routed, the new env vars, and any concerns.
