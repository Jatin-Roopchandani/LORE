"""Download my data: one zip of the user's own chats and their books' metadata.

POST /api/me/export (a POST so it carries the CSRF token in multi mode, like
every mutating route; nothing is changed) answers a zip with:

- `chats.json`: every live chat of the user's, with every message and its
  stored answer (lore.central.history's own shapes);
- `books.json`: the books in the user's library (title, author, series,
  world, status, when added) and their worlds;
- `README.txt`: what the files are.

Never in it: provider keys (sealed or their last four), sessions, sign-in
identities, traces (internals), the books' files or graph, or server paths.
The user and their library come from the request's session (`central_user`,
`user_config`), so a user can only ever export their own.
"""
from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import sessionmaker

from lore.central import history
from lore.central.models import Chat, Message
from lore.config import LoreConfig

README = """Your LORE data, exported {when}.

chats.json  Your chats: each question you asked and the answer LORE gave, with
            the world and depth it was asked in.
books.json  The books in your library (title, author, series, world, status)
            and your worlds. The book files and the graph built from them are
            not included.

Your AI provider keys are never exported.
"""

BOOK_FIELDS = ("id", "title", "author", "series", "series_index", "world", "status",
               "chunk_count")


def chats_of(sessions: sessionmaker, user_id: str) -> list[dict]:
    with sessions() as db:
        chats = db.scalars(sa.select(Chat).where(
            Chat.user_id == user_id, Chat.deleted_at.is_(None))
            .order_by(Chat.created_at, Chat.id)).all()
        out = []
        for chat in chats:
            messages = db.scalars(sa.select(Message).where(Message.chat_id == chat.id)
                                  .order_by(Message.id)).all()
            item = history.chat_out(chat)
            item["messages"] = [history.message_out(m, full=True) for m in messages]
            out.append(item)
    return out


def books_of(config: LoreConfig) -> dict:
    from lore.db.sqlite import open_db
    from lore.ingest.registry import list_books
    from lore.worlds import list_worlds
    if not config.sqlite_path.exists():  # a library never opened: nothing in it
        return {"books": [], "worlds": []}
    conn = open_db(config.sqlite_path)
    try:
        rows, worlds = list_books(conn), list_worlds(conn)
    finally:
        conn.close()
    return {"books": [{k: row.get(k) for k in BOOK_FIELDS} for row in rows],
            "worlds": [{"id": w.id, "name": w.name} for w in worlds]}


def build_zip(chats: list[dict], books: dict, now: datetime) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", README.format(when=now.strftime("%Y-%m-%d %H:%M UTC")))
        zf.writestr("chats.json", json.dumps({"chats": chats}, ensure_ascii=False, indent=2))
        zf.writestr("books.json", json.dumps(books, ensure_ascii=False, indent=2))
    return buffer.getvalue()


def build_export_router(central: Callable[[], sessionmaker],
                        central_user: Callable[[Request], str],
                        user_config: Callable[..., LoreConfig]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/me/export")
    def export(request: Request, config: LoreConfig = Depends(user_config)):
        user_id = central_user(request)
        now = datetime.now(timezone.utc)
        data = build_zip(chats_of(central(), user_id), books_of(config), now)
        name = f"lore-data-{now.strftime('%Y-%m-%d')}.zip"

        def chunks():
            for start in range(0, len(data), 64 * 1024):
                yield data[start:start + 64 * 1024]

        return StreamingResponse(chunks(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store"})

    return router
