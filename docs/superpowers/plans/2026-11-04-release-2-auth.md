# Release step 2: sign-in, sessions, invites and waitlist approval

Read `docs/superpowers/specs/2026-11-03-release-design.md` §4 and §8a, and the step 1 report at `.superpowers/sdd/release-1-report.md`. Step 1 left `lore.tenancy._user_from_session(request)` returning None, so multi mode fails closed with a 401. This step fills that hook in.

**Constraints**
- Work in `/home/jatin/LORE-release` on branch `release`, using its own `.venv`. Add dependencies with `uv add`, for example `authlib` and `itsdangerous`. Pick well-maintained ones.
- Use TDD. Make no network calls: mock the OAuth providers and Resend, and keep `tests/conftest.py` blocking the network.
- **Do not run the full suite.** An eval is running on this machine. Run only the targeted files.
- Commits: git email jatinroopchandani3007@gmail.com, NO trailers, stage files by path, do not push.
- Never use `pkill -f` or `pgrep -f`. Don't touch `~/LORE-r7`, `~/LORE-prose` or `/home/jatin/LORE/data`.
- Local mode (the default) must stay exactly as today: no sign-in, a single user.

## Task 1: sessions and the user hook

- Add `sessions` and `identities` to the central DB (an Alembic migration), following spec §3.
- A session id is a random 256-bit token. Only its SHA-256 is stored. It is sent as a cookie `lore_session`: HttpOnly, Secure (configurable off for local http in dev), SameSite=Lax, Path=/, with a 30-day expiry. The expiry slides on use, and is rewritten at most once a day.
- `_user_from_session(request)` resolves the cookie to its session, then its user, and checks the user's `status` is active. Anything invalid or expired returns None.
- `POST /auth/logout` deletes the session row and clears the cookie.
- **CSRF:** every state-changing request (POST, PUT, PATCH, DELETE) under `/api` must carry a header `X-CSRF-Token` equal to a per-session token. The token is set as a readable, non-HttpOnly cookie `lore_csrf`. A missing or mismatched token returns 403. The web client sends it on every mutating request; update `web/src/api.ts`.

## Task 2: OAuth with Google and GitHub, and magic links

- `GET /auth/{google|github}/start` redirects to the provider with `state` and PKCE. `GET /auth/{provider}/callback` exchanges the code, then finds or creates the identity, then the user, then a session, and redirects to `/app`. Use Authlib's Starlette or FastAPI integration.
  - Google needs `openid email profile`, and only a verified email.
  - GitHub needs `read:user user:email`; fetch the primary verified email.
- `POST /auth/email` takes `{email}` and sends a magic link through Resend. In tests, use an injectable mailer, for example a `Mailer` protocol with a fake.
  - The link token is signed and single-use, valid for 15 minutes, and stored hashed in a new `login_tokens` table.
  - `GET /auth/email/verify?token=` consumes the token, then finds or creates the user, then creates a session.
  - The response is the same whether or not the email exists, so it can't be used to discover addresses.
  - Rate limit: 5 requests per email per hour and 20 per IP per hour, using a simple central-DB counter.
- **Invite gate.** While the beta is invite-only (a `LORE_INVITE_ONLY` setting, default on in multi mode), a NEW user can only be created with a valid unused invite. The invite code travels through the login flow: `/auth/...?invite=CODE`, carried in OAuth `state` or in the magic-link token. Without one, sign-in for an unknown identity lands on a "You're on the waitlist / request an invite" page. Existing users sign in normally.
- **Account linking.** The same verified email across providers links to one user.

## Task 3: the waitlist and admin approval

- `POST /api/waitlist` (public) takes `{email, reading?, consent}`. It has a honeypot field and a per-IP rate limit, and stores into `waitlist`. It always returns 202.
- **Admin role.** `users.role = 'admin'`, bootstrapped by a CLI command, `lore admin grant <email>`.
- **Admin API** (admin-only, CSRF-protected):
  - `GET /api/admin/waitlist` lists entries.
  - `POST /api/admin/waitlist/{id}/approve` creates an invite and emails the person a sign-in link that includes the invite.
  - `POST /api/admin/waitlist/{id}/decline`.
  - `POST /api/admin/invites` creates an invite code to hand out directly.
  - `GET /api/admin/users`.
- **Admin page.** A minimal UI at `/admin`, using the existing React app with a route. It has a waitlist table with Approve and Decline, an invite creator and a user list. All strings go in messages.ts.

## Task 4: routes and pages for multi mode

- In multi mode, `/` is the public homepage. Step 7 builds the real one, so serve a placeholder for now. `/app` is the app and requires a session; without one it redirects to `/signin`. `/signin` offers Google, GitHub and email, plus a "Request an invite" link. `/privacy` and `/terms` are placeholders for now.
- Local mode keeps `/` as the app, exactly as today.
- Ensure a `users` row exists for every id the hook returns. Step 1's enqueue returns 401 for an unknown user, so creation must happen at sign-in.

## Tests (all mocked)

- **Sessions:** create, resolve, expire, logout. A bad or old cookie returns 401. The cookie flags are correct.
- **CSRF:** a missing or mismatched token returns 403 on mutations; GETs are unaffected.
- **OAuth:** mocked token and userinfo for both providers. Cover new user with an invite, new user without an invite (sent to the waitlist page), an existing user, the unverified-email rejection, and linking by email.
- **Magic links:** a token is single-use, expires, and sends the same response for known and unknown addresses. Rate limits apply.
- **Waitlist and admin:** a non-admin gets 403. Approving creates an invite and sends mail through the fake mailer. The honeypot works.
- **Isolation still holds:** a signed-in user A cannot read user B's library. Rerun step 1's isolation tests with real sessions in place of the test header.
- **Postgres:** add the job-claim race test, marked `postgres`, opt-in, to cover step 1's SKIP LOCKED path.
- **Local mode** behaves identically: no auth needed, and existing tests pass.
