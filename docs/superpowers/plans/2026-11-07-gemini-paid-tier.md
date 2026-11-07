# Paid Gemini keys: stop treating every Gemini key as free tier

Agreed with the user on 2026-11-07.

**The problem:** every Gemini key goes through the free-tier budget (`lore/orchestrator/gemini_budget.py`: about 500 requests per model per day, plus the answer reserve). A user on a paid Gemini plan is capped by LORE at about 1,000 requests a day for no reason.

**The fix:** a per-user, per-key setting, "I'm on a paid Gemini plan", off by default.

Read the AI providers work first: `docs/superpowers/plans/2026-11-07-ai-providers.md`, `.superpowers/sdd/ai-providers-report.md`, `lore/inference/providers/` (the factory), the `api_keys` table and migration 0010, `/api/me/providers` and `/api/me/key`, the usage and token-cap code, and the processing estimate. Then read the Settings popup work, which landed just before this: the AI providers tab hosts `ProviderSettings`.

## What to build

- **Storage:** a `paid_tier` boolean on the Gemini `api_keys` row, through an Alembic migration (default false), plus the local-mode equivalent `[lore.llm] gemini_paid = true`.
- **Behaviour when on.** Gemini is handled like Claude and OpenAI:
  - no free-tier daily request cap and no answer reserve;
  - tokens recorded per request from the response's usage metadata, into the same central `usage` table, with an estimated cost from a small Gemini price table for the models LORE uses. Get each price from Google's current pricing page if you can. If you can't verify a price, show tokens only for that model, and say so in the report; never guess a price;
  - the optional daily token cap applies;
  - the processing estimate, with the same "ask above $1" confirmation.
- **Behaviour when off:** today's free-tier behaviour exactly, byte for byte. Prove it with the existing Gemini golden test plus a budget test.
- **The cascade:** the paid tier may still use the same two Flash-Lite models. Don't change the model choice in this task.
- **API:** `PUT /api/me/provider` (or the key route) accepts `gemini_paid: bool`, CSRF-protected, and `GET /api/me/providers` returns it.
- **UI:** a switch on the Gemini card in `ProviderSettings`, labelled "I'm on a paid Gemini plan", with a short explanation: "Removes LORE's free-tier daily limit and shows token usage and estimated cost instead. Google bills you for usage."
  - **The hint:** if a user on the free setting has gone past the free-tier daily request count today with no 429 from Google, show a dismissible hint: "Your key seems to be on a paid plan. Turn on 'I'm on a paid Gemini plan' to remove LORE's daily limit." Never switch automatically.
- **Usage panel:** Gemini paid shows tokens and estimated cost; Gemini free keeps its request count and reset time.

## Tests

- **Paid on:** no request cap, no reserve, tokens recorded, cost estimated (or tokens only where no price is verified), cap enforced, estimate shown.
- **Paid off:** unchanged (golden plus budget).
- **API:** CSRF, auth and per-user isolation for the switch; migration up and down.
- **The hint:** shown only under its condition, and dismissible.
- **Web:** vitest for the switch, the usage panel per mode and the hint; `tsc --noEmit`, and one `npm run build`.

## Rules

- Work in `/home/jatin/LORE-release` on branch `release`, using its own `.venv`. TDD, no network calls in tests.
- **Run targeted tests only.** One full `pytest -q -m "not live"` run is allowed at the end, after committing.
- Git email jatinroopchandani3007@gmail.com. NO trailers. Stage files by path. Do not push.
- Never use `pkill -f` or `pgrep -f`. Don't touch `~/LORE-trial`, `~/LORE-signin`, `~/LORE-prose`, `~/LORE-r8`, `~/.config/lore`, `/home/jatin/LORE/data`, `/home/jatin/lore-multi` or `~/.oci`. Never print keys. Keep disk use small.

## Report

Write `.superpowers/sdd/gemini-paid-report.md` (gitignored), covering:
- the SHAs;
- the price table and its sources, or which prices could not be verified;
- test counts;
- concerns.
