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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.ingest.registry import list_books
from lore.mcp import tools
from lore.orchestrator import jobs
from lore.orchestrator.gemini_budget import GOOGLE_DAY_ZONE, day_report, today
from lore.orchestrator.jobs import JobRefused
from lore.orchestrator.pipeline import processing_budget_left, run_book
from lore.web.ask import ask_events
from lore.web.security import OriginGuard, allowed_hosts, allowed_origins

STATIC = Path(__file__).parent / "static"
MCP_PORT = 8765
MAX_QUESTION_CHARS = 2000


class AskBody(BaseModel):
    question: str

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


def _default_stages(config: LoreConfig) -> list:
    # Imported here: lore.cli imports this module's create_app.
    from lore.cli import default_stages
    return default_stages(config, resume=True)


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
               clock: Callable[[], datetime] | None = None):
    """`stages_factory` and `clock` are test seams for book processing."""
    api = FastAPI(title="LORE", docs_url=None, redoc_url=None)
    make_stages = stages_factory or _default_stages
    now_utc = clock or (lambda: datetime.now(timezone.utc))
    runner_slot = threading.Lock()  # held while a processing job runs

    def recover() -> None:
        """A job whose runner died resumes from Continue, no reset wait."""
        try:
            conn = open_db(config.sqlite_path)
            try:
                jobs.recover_stale(conn, now_utc())
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    recover()

    @api.get("/api/books")
    def books():
        recover()
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
             "status": b["status"], "series": b["series"],
             "series_index": b["series_index"],
             "job": _job_out(by_book.get(b["id"]), now)}
            for b in rows], "gemini": gemini}

    def _process(book_id: str) -> None:
        conn = open_db(config.sqlite_path)  # its own connection: another thread
        try:
            run_book(conn, config, book_id, make_stages(config),
                     clock=now_utc, claimed=True)
        except JobRefused:
            pass  # raced a CLI run; its row stands
        except Exception as exc:  # noqa: BLE001 - a setup failure must not kill the thread
            try:  # the claimed row must not look running until it goes stale
                jobs.fail(conn, book_id, f"{type(exc).__name__}: {exc}"[:4000],
                          now=now_utc())
            except sqlite3.Error:
                pass
        finally:
            conn.close()
            runner_slot.release()

    @api.post("/api/books/{book_id}/continue", status_code=202)
    def continue_book(book_id: str):
        if not runner_slot.acquire(blocking=False):
            return _api_error(409, "busy", "another book is being processed")
        handed_off = False
        try:
            conn = open_db(config.sqlite_path)
            try:
                if book_id not in {b["id"] for b in list_books(conn)}:
                    return _api_error(404, "not_found", f"no book with id {book_id!r}")
                ok, why = jobs.can_continue(jobs.get_job(conn, book_id), now_utc())
                if not ok:
                    return _api_error(409, "not_resumable", why)
                try:  # the 202 implies running: mark it before the thread starts
                    jobs.claim_job(conn, book_id, now=now_utc())
                except JobRefused as exc:
                    return _api_error(409, "not_resumable", str(exc))
            finally:
                conn.close()
            threading.Thread(target=_process, args=(book_id,), daemon=True).start()
            handed_off = True
        finally:
            if not handed_off:
                runner_slot.release()
        return {"started": True}

    @api.get("/api/library")
    def library():
        return tools.library(config)

    @api.get("/api/entity")
    def entity(id: str | None = None, name: str | None = None):
        if id:
            return tools.entity_by_id(config, id)
        if name:
            return tools.entity(config, name)
        raise HTTPException(400, "give id or name")

    @api.get("/api/entities")
    def entities(ids: str = ""):
        return tools.entities_by_ids(config, [i for i in ids.split(",") if i])

    @api.get("/api/subgraph")
    def subgraph(id: str | None = None, name: str | None = None,
                 depth: int = Query(1, ge=1, le=3)):
        if id:
            return tools.subgraph_by_id(config, id, depth)
        if name:
            return tools.subgraph(config, name, depth)
        raise HTTPException(400, "give id or name")

    @api.get("/api/overview")
    def overview(limit: int = 40):
        return tools.overview(config, limit)

    @api.get("/api/quota")
    def quota():
        return _quota(config)

    @api.get("/api/mcp")
    def mcp():
        return {
            "stdio_command": f"claude mcp add lore -- {shlex.quote(_lore_executable())} mcp",
            "http_command": f"lore mcp --http --port {MCP_PORT}",
            "http_url": f"http://127.0.0.1:{MCP_PORT}/mcp",
            "http_running": _port_open(MCP_PORT),
        }

    @api.get("/api/settings")
    def settings():
        return {"show_internals": config.web.show_internals}

    lock = asyncio.Lock()

    @api.post("/api/ask")
    async def ask(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "send JSON")
        try:
            body = AskBody.model_validate(await request.json())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return StreamingResponse(
            ask_events(config, body.question, lock, search=tools.lore_search),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # Decided per request, not once here: building the frontend while the
    # server runs and reloading must serve it, as the not-built page says.
    @api.get("/", include_in_schema=False)
    def root():
        index = STATIC / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(_NOT_BUILT)

    api.mount("/", StaticFiles(directory=STATIC, html=True, check_dir=False),
              name="static")

    return OriginGuard(api, allowed_hosts(host, port), allowed_origins(host, port, dev))
