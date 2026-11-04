"""The history API: each user's chats, on the server (release step 4).

Routes (every one per user; POST/PATCH/DELETE are behind the app-wide CSRF
guard in multi mode):
- GET    /api/chats?limit=&offset=  recent chats, newest first, paginated
- GET    /api/chats/{id}            one chat with its messages
- POST   /api/chats                 a new chat; with `client_id` and `messages`
                                    it imports one from the browser, idempotently
- PATCH  /api/chats/{id}            rename
- DELETE /api/chats/{id}            soft delete (its traces go at once)

Someone else's chat is 404, never 403, so ids cannot be probed. Local mode
runs the same code on a local central database, as the local user.

`record_answer` is how /api/ask appends a finished question and its answer.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from lore.central import history as H

log = logging.getLogger("lore.history")

MAX_IMPORT_MESSAGES = 50


class ImportMessage(BaseModel):
    question: str = Field(max_length=4000)
    answer: dict | None = None
    depth: str | None = Field(None, max_length=16)
    world: str | None = Field(None, max_length=200)


class NewChat(BaseModel):
    client_id: str | None = Field(None, min_length=1, max_length=64)
    title: str | None = Field(None, max_length=200)
    world: str | None = Field(None, max_length=200)
    created_at: datetime | None = None
    messages: list[ImportMessage] = Field(default_factory=list,
                                          max_length=MAX_IMPORT_MESSAGES)


class NewReport(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    note: str | None = Field(None, max_length=2000)


class Rename(BaseModel):
    title: str = Field(min_length=1, max_length=200)


def _not_found() -> JSONResponse:
    return JSONResponse({"error": {"code": "not_found", "message": "no such chat"}},
                        status_code=404)


def _history_full() -> JSONResponse:
    return JSONResponse({"error": {"code": "history_full", "message":
                                   "Your history is full. Delete some chats to save more."}},
                        status_code=409)


def _no_store(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def build_history_router(central: Callable[[], sessionmaker[Session]],
                         user_id: Callable[[Request], str],
                         now: Callable[[], datetime]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/chats")
    def chats(request: Request, limit: int = Query(H.PAGE_DEFAULT, ge=1, le=H.PAGE_MAX),
              offset: int = Query(0, ge=0)):
        uid = user_id(request)
        with central()() as session:
            rows, nxt = H.list_chats(session, uid, limit=limit, offset=offset)
            return _no_store({"chats": [H.chat_out(c, m) for c, m in rows],
                              "next_offset": nxt})

    @router.get("/api/chats/{chat_id}")
    def chat(request: Request, chat_id: str):
        uid = user_id(request)
        with central()() as session:
            try:
                found, messages = H.get_chat(session, uid, chat_id)
            except H.NotFound:
                return _not_found()
            return _no_store({"chat": H.chat_out(found),
                              "messages": [H.message_out(m) for m in messages]})

    @router.post("/api/chats")
    def new_chat(request: Request, body: NewChat):
        uid = user_id(request)
        created_at = body.created_at
        if created_at is not None and (created_at.tzinfo is None
                                       or created_at > now()):
            created_at = None  # a naive or future time from a browser is not trusted
        with central()() as session:
            try:
                made, created = H.create_chat(
                    session, uid, title=body.title, world=body.world,
                    client_id=body.client_id, created_at=created_at,
                    # An import never links to a trace: request ids are the server's.
                    messages=[m.model_dump() for m in body.messages], now=now())
            except H.NotFound:
                return _not_found()
            except H.HistoryFull:
                return _history_full()
            session.commit()
            return _no_store({"chat": H.chat_out(made), "created": created},
                             201 if created else 200)

    @router.patch("/api/chats/{chat_id}")
    def rename(request: Request, chat_id: str, body: Rename):
        uid = user_id(request)
        with central()() as session:
            try:
                found = H.rename_chat(session, uid, chat_id, body.title, now())
            except H.NotFound:
                return _not_found()
            session.commit()
            return _no_store({"chat": H.chat_out(found)})

    @router.delete("/api/chats/{chat_id}")
    def delete(request: Request, chat_id: str):
        uid = user_id(request)
        with central()() as session:
            try:
                H.delete_chat(session, uid, chat_id, now())
            except H.NotFound:
                return _not_found()
            session.commit()
            return _no_store({"deleted": True})

    # "Report this answer": only for the user's own questions (by their
    # trace), so nobody can attach notes to, or learn of, someone else's.
    @router.post("/api/reports")
    def report(request: Request, body: NewReport):
        from lore.central import traces
        from lore.central.models import Report
        uid = user_id(request)
        with central()() as session:
            if not traces.owned_by(session, body.request_id, uid):
                return JSONResponse({"error": {"code": "not_found",
                                               "message": "no such answer"}}, status_code=404)
            row = Report(user_id=uid, request_id=body.request_id,
                         note=(body.note or "").strip() or None, created_at=now(),
                         status="open")
            session.add(row)
            session.commit()
            return _no_store({"report": report_out(row)}, 201)

    return router


def report_out(row) -> dict:
    return {"id": row.id, "request_id": row.request_id, "note": row.note,
            "created_at": row.created_at.isoformat(), "status": row.status}


def answer_payload(result: dict, events: list[dict]) -> dict:
    """What a message keeps: the answer and the stream's other events, so the
    chat reopens exactly as it was shown."""
    steps = [e for e in events if e.get("type") not in ("answer", "request", "error")]
    return {"result": result, "steps": steps}


def standalone_of(events: list[dict]) -> str | None:
    """The follow-up's rewrite, from the stream's `understood` event; None when
    the question was answered as typed."""
    for event in events:
        if event.get("type") == "understood" and isinstance(event.get("standalone"), str):
            return event["standalone"] or None
    return None


def record_answer(session: Session, user: str, *, chat_id: str | None, question: str,
                  world, depth: str | None, result: dict, events: list[dict],
                  request_id: str | None, now: datetime) -> tuple[str, int]:
    """Append the finished answer to the user's chat (a new one when `chat_id`
    is None, or is not theirs any more): (chat id, message id). A follow-up's
    rewrite (its `understood` event) is stored with it."""
    world_s = None if world is None else str(world)
    payload = answer_payload(result, events)
    standalone = standalone_of(events)
    try:
        chat, message = H.append_message(
            session, user, chat_id, question=question, answer=payload, depth=depth,
            world=world_s, request_id=request_id, now=now, standalone=standalone)
    except H.NotFound:
        chat, message = H.append_message(
            session, user, None, question=question, answer=payload, depth=depth,
            world=world_s, request_id=request_id, now=now, standalone=standalone)
    return chat.id, message.id


def _answer_text(answer: dict | None) -> str:
    """What a turn of history says the answer was: its short answer, else the
    answer text (cut later by decompose.clip_history). "" for an answer that
    was not kept (too large) or has no text."""
    if not isinstance(answer, dict) or answer.get("truncated"):
        return ""
    result = answer.get("result")
    if not isinstance(result, dict):
        return ""
    short = [s.get("text") for s in result.get("short_answer") or []
             if isinstance(s, dict) and isinstance(s.get("text"), str)]
    if short:
        return " ".join(short)
    text = result.get("answer")
    return text if isinstance(text, str) else ""


def history_turns(messages, *, world) -> list[dict]:
    """A chat's last turns, as the history a follow-up is asked with: each
    {question, answer}, the question as it was understood (its standalone
    rewrite, when it had one) and the answer's short answer or its start.

    Only the user's own stored answers are used, all already limited by their
    reading position when given, and only those asked in the same world as the
    follow-up, so no other world's text reaches the prompt. An answer that was
    not kept, or had no text, is left out. Capped by decompose.clip_history:
    at most MAX_HISTORY_TURNS turns and MAX_HISTORY_CHARS characters."""
    from lore.query.decompose import clip_history
    world_s = None if world is None else str(world)
    turns = []
    for message in messages:
        if message.world != world_s:
            continue
        answer = _answer_text(message.answer_json)
        if answer:
            turns.append({"question": message.standalone or message.question,
                          "answer": answer})
    return [{"question": t.question, "answer": t.answer} for t in clip_history(turns)]
