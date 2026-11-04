"""lore ui: the browser's API, a thin layer over lore.mcp.tools.

Every endpoint calls the same function the matching MCP tool calls, so
the UI, Claude Code and the evals see the same data and the same
structured errors (tool errors are HTTP 200 with an `error` body, as over
MCP; 4xx only for a malformed request to this API).
"""
from __future__ import annotations

import asyncio
import shlex
import shutil
import socket
import sqlite3
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import sessionmaker

from lore.auth.csrf import csrf_guard
from lore.auth.current import SessionRefresh
from lore.auth.routes import build_router as build_auth_router
from lore.central import queue
from lore.central.db import database_url, make_engine, make_session_factory
from lore.central.models import KIND_MOVE, KIND_PROCESS
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.ingest.registry import list_books
from lore.mcp import tools
from lore.orchestrator import jobs, runner
from lore.orchestrator.gemini_budget import GOOGLE_DAY_ZONE, day_report, today
from lore.orchestrator.jobs import JobRefused
from lore.orchestrator.move import prepare_move
from lore.orchestrator.pipeline import processing_budget_left
from lore.query import explore
from lore.tenancy import (
    CurrentUser, InvalidUserId, config_for, multi_user_enabled,
    require_session_user, resolve_user as default_resolver, validate_user_id,
)
from lore.web.ask import ask_events
from lore.worlds import (
    WorldError, books_in, create_world, delete_world, get_world, list_worlds,
    rename_world,
)
from lore.web.security import OriginGuard, allowed_hosts, allowed_origins

if TYPE_CHECKING:
    from lore.central.keys import KeyChecker, MasterKeys
    from lore.auth.mailer import Mailer
    from lore.auth.providers import Provider
    from lore.auth.settings import AuthSettings

STATIC = Path(__file__).parent / "static"
MCP_PORT = 8765
MAX_QUESTION_CHARS = 2000


class MoveBody(BaseModel):
    # An existing world's id or name; with `create`, the new world's name.
    world: int | str
    create: bool = False


class WorldBody(BaseModel):
    name: str


class AskBody(BaseModel):
    question: str
    # A world's id or name; none (or "all") searches every world.
    world: int | str | None = None
    # auto lets the decomposer decide; deep or focused overrides it.
    depth: Literal["auto", "deep", "focused"] = "auto"

    @field_validator("question")
    @classmethod
    def _sized(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > MAX_QUESTION_CHARS:
            raise ValueError(f"a question is 1-{MAX_QUESTION_CHARS} characters")
        return value

_NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>LORE</title>
<body style="font:15px system-ui;margin:3rem;max-width:40rem">
<h1>LORE's web app is not built yet</h1>
<p>The API is running. To build the interface once:</p>
<pre>cd web &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>then reload this page.</p></body>"""


def _lore_executable() -> str:
    beside = Path(sys.executable).with_name("lore")
    return str(beside) if beside.exists() else (shutil.which("lore") or "lore")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _quota(config: LoreConfig) -> dict:
    now = datetime.now(GOOGLE_DAY_ZONE)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
    if config.synthesis.provider != "gemini":
        return {"provider": "local", "models": [], "resets_at": midnight.isoformat()}
    try:
        conn = sqlite3.connect(f"file:{config.sqlite_path}?mode=ro", uri=True)
        try:
            models = day_report(conn, today())
        finally:
            conn.close()
    except sqlite3.Error:  # no ledger yet: nothing spent
        models = []
    return {"provider": "gemini", "models": models, "resets_at": midnight.isoformat()}


def _job_out(job: jobs.BookJob | None, now: datetime) -> dict | None:
    if job is None:
        return None
    ok, why = jobs.can_continue(job, now)
    return {
        "stage": job.stage, "state": job.state,
        "done": job.done_items, "total": job.total_items,
        "stopped_reason": job.stopped_reason,
        "resumable_at": job.resumable_at.isoformat() if job.resumable_at else None,
        "can_continue": ok, "continue_reason": why,
    }


def _gemini_use(conn: sqlite3.Connection, config: LoreConfig, now: datetime) -> dict:
    """Today's requests across the extraction cascade, as processing counts them."""
    from lore.inference.gemini import limits_for
    models = dict.fromkeys(config.extraction.gemini_models)
    limit = sum(limits_for(m).rpd for m in models)
    left = processing_budget_left(conn, config, today(now))
    return {"used_today": max(0, limit - left), "limit": limit,
            "answer_reserve": config.processing.answer_reserve}


def _api_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}},
                        status_code=status)


def create_app(config: LoreConfig, *, host: str = "127.0.0.1", port: int = 8766,
               dev: bool = False,
               stages_factory: Callable[[LoreConfig], list] | None = None,
               move_stages_factory: Callable[[LoreConfig, int], list] | None = None,
               clock: Callable[[], datetime] | None = None,
               multi_user: bool | None = None,
               resolve_user: Callable[[Request], CurrentUser] | None = None,
               central_sessions: sessionmaker | None = None,
               auth: "AuthSettings | None" = None,
               mailer: "Mailer | None" = None,
               providers: "dict[str, Provider] | None" = None,
               auth_clock: Callable[[], datetime] | None = None,
               master_keys: "MasterKeys | None" = None,
               key_checker: "KeyChecker | None" = None):
    """`stages_factory`, `move_stages_factory` and `clock` are test seams for
    book processing.

    `config` is the BASE config: its `data_dir` is the data root. Every route
    resolves the request's user and works on `config_for(config, user)`, the
    user's own library, opened per request (see lore.mcp.stores: handles are
    never cached, so nothing can leak from one user to the next). In local
    mode (the default) the one local user's library is `config.data_dir`
    itself, as before. `multi_user` (default: LORE_MULTI_USER) and
    `resolve_user` (default: lore.tenancy.resolve_user) are seams for tests.

    Multi mode also needs `auth` (default: read from the environment; the app
    refuses to start without the session and CSRF secrets), a `mailer`
    (default: Resend), the OAuth `providers` (default: Google and GitHub as
    configured) and `auth_clock`: see lore.auth. Every state-changing request
    must carry the per-session CSRF token (see `csrf_guard`).

    Each user brings their own Gemini key (release step 3, lore.web.keys):
    multi mode needs `master_keys` (default: LORE_KEY_ENCRYPTION_KEY; the app
    refuses to start without it) and checks a pasted key through
    `key_checker` (default: one request to Gemini). Every question runs with
    the asker's key bound, and asking or processing without one is
    `key_required`. Local mode keeps the server's key file, as before.

    Book processing: in local mode Continue and Move run in a thread of this
    process, one book at a time, as ever. In multi mode they ENQUEUE a job in
    the central database (`central_sessions`, default LORE_DATABASE_URL) for
    `lore worker` to run, and answer 202 at once.
    """
    multi = multi_user_enabled() if multi_user is None else multi_user
    auth_ctx = None
    if multi:
        from lore.auth.setup import build_context
        auth_ctx = build_context(
            config, auth=auth, mailer=mailer, providers=providers,
            central_sessions=central_sessions, clock=auth_clock)
        central_sessions = auth_ctx.sessions
        from lore.central.keys import KeyService, MasterKeys
        key_service = KeyService(
            central_sessions, master_keys or MasterKeys.from_env(),  # or no start
            key_checker, clock=auth_ctx.now)
    api = FastAPI(title="LORE", docs_url=None, redoc_url=None,
                  dependencies=[Depends(csrf_guard)] if multi else None)
    # `create_app(multi_user=True)` must mean sessions even when the env flag
    # is not set (the default resolver reads the env), so name the resolver.
    resolver = resolve_user or (require_session_user if multi else default_resolver)
    base = config

    def current_user(request: Request) -> CurrentUser:
        return resolver(request)

    def library_of(user: CurrentUser) -> LoreConfig:
        """The one place a user becomes a library: their own config."""
        if user.is_local:
            if multi:  # nobody may claim the single-user library in multi mode
                raise HTTPException(401, "invalid user")
            return base
        try:
            return config_for(base, validate_user_id(user.id))
        except InvalidUserId:
            raise HTTPException(401, "invalid user") from None

    def user_config(request: Request) -> LoreConfig:
        return library_of(current_user(request))

    sessions: list[sessionmaker | None] = [central_sessions]
    if auth_ctx is not None:
        api.state.auth = auth_ctx
        api.include_router(build_auth_router(auth_ctx))
        from lore.web.keys import build_key_router
        api.include_router(build_key_router(auth_ctx, key_service))

    def needs_key(user: CurrentUser) -> JSONResponse | None:
        """Multi mode: 409 key_required for a user with no Gemini key yet."""
        if not multi:
            return None
        from lore.web.keys import key_error
        missing = key_error(user.id, key_service)
        return None if missing is None else JSONResponse(missing, status_code=409)

    def enqueue(user: CurrentUser, book_id: str, kind: str,
                payload: dict | None = None):
        """Queue the job centrally: (job, created). created is False when the
        book already has a live job, which is returned instead."""
        if sessions[0] is None:  # built on first use: local mode never needs it
            sessions[0] = make_session_factory(make_engine(database_url(base)))
        with sessions[0]() as session:
            try:
                return queue.enqueue(session, user.id, book_id, kind, payload)
            except queue.UnknownUser:
                raise HTTPException(401, "unknown user") from None

    make_stages = stages_factory or runner.default_stages
    make_move_stages = move_stages_factory or runner.move_stages
    now_utc = clock or (lambda: datetime.now(timezone.utc))
    runner_slot = threading.Lock()  # held while a processing job runs

    def recover(cfg: LoreConfig) -> None:
        """A job whose runner died resumes from Continue, no reset wait."""
        try:
            conn = open_db(cfg.sqlite_path)
            try:
                jobs.recover_stale(conn, now_utc())
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    if not multi:  # multi mode recovers each user's library when it is read
        recover(base)

    @api.get("/api/books")
    def books(config: LoreConfig = Depends(user_config)):
        recover(config)
        now = now_utc()
        conn = open_db(config.sqlite_path)
        try:
            by_book = {j.book_id: j for j in jobs.list_jobs(conn)}
            rows = list_books(conn)
            gemini = _gemini_use(conn, config, now)
        finally:
            conn.close()
        return {"books": [
            {"id": b["id"], "title": b["title"], "author": b["author"],
             "status": b["status"], "world": b["world"], "series": b["series"],
             "series_index": b["series_index"],
             "job": _job_out(by_book.get(b["id"]), now)}
            for b in rows], "gemini": gemini}

    def _process(config: LoreConfig, book_id: str,
                 move_world: int | None = None) -> None:
        try:  # its own connection, in another thread
            runner.run_claimed(config, book_id, move_world, now_utc,
                               make_stages=make_stages,
                               make_move_stages=make_move_stages)
        finally:
            runner_slot.release()

    @api.post("/api/books/{book_id}/continue", status_code=202)
    def continue_book(book_id: str, config: LoreConfig = Depends(user_config),
                      user: CurrentUser = Depends(current_user)):
        if not multi and not runner_slot.acquire(blocking=False):
            return _api_error(409, "busy", "another book is being processed")
        handed_off = False
        try:
            conn = open_db(config.sqlite_path)
            try:
                if book_id not in {b["id"] for b in list_books(conn)}:
                    return _api_error(404, "not_found", f"no book with id {book_id!r}")
                job = jobs.get_job(conn, book_id)
                ok, why = jobs.can_continue(job, now_utc())
                if not ok:
                    return _api_error(409, "not_resumable", why)
                if multi:  # the worker picks the stage and any move up from the row
                    if (refused := needs_key(user)) is not None:
                        return refused
                    queued, _ = enqueue(user, book_id, KIND_PROCESS)
                    if queued.kind != KIND_PROCESS:
                        return _api_error(409, "busy",
                                          "a move is already queued for this book")
                    return {"started": True, "queued": True, "job": queued.id}
                # An unfinished move continues as a move: the ordinary stages
                # would skip `remove` and `reassign`.
                move_world = jobs.move_target(conn, book_id)
                if move_world is not None and get_world(conn, move_world) is None:
                    move_world = None  # the target was deleted: nothing to resume
                try:  # the 202 implies running: mark it before the thread starts
                    jobs.claim_job(conn, book_id, now=now_utc())
                except JobRefused as exc:
                    return _api_error(409, "not_resumable", str(exc))
            finally:
                conn.close()
            threading.Thread(target=_process, args=(config, book_id, move_world),
                             daemon=True).start()
            handed_off = True
        finally:
            if not handed_off and not multi:
                runner_slot.release()
        return {"started": True}

    @api.post("/api/books/{book_id}/move")
    def move_book(book_id: str, body: MoveBody,
                  config: LoreConfig = Depends(user_config),
                  user: CurrentUser = Depends(current_user)):
        if not multi and not runner_slot.acquire(blocking=False):
            return _api_error(409, "busy", "another book is being processed")
        handed_off = False
        created: int | None = None
        conn = open_db(config.sqlite_path)
        try:
            if book_id not in {b["id"] for b in list_books(conn)}:
                return _api_error(404, "not_found", f"no book with id {book_id!r}")
            if (refused := needs_key(user)) is not None:  # before a world is made
                return refused
            target = get_world(conn, body.world)
            if target is None:
                if not body.create:
                    return _api_error(400, "unknown_world",
                                      f"no world {body.world!r}")
                if not isinstance(body.world, str):
                    return _api_error(400, "unknown_world",
                                      f"no world with id {body.world}")
                try:
                    created = create_world(conn, body.world)
                except WorldError as exc:
                    return _api_error(400, "invalid_world", str(exc))
                target = get_world(conn, created)
            if multi:  # the worker claims the row and runs prepare_move itself
                queued, fresh = enqueue(user, book_id, KIND_MOVE,
                                        {"world": target.id})
                if not fresh and (queued.kind != KIND_MOVE or
                                  (queued.payload or {}).get("world") != target.id):
                    return _api_error(409, "busy",
                                      "another job is already queued for this book")
                created = None  # the world is in use now
                return JSONResponse(
                    {"started": True, "queued": True, "job": queued.id,
                     "world": {"id": target.id, "name": target.name}},
                    status_code=202)
            try:
                # Claims the row too (the 202 implies running) and saves the
                # target only once the claim has succeeded.
                if not prepare_move(conn, book_id, target.id, now=now_utc()):
                    return JSONResponse({"started": False})  # nothing to move
            except JobRefused as exc:
                return _api_error(409, "busy", str(exc))
            threading.Thread(target=_process, args=(config, book_id, target.id),
                             daemon=True).start()
            handed_off = True
            created = None  # the world is in use now
            return JSONResponse({"started": True,
                                 "world": {"id": target.id, "name": target.name}},
                                status_code=202)
        finally:
            if created is not None:  # refused: no stray empty world
                try:
                    delete_world(conn, created)
                except (WorldError, sqlite3.Error):
                    pass
            conn.close()
            if not handed_off and not multi:
                runner_slot.release()

    @api.get("/api/worlds")
    def worlds(config: LoreConfig = Depends(user_config)):
        conn = open_db(config.sqlite_path)
        try:
            rows = list_books(conn)
            listed = list_worlds(conn)
        finally:
            conn.close()
        return [
            {"id": w.id, "name": w.name,
             "books": [{"id": b["id"], "title": b["title"], "series": b["series"],
                        "series_index": b["series_index"]}
                       for b in rows if b["world_id"] == w.id]}
            for w in listed]

    def _world_out(w) -> dict:
        return {"id": w.id, "name": w.name}

    @api.post("/api/worlds", status_code=201)
    def create_a_world(body: WorldBody, config: LoreConfig = Depends(user_config)):
        conn = open_db(config.sqlite_path)
        try:
            if body.name.strip() and get_world(conn, body.name) is not None:
                return _api_error(409, "exists",
                                  f"a world named {body.name.strip()!r} already exists")
            try:
                new_id = create_world(conn, body.name)
            except WorldError as exc:
                return _api_error(400, "invalid_world", str(exc))
            return _world_out(get_world(conn, new_id))
        finally:
            conn.close()

    @api.patch("/api/worlds/{world_id}")
    def rename_a_world(world_id: int, body: WorldBody,
                       config: LoreConfig = Depends(user_config)):
        conn = open_db(config.sqlite_path)
        try:
            if get_world(conn, world_id) is None:
                return _api_error(404, "not_found", f"no world with id {world_id}")
            clash = get_world(conn, body.name) if body.name.strip() else None
            if clash is not None and clash.id != world_id:
                return _api_error(409, "exists",
                                  f"a world named {body.name.strip()!r} already exists")
            try:
                rename_world(conn, world_id, body.name)
            except WorldError as exc:
                return _api_error(400, "invalid_world", str(exc))
            return _world_out(get_world(conn, world_id))
        finally:
            conn.close()

    @api.delete("/api/worlds/{world_id}")
    def delete_a_world(world_id: int, config: LoreConfig = Depends(user_config)):
        conn = open_db(config.sqlite_path)
        try:
            if get_world(conn, world_id) is None:
                return _api_error(404, "not_found", f"no world with id {world_id}")
            if books_in(conn, world_id):
                return _api_error(409, "not_empty",
                                  "this world still has books; move them out first")
            try:
                delete_world(conn, world_id)
            except WorldError as exc:
                return _api_error(409, "not_empty", str(exc))
            return {"deleted": True}
        finally:
            conn.close()

    @api.get("/api/library")
    def library(world: str | None = None, config: LoreConfig = Depends(user_config)):
        return tools.library(config, world=world)

    @api.get("/api/entity")
    def entity(id: str | None = None, name: str | None = None,
               world: str | None = None, config: LoreConfig = Depends(user_config)):
        if id:
            return tools.entity_by_id(config, id, world=world)
        if name:
            return tools.entity(config, name, world=world)
        raise HTTPException(400, "give id or name")

    @api.get("/api/entities")
    def entities(ids: str = "", world: str | None = None,
                 config: LoreConfig = Depends(user_config)):
        return tools.entities_by_ids(config, [i for i in ids.split(",") if i],
                                     world=world)

    @api.get("/api/subgraph")
    def subgraph(id: str | None = None, name: str | None = None,
                 depth: int = Query(1, ge=1, le=3), world: str | None = None,
                 config: LoreConfig = Depends(user_config)):
        if id:
            return tools.subgraph_by_id(config, id, depth, world=world)
        if name:
            return tools.subgraph(config, name, depth, world=world)
        raise HTTPException(400, "give id or name")

    @api.get("/api/overview")
    def overview(limit: int = 40, world: str | None = None,
                 config: LoreConfig = Depends(user_config)):
        return tools.overview(config, limit, world=world)

    # The graph explorer's reads: stored data only, never a model call.
    @api.get("/api/entity/{entity_id}/profile")
    def entity_profile(entity_id: str, world: str | None = None,
                       config: LoreConfig = Depends(user_config)):
        return explore.profile(config, entity_id, world=world)

    @api.get("/api/entity/{entity_id}/timeline")
    def entity_timeline(entity_id: str, world: str | None = None,
                        config: LoreConfig = Depends(user_config)):
        return explore.timeline(config, entity_id, world=world)

    @api.get("/api/entity/{entity_id}/relations")
    def entity_relations(entity_id: str, world: str | None = None,
                         config: LoreConfig = Depends(user_config)):
        return explore.relations(config, entity_id, world=world)

    @api.get("/api/edge")
    def edge(a: str, b: str, world: str | None = None,
             config: LoreConfig = Depends(user_config)):
        return explore.edge(config, a, b, world=world)

    @api.get("/api/search")
    def search(q: str = "", world: str | None = None,
               limit: int = Query(explore.SEARCH_LIMIT_DEFAULT, ge=1),
               config: LoreConfig = Depends(user_config)):
        return explore.search(config, q, world=world, limit=limit)

    @api.get("/api/path")
    def path(a: str, b: str, world: str | None = None,
             max: int = Query(explore.PATH_MAX_HOPS, ge=1),
             config: LoreConfig = Depends(user_config)):
        return explore.path(config, a, b, world=world, max_hops=max)

    @api.get("/api/passage/{chunk_id}")
    def passage(chunk_id: str, world: str | None = None,
                config: LoreConfig = Depends(user_config)):
        return explore.passage(config, chunk_id, world=world)

    @api.get("/api/quota")
    def quota(config: LoreConfig = Depends(user_config)):
        return _quota(config)

    @api.get("/api/mcp")
    def mcp(config: LoreConfig = Depends(user_config)):
        return {
            "stdio_command": f"claude mcp add lore -- {shlex.quote(_lore_executable())} mcp",
            "http_command": f"lore mcp --http --port {MCP_PORT}",
            "http_url": f"http://127.0.0.1:{MCP_PORT}/mcp",
            "http_running": _port_open(MCP_PORT),
        }

    @api.get("/api/settings")
    def settings(config: LoreConfig = Depends(user_config)):
        return {"show_internals": config.web.show_internals}

    # One question at a time PER USER: one user's long answer must not make
    # another's `busy`. Locks only; no store handle is held across requests.
    locks: dict[str, asyncio.Lock] = {}

    @api.post("/api/ask")
    async def ask(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "send JSON")
        try:
            body = AskBody.model_validate(await request.json())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        user = current_user(request)
        cfg = library_of(user)
        lock = locks.setdefault(user.id, asyncio.Lock())
        search = tools.lore_search
        if multi:  # the asker's own Gemini key, bound for this question only
            from lore.web.keys import with_user_key
            search = with_user_key(user.id, key_service, search)
        return StreamingResponse(
            ask_events(cfg, body.question, lock, search=search,
                       world=body.world, depth=body.depth),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # Decided per request, not once here: building the frontend while the
    # server runs and reloading must serve it, as the not-built page says.
    @api.get("/", include_in_schema=False)
    def root():
        index = STATIC / "index.html"
        if multi:  # the public homepage (a placeholder until step 7)
            return _spa_page()
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(_NOT_BUILT)

    def _spa_page() -> HTMLResponse:
        """The web app's one HTML page, marked as multi-user so the script
        knows which page to draw for the address (see web/src/routing.ts)."""
        index = STATIC / "index.html"
        if not index.exists():
            return HTMLResponse(_NOT_BUILT)
        html = index.read_text(encoding="utf-8").replace(
            "</head>", '<meta name="lore-multi" content="1"></head>', 1)
        return HTMLResponse(html, headers={
            "Cache-Control": "no-store", "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin", "X-Content-Type-Options": "nosniff"})

    if multi:
        from lore.auth.current import session_user

        def _redirect(path: str) -> RedirectResponse:
            return RedirectResponse(path, status_code=303,
                                    headers={"Cache-Control": "no-store"})

        # Public pages: anyone. The page draws itself from the address.
        @api.get("/waitlist", include_in_schema=False)
        @api.get("/privacy", include_in_schema=False)
        @api.get("/terms", include_in_schema=False)
        def public_page():
            return _spa_page()

        @api.get("/signin", include_in_schema=False)
        def signin_page(request: Request):
            # Already signed in: straight to the app.
            return _redirect("/app") if session_user(request) else _spa_page()

        # The app and the admin page need a session; without one, sign in.
        # (The API behind them checks again: this is only the front door.)
        @api.get("/app", include_in_schema=False)
        @api.get("/admin", include_in_schema=False)
        def private_page(request: Request):
            return _spa_page() if session_user(request) else _redirect("/signin")

    api.mount("/", StaticFiles(directory=STATIC, html=True, check_dir=False),
              name="static")

    hosts, origins = allowed_hosts(host, port), allowed_origins(host, port, dev)
    inner = api
    if auth_ctx is not None:
        # The public address (LORE_BASE_URL) is the one more host and origin
        # this server answers; anything else is still refused.
        public = urlparse(auth_ctx.settings.origin)
        hosts, origins = hosts | {public.netloc}, origins | {auth_ctx.settings.origin}
        inner = SessionRefresh(api, auth_ctx.settings)
    return OriginGuard(inner, hosts, origins)
