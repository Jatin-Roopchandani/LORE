# Release: LORE at lore.jrvn.site, private beta

**Status:** draft, 2026-11-03. The decisions below were taken in chat. This spec still needs the user's review.

**Goal:** run LORE on the internet at **https://lore.jrvn.site** for an invite-only group. Each person gets their own private library and signs in with Google, GitHub or an email link. Each person pays nothing because they use their own free Gemini key. Hosting is free or close to it.

## Decisions (taken in chat)

1. **Audience: private beta.** Invite-only sign-up. No billing.
2. **Gemini: each user brings their own key.** The key is stored encrypted, LORE uses it for that user's requests, and that user's free daily allowance applies. There is no shared key.
3. **Sign-in:** Google, GitHub, and email magic links. No passwords are stored.
4. **Hosting: free or very cheap.** First choice is Oracle Cloud Always Free (an ARM machine with 4 cores and 24 GB). The fallback is Hetzner CAX11 (about €4–5 a month). Everything runs in Docker Compose, so the setup is portable.

## 1. Architecture

```
Caddy (HTTPS, lore.jrvn.site)
  ├─ web     FastAPI app + built UI      (stateless; reads the user from the session)
  └─ worker  processing jobs (ingest → extract → load → beats → codex), one per user at a time
Postgres     accounts, sessions, invites, keys (encrypted), chats, traces, usage, job queue
Ollama       embeddings (CPU), shared, internal network only
/data/users/<user_id>/   per-user library: lore.db (SQLite), kuzu/, lancedb/, inbox/, caches
```

## 2. Tenancy

- **One data directory per user**, at `/data/users/<user_id>/`. It holds that user's SQLite, Kuzu graph, LanceDB vectors, uploaded books and the caches (extractions, adjudication, beats, codex).
  - Kuzu allows only one writer, so isolation by directory suits it.
  - Deleting an account removes its directory.
  - Worlds keep working inside each library.
- **A single choke point.** Every request resolves the session to a user, and from that user to a `LoreConfig` whose `data_dir` is the user's directory. No code path takes a data path from the request. The worker does the same per job.
- **Concurrency.** A user's library has at most one writer, the worker or a move. Reads are read-only opens. The existing `book_jobs` claim logic keeps working per user.
- **Later, a shared public library.** Public-domain books could live in a read-only system tenant that every user can query beside their own library. This is out of scope for the beta.

## 3. Postgres (central, small)

| Table | Purpose |
|---|---|
| `users` | id, email, name, created_at, role (`user` or `admin`), status |
| `identities` | provider (google, github or email), provider_user_id, user_id |
| `sessions` | id (random), user_id, created_at, expires_at, user_agent. Delivered as an HTTP-only, Secure, SameSite=Lax cookie |
| `invites` | code, created_by, used_by, expires_at |
| `api_keys` | user_id, the Gemini key **encrypted** (AES-GCM, key held in a server secret), added_at, last_ok_at |
| `chats`, `messages` | server-side history that replaces browser localStorage; each message has a world and depth |
| `traces` | request_id, user_id, question, sub-questions, seeds, passages, every model prompt and reply, verdicts, answer, timings. This covers the release requirement "trace a user's report" |
| `usage` | user_id, day, model, request count. It feeds the per-user budget display |
| `jobs` | the queue the worker pulls from (user_id, book_id, kind, state). Per-book progress stays in the user's `book_jobs` |

Migrations use Alembic. Per-user SQLite migrations keep the existing in-app style.

## 4. Authentication

- **Providers:** OAuth 2 with Google and with GitHub, through a standard library (Authlib), plus email magic links. A magic link is a signed single-use token valid for 15 minutes, sent through Resend's free tier.
- **Invite gate.** Sign-in for a new identity requires a valid invite code while the beta runs. An admin page creates invites.
- **Sessions.** Server-side session rows, a rotating cookie, and CSRF protection on state-changing requests (a double-submit token).
- **Logout and revocation** delete the session row.

## 5. Each user's Gemini key

- **Onboarding.** After sign-in, a new user is asked for their Gemini API key, with a link to get one. The key is checked with one cheap request and then stored encrypted.
- **Use.** All model calls for that user, from the web and from the worker, build their Gemini client from that user's key. The answer reserve and per-model budgets are counted per user, in `usage`.
- **Visibility.** The key is never shown back in full; only its last 4 characters appear. It never appears in logs or traces.

## 6. Product changes for multi-user

- **History:** history and chats are stored server-side per user and work across devices. The quick switcher and drawer read from the API.
- **Adding books:** upload goes directly in the browser, which is the Phase 5b work. The CLI is admin-only on the server.
  - Upload limits: 50 MB, EPUB or PDF only, a check on the zip structure (zip-bomb and path-traversal guards), and a check on the MIME type.
- **Hidden internals:** `show_internals` is off for users, so no model names, prompts or stack traces appear in the UI. Errors map to friendly messages.
- **Rate limits:** a per-user request rate, uploads per day and concurrent asks.
- **MCP:** this is local-only for the beta. A per-user token can come later.

## 7. Hosting and operations

- **The machine:** Oracle Always Free on ARM (Ampere A1, 4 OCPU, 24 GB RAM) running Ubuntu with Docker and Docker Compose. Kuzu, LanceDB, Ollama and Postgres all have ARM builds.
- **DNS:** at Namecheap, an `A` record for `lore` pointing at the VM's IP. Caddy obtains and renews the Let's Encrypt certificate automatically.
- **Secrets:** an `.env` file on the server (session secret, encryption key, OAuth client secrets, Resend key). Never committed.
- **Deploys:** GitHub Actions builds images, then runs `docker compose pull && up -d` over SSH. Alternatively, a script deploys manually from the laptop.
- **Backups:**
  - a nightly `pg_dump` plus a tar of `/data/users`, uploaded to free object storage (Oracle's free tier includes 10 GB, or Cloudflare R2's free 10 GB), keeping 7 days;
  - a restore drill once before inviting anyone.
- **Monitoring:** a health endpoint, an uptime check (a free tier such as UptimeRobot), and per-request ids in logs that link to traces.
- **Fallback:** Hetzner CAX11 (2 cores, 4 GB), with the same Compose file. Ollama embeddings fit in 4 GB.

## 8. Privacy and legal (beta)

- Each user's books stay in their own directory, and nothing is shared between users.
- A plain privacy note: book passages and questions go to Google's Gemini API under the user's own key. Embeddings stay on the server.
- A terms line: users upload only books they own, for personal use.
- An account deletion action removes the data directory, the Postgres rows and the key.

## 8a. Homepage and waitlist (decided 2026-11-03)

A small front door, served by the same app. The app itself moves behind sign-in.
- **`/` (public):** a one-line pitch, a short looping clip or 2–3 screenshots of a real answer, and the differentiators:
  - every sentence checked against the book;
  - deep and analytical answers;
  - an explorable graph;
  - a private library;
  - spoiler-safe reading, marked "soon".

  It has a "How the beta works" section (invite-only, bring your own free Gemini key) and two buttons: **Sign in** and **Request an invite**. Screenshots use public-domain books only. It uses the logo and supports light and dark themes.
- **Waitlist:** the form takes an email, "what are you reading?" (optional) and a consent checkbox. Entries go into a new table `waitlist(id, email, reading, created_at, status, invited_at)`.
  - Spam protection: a rate limit per IP plus a honeypot field. No third-party captcha in the beta.
  - The admin page lists requests. **Approve** creates an invite and emails a sign-in link through Resend; **Decline** removes the request.
- **`/privacy` and `/terms`:** short, plain pages. Google's OAuth consent screen requires these links.

## 9. Order of work (each piece gets its own plan)

1. **Tenancy core:** per-user data directories, resolving a user to a config, and the worker as a separate process with a Postgres job queue.
2. **Auth:** OAuth, magic links, sessions, invites, CSRF, and an admin invite page.
3. **Per-user Gemini keys:** onboarding, encryption, per-user budgets.
4. **Server-side history and the trace store.**
5. **Browser upload** (Phase 5b), with limits and validation.
6. **Hardening:** hidden internals, rate limits, friendly errors.
7. **Homepage, waitlist and admin approval**, plus the privacy and terms pages.
8. **Deploy:** Docker images, Compose, Caddy, Oracle VM, DNS, backups and a restore drill, monitoring.
9. **Invite the first testers.**

## Open questions (decide during the plans)

- Whether to embed with Ollama on the server's CPU, which is free but slower to ingest, or with Gemini embeddings, which use the user's key but would require re-embedding existing libraries. **Proposed: keep Ollama.**
- Whether the shared public-domain library comes in the beta or later. **Proposed: later.**
