# Running LORE for many users

LORE has two modes. **Local mode** (the default) is one user and one library, as ever: `lore ui` and `lore process`. **Multi mode** serves many users from one deployment: each user gets an isolated library, a central database holds the users and the job queue, and book processing runs in a separate worker process.

**The production deploy** (Docker Compose on one VM behind a Cloudflare Tunnel: image, updates, backups) is in [server.md](server.md), with the files in `deploy/`. In multi mode `lore ui` may bind a non-loopback address (`--host 0.0.0.0` in the container), and `GET /healthz` answers `{"ok": true}` while the central database does.

This page covers step 1 of the release work (the tenancy core). Sign-in is step 2, so a multi-mode web process refuses every request with 401 until auth is in place; the tests exercise it through an injected resolver.

## The pieces

| Piece | Command | Needs |
|---|---|---|
| Central database | `lore migrate` | `LORE_DATABASE_URL` |
| Web app | `lore ui` | `LORE_MULTI_USER=1`, `LORE_DATABASE_URL`, `LORE_DATA_DIR` |
| Worker | `lore worker` | the same three |

- `LORE_DATA_DIR` is the data **root**. Each user's library is `<root>/users/<user_id>/` (SQLite, Kuzu, LanceDB, inbox, caches). The directory is created on first use. User ids are UUIDs or safe slugs only.
- `LORE_DATABASE_URL` is a SQLAlchemy URL, for example `postgresql+psycopg://lore:secret@db:5432/lore`. If it is unset, a SQLite file `<data root>/central.db` is used, which is fine for one machine and for trying things out; use Postgres in production.
- `LORE_MULTI_USER=1` switches the web app to multi mode. Leave it unset for local mode.

## Start-up order

```
export LORE_DATA_DIR=/data
export LORE_DATABASE_URL=postgresql+psycopg://lore:secret@db:5432/lore
export LORE_MULTI_USER=1

lore migrate          # create or upgrade the central tables; safe to repeat
lore ui --port 8766   # the web app (stateless; run behind a reverse proxy)
lore worker           # run one or more of these; each claims jobs
```

Run as many workers as you like. A user's jobs never run at the same time (the database enforces it), and two workers never take the same job. A worker that dies has its job requeued after three minutes without a heartbeat; after three failed attempts the job is marked failed.

## What changes for the web API in multi mode

- `POST /api/books/{id}/continue` and `POST /api/books/{id}/move` queue a job and answer `202` with `{"started": true, "queued": true, "job": <id>}`. The worker reports progress on the user's own job row, which `GET /api/books` already shows. In local mode they still run in a thread of the web process.
- One question at a time applies per user, not for the whole server.
- MCP (`lore mcp`) is local-mode only. Do not expose it from a multi-user deployment.

## Tests

The central-database tests run on SQLite. One more test applies the migrations to a throwaway Postgres container; it is skipped unless docker is available and `LORE_TEST_POSTGRES=1`:

```
LORE_TEST_POSTGRES=1 pytest -m postgres tests/test_central_db.py
```
