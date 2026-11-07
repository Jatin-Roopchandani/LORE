"""lore ui: the browser's API, a thin layer over lore.mcp.tools.

Every endpoint calls the same function the matching MCP tool calls, so
the UI, Claude Code and the evals see the same data and the same
structured errors (tool errors are HTTP 200 with an `error` body, as over
MCP; 4xx only for a malformed request to this API).
"""
from __future__ import annotations

import asyncio
import json
import logging
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
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import sessionmaker

from lore.auth.csrf import csrf_guard
from lore.auth.current import SessionRefresh
from lore.auth.routes import build_router as build_auth_router
from lore.central import queue
from lore.central.db import database_url, make_engine, make_session_factory
from lore.central.db import upgrade as upgrade_central
from lore.central import history as history_store
from lore.central import traces as trace_store
from lore.central.history import ensure_user
from lore.central.models import KIND_MOVE, KIND_PROCESS
from lore import tracing
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.ingest.registry import book_rows, list_books, story_chapters
from lore.mcp import tools
from lore.orchestrator import jobs, runner
from lore.orchestrator.gemini_budget import GOOGLE_DAY_ZONE, day_report, today
from lore.orchestrator.jobs import JobRefused
from lore.orchestrator.move import prepare_move
from lore.orchestrator.pipeline import processing_budget_left
from lore.query import explore, visible
from lore.reading_position import ReadingPosition, get_position, set_position
from lore.tenancy import (
    LOCAL_USER_ID, CurrentUser, InvalidUserId, config_for, multi_user_enabled,
    require_session_user, resolve_user as default_resolver, validate_user_id,
)
from lore.web import friendly, public_pages
from lore.web.ask import ask_events
from lore.web.headers import SecurityHeaders
from lore.web.throttle import RateSettings, Throttle
from lore.web.limits import BodyLimit
from lore.web.upload import UploadDeps, UploadSettings, build_upload_router
from lore.web.request_id import RequestId
from lore.web.history import build_history_router, history_turns, record_answer
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


class PositionBody(BaseModel):
    enabled: bool
    book_id: str | None = None
    chapter: int = Field(0, ge=0)
    # Books of unknown order the reader has read (the position ignores any other).
    read_books: list[str] = []


def _invalid_position(body: PositionBody, rows: dict, chapters: dict) -> str | None:
    """Why `body` cannot be stored for a world whose books are `rows`, or None."""
    if body.book_id is not None and body.book_id not in rows:
        return f"no book {body.book_id!r} in this world"
    if body.enabled and body.book_id is None:
        return "choose the book you are reading"
    if body.book_id is not None and body.chapter > int(chapters.get(body.book_id) or 0):
        return f"{rows[body.book_id]['title']} has no chapter {body.chapter}"
    unknown = [b for b in body.read_books if b not in rows]
    return f"no book {unknown[0]!r} in this world" if unknown else None


def _bypass(spoilers: bool) -> dict:
    """`spoilers=True` only when asked: a call that takes none is unchanged."""
    return {"spoilers": True} if spoilers else {}


class AskBody(BaseModel):
    # Nothing else is accepted: above all no history, which only the server
    # loads, from the chat (`chat_id`) of the user asking.
    model_config = ConfigDict(extra="forbid")

    question: str
    # A world's id or name; none (or "all") searches every world.
    world: int | str | None = None
    # auto lets the decomposer decide; deep or focused overrides it.
    depth: Literal["auto", "deep", "focused"] = "auto"
    # The chat to append this question and its answer to, and whose last turns
    # make it a follow-up; none starts a new one. Not the user's: 404.
    chat_id: str | None = Field(None, max_length=64)

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


def mcp_setup(config: LoreConfig, exe: str, http_running: bool) -> dict:
    """The exact snippets that connect an MCP client to this library: the
    real `lore` executable, with the data directory (and lore.toml, when the
    server was started beside one) pinned by environment, because an MCP
    client starts `lore mcp` in a working directory of its own choosing."""
    env = {"LORE_DATA_DIR": str(Path(config.data_dir).resolve())}
    toml = Path("lore.toml").resolve()
    if toml.is_file():
        env["LORE_CONFIG"] = str(toml)
    desktop = {"mcpServers": {"lore": {"command": exe, "args": ["mcp"], "env": env}}}
    flags = " ".join(f"-e {shlex.quote(f'{k}={v}')}" for k, v in env.items())
    url = f"http://127.0.0.1:{MCP_PORT}/mcp"
    exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    return {
        "lore": exe,
        "env": env,
        "claude_desktop": json.dumps(desktop, indent=2),
        "claude_code": f"claude mcp add --scope user lore {flags} -- {shlex.quote(exe)} mcp",
        "http_command": f"{exports} {shlex.quote(exe)} mcp --http --port {MCP_PORT}",
        "http_url": url,
        "claude_code_http": f"claude mcp add --scope user --transport http lore {url}",
        "http_running": http_running,
        # The older field names, kept for any client of the first panel.
        "stdio_command": f"claude mcp add lore -- {shlex.quote(exe)} mcp",
    }


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


class _Releasing:
    """The answer stream, giving the question's in-flight slots back when it
    ends however it ends: finished, failed, the client gone, or never started
    (a response dropped before its first chunk is only garbage collected)."""

    def __init__(self, inner, grant) -> None:
        self._inner, self._grant = inner, grant

    def _done(self) -> None:
        grant, self._grant = self._grant, None
        if grant is not None:
            grant.release()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self._inner.__anext__()
        except BaseException:  # StopAsyncIteration included
            self._done()
            raise

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._done()

    def __del__(self) -> None:
        self._done()


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
               key_checker: "KeyChecker | None" = None,
               upload_settings: "UploadSettings | None" = None,
               rate_settings: "RateSettings | None" = None):
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

    Books are uploaded through POST /api/books/upload (lore.web.upload), with
    `upload_settings` (default: from the environment) for its quotas.

    Multi mode limits questions, entity intros, reports and parses per user
    and overall (lore.web.throttle), with `rate_settings` (default: from the
    environment).
    """
    multi = multi_user_enabled() if multi_user is None else multi_user
    auth_ctx = None
    if multi:  # no configured model name reaches a user (lore.web.friendly)
        friendly.known_models(friendly.config_models(config))
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
    central_lock = threading.Lock()
    local_ready: list[bool] = [False]

    def central() -> sessionmaker:
        """The central database's sessions. Multi mode: the migrated database
        the auth context opened. Local mode: built on first use, beside the
        data root unless LORE_DATABASE_URL says otherwise, migrated by the app
        itself and given the local user's row, so history and traces take the
        same code path with no sign-in."""
        if multi:
            return sessions[0]
        with central_lock:
            if not local_ready[0]:
                if sessions[0] is None:
                    url = database_url(base)
                    upgrade_central(url)
                    sessions[0] = make_session_factory(make_engine(url))
                with sessions[0]() as session:
                    ensure_user(session, LOCAL_USER_ID)
                    session.commit()
                purge_traces()
                local_ready[0] = True
        return sessions[0]

    def purge_traces() -> None:
        """Traces past their 30 days go: at start-up (multi mode) or when the
        local central database is first opened. Never fatal."""
        try:
            with sessions[0]() as session:
                trace_store.purge_expired(session, datetime.now(timezone.utc))
                session.commit()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("purging expired traces failed")

    if multi:
        purge_traces()
        trace_store.per_user_limit()  # a bad LORE_TRACES_PER_USER stops start-up
    throttle = (Throttle(lambda: sessions[0], rate_settings or RateSettings.from_env(),
                         auth_ctx.now) if multi else None)

    def central_user(request: Request) -> str:
        """The user's id in the central database (`local` in local mode)."""
        user = current_user(request)
        if user.is_local and not multi:
            return LOCAL_USER_ID
        try:
            return validate_user_id(user.id)
        except InvalidUserId:
            raise HTTPException(401, "invalid user") from None

    api.include_router(build_history_router(
        central, central_user, lambda: datetime.now(timezone.utc),
        report_limit=throttle.report if throttle is not None else None))
    from lore.web.export import build_export_router
    api.include_router(build_export_router(central, central_user, user_config))

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

    def _position_out(config: LoreConfig, world: str | None,
                      body: PositionBody | None = None):
        """The world's reading position (stored first when `body` is given),
        with the books to pick from in series order."""
        ref = (world or "").strip()
        if not ref or ref.casefold() == "all":
            return _api_error(400, "world_required",
                              "a reading position belongs to one world: choose a world")
        conn = open_db(config.sqlite_path)
        try:
            found = get_world(conn, int(ref)) if ref.isdigit() else None
            found = found or get_world(conn, ref)
            if found is None:
                return _api_error(404, "not_found", f"no world {ref!r}")
            rows = {b["id"]: b for b in list_books(conn) if b["world_id"] == found.id}
            # A stored `chapter` is a chapter_seq (the last raw section read);
            # the reader picks among story chapters only (`sections`), numbered
            # from 1 and named by their headings (lore/ingest/matter.py).
            chapters = dict(conn.execute(
                "SELECT book_id, MAX(chapter_seq) FROM chunks GROUP BY book_id").fetchall())
            story = {b: story_chapters(conn, b) for b in rows}
            if body is not None:
                bad = _invalid_position(body, rows, chapters)
                if bad:
                    return _api_error(400, "invalid_position", bad)
                set_position(conn, ReadingPosition(
                    found.id, body.book_id, body.chapter,
                    tuple(b for b in body.read_books if b != body.book_id), body.enabled))
            stored = get_position(conn, found.id)
            infos = book_rows(conn)
        finally:
            conn.close()
        current = stored.book_id if stored and stored.book_id in rows else None
        relation = visible.order_against(infos, rows, current) if current else {}
        return {
            "world": {"id": found.id, "name": found.name},
            "enabled": bool(stored and stored.enabled and current),
            "book_id": current,
            "chapter": stored.chapter if stored and current else 0,
            "read_books": [b for b in (stored.read_books if stored else ()) if b in rows],
            "books": [{"id": b, "title": rows[b]["title"], "series": rows[b]["series"],
                       "series_index": rows[b]["series_index"],
                       "chapters": len(story[b]),
                       "sections": story[b],
                       "order": "current" if b == current else relation.get(b, "unknown")}
                      for b in visible.series_order(infos, rows)],
        }

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

    def processing_choice(user: CurrentUser, cfg: LoreConfig) -> tuple[str, str | None]:
        """(provider, the model processing mostly uses) for this user."""
        from lore.inference.providers import roles
        if not multi:
            provider = cfg.llm.provider
            model = cfg.llm.fast_model or cfg.llm.model or None
        else:
            state = key_service.providers(user.id)
            provider = state["active"]
            row = next(p for p in state["providers"] if p["provider"] == provider)
            model = row["fast_model"] or row["model"]
        if provider == "anthropic" and model not in roles.ANTHROPIC_MODELS:
            model = roles.ANTHROPIC_DEFAULT
        return provider, model

    def gemini_paid_for(user: CurrentUser, cfg: LoreConfig) -> bool:
        """The user's "I'm on a paid Gemini plan" (multi), or [lore.llm] gemini_paid."""
        if not multi:
            return cfg.llm.provider == "gemini" and cfg.llm.gemini_paid
        return key_service.gemini_paid(user.id)

    def book_estimate(user: CurrentUser, cfg: LoreConfig, book_id: str) -> dict:
        from lore.inference.providers import estimate as estimates
        provider, model = processing_choice(user, cfg)
        conn = open_db(cfg.sqlite_path)
        try:
            chunks = estimates.chunk_count(conn, book_id)
        finally:
            conn.close()
        if provider == "gemini" and gemini_paid_for(user, cfg):
            # Priced on the extraction cascade's first model: what processing
            # mostly uses. The free tier's estimate is unchanged.
            first = (cfg.extraction.gemini_models or [None])[0]
            return estimates.estimate(chunks, provider, first, gemini_paid=True)
        return estimates.estimate(chunks, provider, model)

    @api.get("/api/books/{book_id}/estimate")
    def estimate_book(book_id: str, config: LoreConfig = Depends(user_config),
                      user: CurrentUser = Depends(current_user)):
        """What processing this book should cost on the user's provider (an
        estimate; see lore.inference.providers.estimate)."""
        conn = open_db(config.sqlite_path)
        try:
            if book_id not in {b["id"] for b in list_books(conn)}:
                return _api_error(404, "not_found", f"no book with id {book_id!r}")
        finally:
            conn.close()
        return book_estimate(user, config, book_id)

    def start_processing(user: CurrentUser, cfg: LoreConfig, book_id: str) -> dict:
        """A freshly uploaded book's processing: what Continue would do, but
        never past the user's Gemini budget. Spent: the book waits for the
        reset (Continue then), as a run stopped by the allowance would.

        On Claude, OpenAI or a paid Gemini plan (billed to the user) there is
        no free-tier ledger:
        the book waits when the user's own daily token cap is reached, and
        when the estimate is above the confirmation threshold it does not
        start at all: the reply carries the estimate, and Continue (the
        user's confirmation) starts it."""
        now = now_utc()
        provider, _model = processing_choice(user, cfg)
        if provider != "gemini" or gemini_paid_for(user, cfg):
            if multi and key_service.over_cap(user.id):
                conn = open_db(cfg.sqlite_path)
                try:
                    jobs.claim_job(conn, book_id, now=now)
                    jobs.stop_quota(conn, book_id, "your daily token cap is reached;"
                                    " processing can start after midnight Pacific",
                                    now=now)
                    job = jobs.get_job(conn, book_id)
                    return {"state": "waiting",
                            "resumable_at": job.resumable_at.isoformat()}
                finally:
                    conn.close()
            estimate = book_estimate(user, cfg, book_id)
            if estimate["needs_confirm"]:
                return {"state": "needs_confirm", "estimate": estimate}
            return begin_processing(user, cfg, book_id)
        conn = open_db(cfg.sqlite_path)
        try:
            if processing_budget_left(conn, cfg, today(now)) <= cfg.processing.answer_reserve:
                jobs.claim_job(conn, book_id, now=now)
                jobs.stop_quota(conn, book_id, "today's Gemini allowance is used up;"
                                " processing can start after the reset", now=now)
                job = jobs.get_job(conn, book_id)
                return {"state": "waiting",
                        "resumable_at": job.resumable_at.isoformat()}
        finally:
            conn.close()
        return begin_processing(user, cfg, book_id)

    def begin_processing(user: CurrentUser, cfg: LoreConfig, book_id: str) -> dict:
        if multi:
            queued, _ = enqueue(user, book_id, KIND_PROCESS)
            return {"state": "queued", "job": queued.id}
        if not runner_slot.acquire(blocking=False):
            return {"state": "busy"}  # added; Continue once the other book is done
        handed_off = False
        try:
            conn = open_db(cfg.sqlite_path)
            try:
                jobs.claim_job(conn, book_id, now=now_utc())
            except JobRefused:
                return {"state": "busy"}
            finally:
                conn.close()
            threading.Thread(target=_process, args=(cfg, book_id, None),
                             daemon=True).start()
            handed_off = True
        finally:
            if not handed_off:
                runner_slot.release()
        return {"state": "processing"}

    api.include_router(build_upload_router(UploadDeps(
        multi=multi, current_user=current_user, library_of=library_of,
        needs_key=needs_key, central=lambda: sessions[0], start=start_processing,
        now=auth_ctx.now if auth_ctx is not None else (lambda: datetime.now(timezone.utc)),
        settings=upload_settings or UploadSettings.from_env(), throttle=throttle)))

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

    # Every read below applies the world's stored reading position (what the
    # reader has read so far). `?spoilers=1` bypasses it for that one request;
    # the UI sends it only when the reader explicitly asks to reveal.
    @api.get("/api/library")
    def library(world: str | None = None, spoilers: bool = False,
                config: LoreConfig = Depends(user_config)):
        return tools.library(config, world=world, **_bypass(spoilers))

    @api.get("/api/entity")
    def entity(id: str | None = None, name: str | None = None,
               world: str | None = None, spoilers: bool = False,
               config: LoreConfig = Depends(user_config)):
        if id:
            return tools.entity_by_id(config, id, world=world, **_bypass(spoilers))
        if name:
            return tools.entity(config, name, world=world, **_bypass(spoilers))
        raise HTTPException(400, "give id or name")

    @api.get("/api/entities")
    def entities(ids: str = "", world: str | None = None, spoilers: bool = False,
                 config: LoreConfig = Depends(user_config)):
        return tools.entities_by_ids(config, [i for i in ids.split(",") if i],
                                     world=world, **_bypass(spoilers))

    @api.get("/api/subgraph")
    def subgraph(id: str | None = None, name: str | None = None,
                 depth: int = Query(1, ge=1, le=3), world: str | None = None,
                 spoilers: bool = False, config: LoreConfig = Depends(user_config)):
        if id:
            return tools.subgraph_by_id(config, id, depth, world=world,
                                        **_bypass(spoilers))
        if name:
            return tools.subgraph(config, name, depth, world=world, **_bypass(spoilers))
        raise HTTPException(400, "give id or name")

    @api.get("/api/overview")
    def overview(limit: int = 40, world: str | None = None, spoilers: bool = False,
                 config: LoreConfig = Depends(user_config)):
        return tools.overview(config, limit, world=world, **_bypass(spoilers))

    # The graph explorer's reads: stored data only, never a model call.
    @api.get("/api/entity/{entity_id}/profile")
    def entity_profile(entity_id: str, world: str | None = None, spoilers: bool = False,
                       config: LoreConfig = Depends(user_config)):
        return explore.profile(config, entity_id, world=world, **_bypass(spoilers))

    # The one explorer read that may call a model: under a reading position, a
    # short intro written only from what has been read (one cheap request,
    # then cached; the deterministic intro whenever no request can be made).
    # In multi mode the request runs on the reader's own Gemini key.
    @api.get("/api/entity/{entity_id}/intro")
    def entity_intro(request: Request, entity_id: str, world: str | None = None,
                     spoilers: bool = False, config: LoreConfig = Depends(user_config)):
        if not multi:
            return explore.intro(config, entity_id, world=world, **_bypass(spoilers))
        from lore.web.keys import user_key_bound
        uid = current_user(request).id
        if (refused := throttle.intro_wait(uid)) is not None:
            return refused
        spent: list[str] = []
        try:
            with user_key_bound(uid, key_service, spent):
                return explore.intro(config, entity_id, world=world, **_bypass(spoilers))
        finally:  # the model calls it made count against the hourly limit
            throttle.intro_spent(uid, len(spent))

    @api.get("/api/entity/{entity_id}/timeline")
    def entity_timeline(entity_id: str, world: str | None = None, spoilers: bool = False,
                        config: LoreConfig = Depends(user_config)):
        return explore.timeline(config, entity_id, world=world, **_bypass(spoilers))

    @api.get("/api/entity/{entity_id}/relations")
    def entity_relations(entity_id: str, world: str | None = None, spoilers: bool = False,
                         config: LoreConfig = Depends(user_config)):
        return explore.relations(config, entity_id, world=world, **_bypass(spoilers))

    @api.get("/api/edge")
    def edge(a: str, b: str, world: str | None = None, spoilers: bool = False,
             config: LoreConfig = Depends(user_config)):
        return explore.edge(config, a, b, world=world, **_bypass(spoilers))

    @api.get("/api/search")
    def search(q: str = "", world: str | None = None,
               limit: int = Query(explore.SEARCH_LIMIT_DEFAULT, ge=1),
               spoilers: bool = False, config: LoreConfig = Depends(user_config)):
        return explore.search(config, q, world=world, limit=limit, **_bypass(spoilers))

    @api.get("/api/path")
    def path(a: str, b: str, world: str | None = None,
             max: int = Query(explore.PATH_MAX_HOPS, ge=1), spoilers: bool = False,
             config: LoreConfig = Depends(user_config)):
        return explore.path(config, a, b, world=world, max_hops=max, **_bypass(spoilers))

    @api.get("/api/passage/{chunk_id}")
    def passage(chunk_id: str, world: str | None = None, spoilers: bool = False,
                config: LoreConfig = Depends(user_config)):
        return explore.passage(config, chunk_id, world=world, **_bypass(spoilers))

    # The reading position lives in each library's own lore.db, so it is per user.
    @api.get("/api/reading-position")
    def get_reading_position(world: str | None = None,
                             config: LoreConfig = Depends(user_config)):
        return _position_out(config, world)

    @api.put("/api/reading-position")
    def put_reading_position(body: PositionBody, world: str | None = None,
                             config: LoreConfig = Depends(user_config)):
        return _position_out(config, world, body)

    @api.get("/api/quota")
    def quota(config: LoreConfig = Depends(user_config)):
        out = _quota(config)
        if multi:  # per-model counts name the models: internals
            out["models"] = []
        return out

    @api.get("/api/mcp")
    def mcp(config: LoreConfig = Depends(user_config)):
        if multi:  # MCP is local-only (lore.mcp.server refuses multi mode)
            return _api_error(404, "not_found", "MCP is not available here")
        out = mcp_setup(config, _lore_executable(), _port_open(MCP_PORT))
        # Where the library lives, for the Your data tab's backup advice: the
        # data directory and, when it is a file, the central database (chats).
        central_url = database_url(base)
        out["library"] = {"data_dir": out["env"]["LORE_DATA_DIR"],
                          "central_db": central_url.removeprefix("sqlite:///")
                          if central_url.startswith("sqlite:///") else None}
        return out

    @api.get("/api/mcp/check")
    async def mcp_check(config: LoreConfig = Depends(user_config)):
        """Can the MCP server start here? Builds it as `lore mcp` would (no
        transport, nothing served) and lists the tools it exposes."""
        if multi:
            return _api_error(404, "not_found", "MCP is not available here")
        exe = _lore_executable()
        found = Path(exe).is_file() or shutil.which(exe) is not None
        try:
            from lore.mcp.server import build_server
            listed = await build_server(config).list_tools()
        except Exception as exc:  # noqa: BLE001 -- the check reports, never raises
            return {"ok": False, "lore_found": found, "tools": [],
                    "problem": f"the MCP server could not start: {type(exc).__name__}"}
        out = [{"name": t.name,
                "summary": " ".join((t.description or "").strip().split("\n\n")[0].split())}
               for t in listed]
        return {"ok": found, "lore_found": found, "tools": out,
                "problem": None if found else f"the lore command was not found at {exe}",
                "library_found": config.sqlite_path.exists()}

    @api.get("/api/settings")
    def settings(config: LoreConfig = Depends(user_config)):
        # Multi mode: never. No model names, prompts or provider errors.
        return {"show_internals": False if multi else config.web.show_internals}

    # One question at a time PER USER: one user's long answer must not make
    # another's `busy`. Locks only; no store handle is held across requests.
    locks: dict[str, asyncio.Lock] = {}

    def _position_note_now(cfg, world, spoilers: bool) -> str | None:
        """The note a question asked now would carry: None with spoilers shown or
        no reading position on (lore.query.visible.position_note)."""
        if spoilers:
            return None
        from lore.query import visible as visible_module
        from lore.query.scope import resolve_scopes
        conn = open_db(cfg.sqlite_path)
        try:
            scopes = resolve_scopes(conn, world)
        except Exception:  # noqa: BLE001 -- an unknown world fails later, as it always has
            return None
        finally:
            conn.close()
        return visible_module.position_note([s.position for s in scopes if s.position])

    @api.post("/api/ask")
    async def ask(request: Request, spoilers: bool = False):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "send JSON")
        try:
            body = AskBody.model_validate(await request.json())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        user = current_user(request)
        cfg = library_of(user)
        # Multi mode: the central in-flight slots decide (lore.web.throttle),
        # across every web process; local mode: one question at a time.
        lock = asyncio.Lock() if multi else locks.setdefault(user.id, asyncio.Lock())
        owner = central_user(request)
        # A follow-up: the chat's last turns, loaded here from the asker's own
        # chat, never taken from the request. Someone else's chat, or none, is
        # 404 before anything is searched.
        history: list[dict] = []
        if body.chat_id is not None:
            with central()() as session:
                try:
                    _, earlier = history_store.get_chat(session, owner, body.chat_id)
                except history_store.NotFound:
                    return JSONResponse({"error": {"code": "not_found",
                                                   "message": "no such chat"}}, status_code=404)
                history = history_turns(earlier, world=body.world,
                                        position_note=_position_note_now(cfg, body.world, spoilers))
        # The question's trace, keyed by this request's id (lore.web.request_id):
        # every model call it makes, captured inside the user's key scope so
        # the trace is redacted against their key.
        trace = tracing.Trace(request_id=tracing.current_request_id()
                              or tracing.new_request_id())
        search = tracing.traced_search(tools.lore_search, trace)
        if multi:  # the asker's own Gemini key, bound for this question only
            from lore.web.keys import with_user_key
            search = with_user_key(user.id, key_service, search)

        def save(result: dict | None, error: dict | None, events: list[dict]) -> dict:
            """The finished question: appended to the user's chat when it was
            answered, and its trace stored either way. Returns the chat it
            went to, for the answer event."""
            if trace.finished_s is None or (error and not trace.error):
                trace.finish(answer=result if trace.answer is None else trace.answer,
                             error=f"{error.get('code')}: {error.get('message')}"
                             if error else None)
            now = datetime.now(timezone.utc)
            out: dict = {}
            with central()() as session:
                chat_id = message_id = None
                if result is not None:
                    try:
                        chat_id, message_id = record_answer(
                            session, owner, chat_id=body.chat_id, question=body.question,
                            world=body.world, depth=body.depth,
                            result=friendly.public_result(result) if multi else result,
                            events=events, request_id=trace.request_id, now=now)
                        out = {"chat_id": chat_id, "message_id": message_id}
                    except history_store.HistoryFull:
                        session.rollback()  # shown, not saved; the trace still is
                        out = {"history_full": True}
                elif body.chat_id is not None:
                    try:  # a failed answer still links to the chat it was asked in
                        chat_id = history_store.get_chat(session, owner, body.chat_id)[0].id
                    except history_store.NotFound:
                        chat_id = None
                trace_store.save(session, trace, user_id=owner, question=body.question,
                                 world=body.world, depth=body.depth, chat_id=chat_id,
                                 message_id=message_id, now=now)
                session.commit()
            return out

        grant = None
        if throttle is not None:  # per user and overall; 429 past a limit
            grant = throttle.start_question(user.id)
            if isinstance(grant, JSONResponse):
                return grant
        return StreamingResponse(
            _Releasing(ask_events(cfg, body.question, lock, search=search,
                       world=body.world, depth=body.depth, on_finish=save, history=history,
                       request_id=trace.request_id,
                       public=friendly.public_event if multi else None,
                       **_bypass(spoilers)), grant),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # Decided per request, not once here: building the frontend while the
    # server runs and reloading must serve it, as the not-built page says.
    @api.get("/", include_in_schema=False)
    def root(request: Request):
        index = STATIC / "index.html"
        if multi:  # the public homepage
            return _spa_page(public=request)
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(_NOT_BUILT)

    def _spa_page(public: Request | None = None) -> HTMLResponse:
        """The web app's one HTML page, marked as multi-user so the script
        knows which page to draw for the address (see web/src/routing.ts).
        `public` (the request for /, /privacy or /terms) gives the page its
        own title and link-preview tags, and marks a signed-in visitor."""
        index = STATIC / "index.html"
        if not index.exists():
            return HTMLResponse(_NOT_BUILT)
        html = index.read_text(encoding="utf-8").replace(
            "</head>", '<meta name="lore-multi" content="1"></head>', 1)
        if public is not None and auth_ctx is not None:
            from lore.auth.current import session_user
            html = public_pages.with_head(
                html, public.url.path.rstrip("/") or "/", auth_ctx.settings.origin,
                signed_in=session_user(public) is not None)
        # The security headers (CSP and the rest) are added to every
        # response by lore.web.headers.SecurityHeaders.
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    if multi:
        from lore.auth.current import session_user

        def _redirect(path: str) -> RedirectResponse:
            return RedirectResponse(path, status_code=303,
                                    headers={"Cache-Control": "no-store"})

        # Public pages: anyone. The page draws itself from the address.
        @api.get("/waitlist", include_in_schema=False)
        def waitlist_page():
            return _spa_page()

        @api.get("/privacy", include_in_schema=False)
        @api.get("/terms", include_in_schema=False)
        def legal_page(request: Request):
            return _spa_page(public=request)

        # The mailed sign-in link: a page with a "Continue signing in" button
        # that POSTs the token (lore.auth.signin). Opening the link spends
        # nothing, so a mail scanner that prefetches it cannot sign anyone
        # in or burn the link. The URL holds the token, so no cache, and the
        # Referer never leaves this site (`same-origin`). Not `no-referrer`:
        # with it, browsers send `Origin: null` on the page's own form POST,
        # and the origin guard rightly refuses that.
        @api.get("/auth/email/verify", include_in_schema=False)
        def continue_sign_in():
            page = _spa_page()
            page.headers["Referrer-Policy"] = "same-origin"
            page.headers["Cache-Control"] = "no-store"
            return page

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
    # Every request gets an id, on its log lines and in X-Request-Id; /api/ask
    # keys its trace by it. Request bodies are capped (lore.web.limits).
    api.add_middleware(BodyLimit)
    if multi:  # friendly errors with the request id, never a traceback
        api.add_middleware(friendly.PublicErrors)
    api.add_middleware(RequestId)

    hosts, origins = allowed_hosts(host, port), allowed_origins(host, port, dev)
    inner = api
    if auth_ctx is not None:
        # The public address (LORE_BASE_URL) is the one more host and origin
        # this server answers; anything else is still refused.
        public = urlparse(auth_ctx.settings.origin)
        hosts, origins = hosts | {public.netloc}, origins | {auth_ctx.settings.origin}
        inner = SessionRefresh(api, auth_ctx.settings)
        # CSP, Referrer-Policy, nosniff, Permissions-Policy, frame refusal
        # and HSTS behind https, on every response (lore.web.headers).
        return SecurityHeaders(OriginGuard(inner, hosts, origins),
                               hsts=auth_ctx.settings.origin.startswith("https://"))
    return OriginGuard(inner, hosts, origins)
