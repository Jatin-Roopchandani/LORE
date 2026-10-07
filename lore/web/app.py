"""lore ui: the browser's API, a thin layer over lore.mcp.tools.

Every endpoint calls the same function the matching MCP tool calls, so
the UI, Claude Code and the evals see the same data and the same
structured errors (tool errors are HTTP 200 with an `error` body, as over
MCP; 4xx only for a malformed request to this API).
"""
from __future__ import annotations

import asyncio
import shutil
import socket
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from lore.config import LoreConfig
from lore.mcp import tools
from lore.orchestrator.gemini_budget import GOOGLE_DAY_ZONE, day_report, today
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


def create_app(config: LoreConfig, *, host: str = "127.0.0.1", port: int = 8766,
               dev: bool = False):
    api = FastAPI(title="LORE", docs_url=None, redoc_url=None)

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

    @api.get("/api/subgraph")
    def subgraph(id: str | None = None, name: str | None = None,
                 depth: int = Query(1, ge=1, le=3)):
        if id:
            return tools.subgraph_by_id(config, id, depth)
        if name:
            return tools.subgraph(config, name, depth)
        raise HTTPException(400, "give id or name")

    @api.get("/api/quota")
    def quota():
        return _quota(config)

    @api.get("/api/mcp")
    def mcp():
        return {
            "stdio_command": f"claude mcp add lore -- {_lore_executable()} mcp",
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

    if (STATIC / "index.html").exists():
        api.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    else:
        @api.get("/", response_class=HTMLResponse)
        def not_built():
            return _NOT_BUILT

    return OriginGuard(api, allowed_hosts(host, port), allowed_origins(host, port, dev))
