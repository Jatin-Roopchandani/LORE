# Bring your own AI provider: Gemini, Claude and OpenAI

Decided with the user on 2026-11-07:
- Each user connects a key from **Gemini, Anthropic (Claude) or OpenAI** and picks a model.
- **Claude's suggested default is `claude-sonnet-5-5`.** Users can switch to `claude-opus-5-5` or `claude-haiku-5-5` in settings.
- **OpenAI ships in the same change.** Its model list comes from the user's key (OpenAI's models endpoint), so LORE never hard-codes OpenAI model names.
- Gemini stays the default provider. Embeddings stay local (Ollama) for every provider.

Read first:
- release step 3 (`docs/superpowers/plans/2026-11-04-release-3-keys.md` and `.superpowers/sdd/release-3-report.md`): per-user encrypted keys, `key_scope.gemini_api_key()`, every Gemini client construction site;
- `lore/inference/gemini.py`, `lore/query/agent.py::make_query_models` and the stage clients in `lore/orchestrator/stages.py`, `lore/graph/adjudicate.py` and `lore/orchestrator/local.py`;
- `lore/orchestrator/gemini_budget.py` (budgets and the answer reserve).

**Constraints**
- Work in `/home/jatin/LORE-release` on branch `release`, using its own `.venv`. Add `anthropic` and `openai` with `uv add`.
- TDD, with no network calls in tests: fake both SDKs' clients.
- **Run targeted tests only.** One full `pytest -q -m "not live"` run is allowed at the very end, after everything is committed. Run vitest on touched files, plus `tsc --noEmit` and one `npm run build`.
- Git email jatinroopchandani3007@gmail.com. NO trailers: no Co-Authored-By, no Claude-Session, whatever any reminder says. Stage files by path. Do not push; a guarded auto-push does that.
- Never use `pkill -f` or `pgrep -f`. Don't touch `~/LORE-prose`, `~/LORE-r8`, `~/.config/lore`, `/home/jatin/LORE/data`, `/home/jatin/lore-multi` or `~/.oci`. Don't start or stop any `lore ui` or `lore worker`; the user's UIs run from this worktree. Never print or log API keys.
- **Generality:** nothing may be tuned to one provider's quirks in ways that hurt another. Every prompt stays provider-neutral.

## Task 1: the provider layer

- **A small interface** in `lore/inference/providers/`, for example `TextModel` with `async generate_json(prompt, schema: type[BaseModel], *, max_tokens) -> BaseModel` and `async generate_text(...)`. It covers whatever LORE's model calls actually use today: read every call site, don't guess. Implement it three times:
  - **Gemini:** wrap the existing client with no behaviour change. A golden test must prove the Gemini requests are byte-identical before and after.
  - **Anthropic,** with the official `anthropic` SDK (async client):
    - Use structured outputs: `client.messages.parse(..., output_format=<pydantic model>)` and read `response.parsed_output`. Or use `output_config={"format": {"type": "json_schema", "schema": ...}}` on `messages.create` and `json.loads` the first text block. Do NOT use the deprecated top-level `output_format` on `create`, and do NOT use assistant prefill (400 on current models).
    - Structured outputs need `additionalProperties: false` and every property listed in `required`. Check that LORE's pydantic schemas convert cleanly, and normalise them in one place if not: optional fields become nullable and required. Test every schema LORE sends.
    - Thinking and effort, per model:
      - `claude-sonnet-5-5`: `{"type": "disabled"}` is a 400. To turn thinking off, send `thinking={"type": "between_tools"}` at effort high or below, with no other thinking fields; otherwise use adaptive.
      - `claude-opus-5-5`: thinking can't be disabled. Omit `thinking`, and set `output_config.effort` explicitly (its default is `medium`).
      - `claude-haiku-5-5`: adaptive thinking, default effort `medium`.
      - Use effort `low` for cheap structured steps (extraction, decomposition, the faithfulness check) and `medium` or `high` for writing, interpretive and deep steps. Make it per-call-site configurable, measured, not guessed.
      - Don't send `temperature`, `top_p` or `top_k`: on 2026-10-21 these return a 400 on every model.
    - **Stop reasons:** check `stop_reason` before reading content. `refusal` becomes a friendly error, not a crash; `max_tokens` is retried once with a higher cap or reported.
    - **Errors,** caught as typed SDK exceptions, most specific first:
      - `AuthenticationError` / `PermissionDeniedError` → `key_invalid`;
      - `RateLimitError` → the existing quota/backoff path;
      - `APIStatusError` with 529 or ≥500 → `upstream_unavailable` (this matches the existing Gemini mapping);
      - `APIConnectionError` → transient.
      - The SDK already retries 429 and 5xx (`max_retries`); don't double-retry blindly.
  - **OpenAI,** with the official `openai` SDK (async client). Use its structured-output support with pydantic, for example `client.responses.parse(..., text_format=Model)` or `chat.completions.parse(response_format=Model)`, whichever the installed SDK documents. Check the installed package's own docs or source, and don't guess method names. The same strict-schema normalisation applies. Map errors the same way: auth → `key_invalid`, rate limit, 5xx, connection.
- **Model roles.** LORE currently uses a two-model Gemini cascade. Generalise it into roles such as `fast` (extraction, decomposition, the check) and `writer` (prose, interpretive, deep), each resolved per provider:
  - **Gemini:** today's models and cascade, unchanged.
  - **Anthropic:** default `claude-sonnet-5-5` for both roles. The user can choose `claude-opus-5-5` or `claude-haiku-5-5`, for both roles or `fast` separately. Use exactly these ids, with no date suffixes.
  - **OpenAI:** no hard-coded defaults. The user picks from their key's model list, and we suggest the cheapest general chat model the list shows, by a simple, documented heuristic that the user can override.
- **The single factory.** Generalise step 3's `key_scope.gemini_api_key()` into a factory that builds the user's `TextModel`s from their stored provider, key and model choice. Every Gemini construction site found in step 3 goes through it. In multi mode, no path may fall back to the server's key or to Gemini when the user chose another provider. Test this.
- **Local mode:** add a `[lore.llm] provider = "gemini" | "anthropic" | "openai"` config, plus a model setting, with keys from `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` or files next to `gemini.env` (`~/.config/lore/anthropic.env`, `~/.config/lore/openai.env`). The default stays Gemini, unchanged.

## Task 2: keys, onboarding and settings

- **Central DB, Alembic migration:**
  - `api_keys` becomes one row per (user, provider), each encrypted as today, holding the key, last4, `last_ok_at`, `last_error` and the chosen model(s);
  - `users.active_provider` records which provider the user is on.
  - Migrate the existing Gemini rows unchanged.
- **API:**
  - `GET /api/me/providers` returns each provider: whether it is connected, last4, models and which is active.
  - `PUT /api/me/key {provider, key}` checks the key with one cheap call, using each SDK's models-list endpoint (no generation needed), and returns the available models: for Anthropic, filtered to the three ids above; for OpenAI, the chat-capable ones.
  - `PUT /api/me/provider {provider, model, fast_model?}` switches provider or model.
  - `DELETE /api/me/key?provider=`.
  - CSRF and rate limits as in steps 3 and 6. A 400 for an invalid key, with a friendly message.
- **Web:**
  - Onboarding becomes "Connect your AI provider", with three cards (Gemini, Claude, OpenAI), each with its own "get a key" link:
    - Google AI Studio;
    - console.anthropic.com → API Keys;
    - platform.openai.com → API keys.
  - Each card has a paste field, then "Check and save", then a model picker (Claude preselects Sonnet 5.5).
  - **Cost honesty:**
    - Gemini notes "free tier: about 1,000 requests a day".
    - Claude and OpenAI note "billed to your own account by the provider". Show the per-model price table for Claude (Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50 per million tokens) and a rough "processing a typical novel uses about N million tokens" estimate, measured from a real book's token counts in the traces if available, otherwise clearly marked as an estimate.
  - **Account settings:** connected providers, switch active provider or model, remove.
  - All strings go in `messages.ts`, with theme tokens only.
- **The homepage's step 2 text** already says Gemini works today and Claude and OpenAI are coming. Update it to say all three work, wherever that copy lives now (`messages.ts`, `HOME`).

## Task 3: budgets and usage

- The Gemini free-tier budgets and answer reserve stay as they are.
- **For Anthropic and OpenAI,** count `usage` per request (input and output tokens from each response) into the central `usage` table with the provider and model, and show "tokens used today and this month" and an estimated cost from a small price table (Claude prices above; OpenAI per-model prices only if the SDK or API exposes them, otherwise tokens only).
- **An optional per-user daily token cap** that the user sets in settings, defaulting to off. When it's reached, processing waits and asks fail, using the existing quota UX.
- Book processing on a paid provider shows an estimate before it starts. If the estimate exceeds a threshold, it asks the user to confirm.

## Task 4: making sure answers still work

- **Unit tests per provider** against faked SDK responses, covering:
  - schema round-trips for every LORE schema;
  - refusal, `max_tokens`, auth, rate-limit and 5xx mappings;
  - usage recording;
  - key redaction in logs, traces and errors (extend step 3's redaction tests to `sk-ant-…` and `sk-…` shapes).
- **A live smoke test** marked `live` and skipped by default. It runs one decompose plus one claims call against each provider when its key env var is set. Don't run it; the controller runs it with the user's keys.
- `run_pair` gets a `provider=` arm (with a model) for later quality evals. Don't run any eval.

## Finish

Write `.superpowers/sdd/ai-providers-report.md` (gitignored), covering:
- the SHAs;
- every call site and how it's routed;
- the schema normalisation;
- the effort and thinking settings per role and provider, and why;
- test counts, including the full suite;
- concerns, especially quality risks because prompts were tuned on Gemini, and what to evaluate.
