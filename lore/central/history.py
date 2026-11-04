"""Each user's chats and messages in the central database (release step 4).

Every function takes the asking user's id and only ever touches that user's
rows: a chat id that belongs to someone else is `NotFound`, exactly like one
that does not exist, so ids cannot be probed. Soft-deleted chats are
`NotFound` too.

Limits: MAX_CHATS rows per user (soft-deleted chats are purged first, then
the least recently used); an answer payload whose JSON is over
MAX_ANSWER_BYTES is stored as a marker instead; and each user stores at most
MAX_USER_BYTES in all (each message's `size`): soft-deleted chats are evicted
first to make room, and past that a new message is refused (`HistoryFull`).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from lore.central.models import Chat, Message, User, utcnow

MAX_CHATS = 500
MAX_ANSWER_BYTES = 1_000_000
MAX_USER_BYTES = 50_000_000
TITLE_CHARS = 80
PAGE_DEFAULT = 50
PAGE_MAX = 200
LOCAL_EMAIL = "local@localhost"


class NotFound(Exception):
    """No such chat for this user (or it was deleted)."""


class HistoryFull(Exception):
    """The user's history holds MAX_USER_BYTES and nothing deleted is left to evict."""


def stored_size(question: str, answer: dict | None) -> int:
    """The bytes a message stores: its question and its answer as JSON."""
    size = len((question or "").encode("utf-8"))
    if answer is not None:
        size += len(json.dumps(answer, ensure_ascii=False).encode("utf-8"))
    return size


def used_bytes(session: Session, user_id: str) -> int:
    return int(session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(Message.size), 0))
        .join(Chat, Chat.id == Message.chat_id).where(Chat.user_id == user_id)) or 0)


def make_room(session: Session, user_id: str, needed: int) -> None:
    """Fit `needed` more bytes under the user's quota: evict soft-deleted
    chats, oldest first, while over; HistoryFull when that is not enough."""
    used = used_bytes(session, user_id)
    if used + needed <= MAX_USER_BYTES:
        return
    deleted = session.execute(
        sa.select(Chat.id, sa.func.coalesce(sa.func.sum(Message.size), 0))
        .outerjoin(Message, Message.chat_id == Chat.id)
        .where(Chat.user_id == user_id, Chat.deleted_at.is_not(None))
        .group_by(Chat.id, Chat.deleted_at).order_by(Chat.deleted_at, Chat.id)).all()
    doomed = []
    for chat_id, size in deleted:
        if used + needed <= MAX_USER_BYTES:
            break
        doomed.append(chat_id)
        used -= int(size)
    if used + needed > MAX_USER_BYTES:
        raise HistoryFull(user_id)
    purge_chats(session, doomed)


def ensure_user(session: Session, user_id: str, email: str = LOCAL_EMAIL) -> None:
    """Local mode: the one local user needs a `users` row for its chats and
    traces (multi mode's users are made at sign-in)."""
    if session.get(User, user_id) is None:
        session.add(User(id=user_id, email=email, name=None))
        session.flush()


def title_from(question: str) -> str:
    text = re.sub(r"\s+", " ", question or "").strip()
    if not text:
        return "Untitled"
    return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS - 1].rstrip() + "…"


def cap_answer(answer: dict | None) -> dict | None:
    """The payload as stored: itself, or a marker past MAX_ANSWER_BYTES."""
    if answer is None:
        return None
    size = len(json.dumps(answer, ensure_ascii=False).encode("utf-8"))
    if size <= MAX_ANSWER_BYTES:
        return answer
    return {"truncated": True, "bytes": size}


def _live(session: Session, user_id: str, chat_id: str) -> Chat:
    chat = session.get(Chat, chat_id) if isinstance(chat_id, str) else None
    if chat is None or chat.user_id != user_id or chat.deleted_at is not None:
        raise NotFound(chat_id)
    return chat


def get_chat(session: Session, user_id: str, chat_id: str) -> tuple[Chat, list[Message]]:
    chat = _live(session, user_id, chat_id)
    messages = session.scalars(
        sa.select(Message).where(Message.chat_id == chat.id).order_by(Message.id)).all()
    return chat, list(messages)


def list_chats(session: Session, user_id: str, *, limit: int = PAGE_DEFAULT,
               offset: int = 0) -> tuple[list[tuple[Chat, Message | None]], int | None]:
    """The user's live chats, most recently used first, each with its latest
    message; and the offset of the next page (None at the end)."""
    limit = max(1, min(int(limit), PAGE_MAX))
    offset = max(0, int(offset))
    chats = session.scalars(
        sa.select(Chat).where(Chat.user_id == user_id, Chat.deleted_at.is_(None))
        .order_by(Chat.updated_at.desc(), Chat.id.desc())
        .offset(offset).limit(limit + 1)).all()
    more = len(chats) > limit
    chats = list(chats[:limit])
    latest: dict[str, Message] = {}
    if chats:
        ids = [c.id for c in chats]
        newest = (sa.select(Message.chat_id, sa.func.max(Message.id).label("mid"))
                  .where(Message.chat_id.in_(ids)).group_by(Message.chat_id).subquery())
        for message in session.scalars(
                sa.select(Message).join(newest, Message.id == newest.c.mid)):
            latest[message.chat_id] = message
    return [(c, latest.get(c.id)) for c in chats], (offset + limit if more else None)


def create_chat(session: Session, user_id: str, *, title: str | None = None,
                world: str | None = None, client_id: str | None = None,
                created_at: datetime | None = None, messages: list[dict] | None = None,
                now: datetime | None = None) -> tuple[Chat, bool]:
    """A new chat (with `messages`, for an import), or the user's existing chat
    with that `client_id`: (chat, created)."""
    now = now or utcnow()
    if client_id is not None:
        found = session.scalars(sa.select(Chat).where(
            Chat.user_id == user_id, Chat.client_id == client_id)).first()
        if found is not None:
            if found.deleted_at is not None:
                raise NotFound(client_id)  # deleted on purpose: not brought back
            return found, False
    messages = [{**m, "question": str(m.get("question") or ""),
                 "answer": cap_answer(m.get("answer"))} for m in (messages or [])]
    make_room(session, user_id, sum(stored_size(m["question"], m["answer"])
                                    for m in messages))
    first_question = messages[0]["question"] if messages else ""
    made = created_at or now
    chat = Chat(id=str(uuid.uuid4()), user_id=user_id, client_id=client_id,
                world=world, title=(title or title_from(first_question))[:200],
                created_at=made, updated_at=made)
    session.add(chat)
    session.flush()
    for m in messages:
        session.add(Message(
            chat_id=chat.id, role="assistant", question=m["question"],
            answer_json=m["answer"], depth=m.get("depth"),
            world=m.get("world", world), request_id=m.get("request_id"),
            created_at=made, size=stored_size(m["question"], m["answer"])))
    session.flush()
    enforce_limit(session, user_id)
    return chat, True


def rename_chat(session: Session, user_id: str, chat_id: str, title: str,
                now: datetime | None = None) -> Chat:
    chat = _live(session, user_id, chat_id)
    chat.title = (re.sub(r"\s+", " ", title).strip() or chat.title)[:200]
    chat.updated_at = now or utcnow()
    session.flush()
    return chat


def delete_chat(session: Session, user_id: str, chat_id: str,
                now: datetime | None = None) -> None:
    """Soft delete; the chat's traces are deleted at once (they hold its
    passages and questions)."""
    chat = _live(session, user_id, chat_id)
    chat.deleted_at = now or utcnow()
    _delete_traces(session, [chat.id])
    session.flush()


def append_message(session: Session, user_id: str, chat_id: str | None, *,
                   question: str, answer: dict | None, depth: str | None,
                   world: str | None, request_id: str | None,
                   now: datetime | None = None) -> tuple[Chat, Message]:
    """Add a question and its answer to the user's chat, or to a new chat
    titled from the question when `chat_id` is None."""
    now = now or utcnow()
    answer = cap_answer(answer)
    size = stored_size(question, answer)
    if chat_id is None:
        make_room(session, user_id, size)
        chat, _ = create_chat(session, user_id, title=title_from(question),
                              world=world, now=now)
    else:
        chat = _live(session, user_id, chat_id)
        make_room(session, user_id, size)
        chat.updated_at = now
    message = Message(chat_id=chat.id, role="assistant", question=question,
                      answer_json=answer, depth=depth, world=world,
                      request_id=request_id, created_at=now, size=size)
    session.add(message)
    session.flush()
    return chat, message


def enforce_limit(session: Session, user_id: str, limit: int | None = None) -> int:
    """Purge the user's rows over `limit`: soft-deleted chats first (oldest
    first), then the least recently used. Returns how many went."""
    limit = MAX_CHATS if limit is None else limit
    total = session.scalar(sa.select(sa.func.count()).select_from(Chat)
                           .where(Chat.user_id == user_id)) or 0
    excess = total - limit
    if excess <= 0:
        return 0
    doomed = session.scalars(
        sa.select(Chat.id).where(Chat.user_id == user_id)
        .order_by(Chat.deleted_at.is_(None), Chat.updated_at, Chat.id)
        .limit(excess)).all()
    purge_chats(session, list(doomed))
    return len(doomed)


def purge_chats(session: Session, chat_ids: list[str]) -> None:
    """Delete chats for good, with their messages and traces (explicitly, so
    a backend without enforced foreign keys loses nothing either)."""
    if not chat_ids:
        return
    _delete_traces(session, chat_ids)
    session.execute(sa.delete(Message).where(Message.chat_id.in_(chat_ids)))
    session.execute(sa.delete(Chat).where(Chat.id.in_(chat_ids)))
    session.flush()


def _delete_traces(session: Session, chat_ids: list[str]) -> None:
    """Traces linked to these chats (the table arrives with migration 0005)."""
    try:
        from lore.central.models import Trace
    except ImportError:
        return
    session.execute(sa.delete(Trace).where(Trace.chat_id.in_(chat_ids)))


def chat_out(chat: Chat, latest: Message | None = None, *, full: bool = False) -> dict:
    out = {"id": chat.id, "title": chat.title, "world": chat.world,
           "client_id": chat.client_id,
           "created_at": chat.created_at.isoformat(),
           "updated_at": chat.updated_at.isoformat()}
    if latest is not None:
        out["latest"] = message_out(latest, full=full)
    return out


def message_out(message: Message, *, full: bool = True) -> dict:
    out = {"id": message.id, "role": message.role, "question": message.question,
           "depth": message.depth, "world": message.world,
           "request_id": message.request_id,
           "created_at": message.created_at.isoformat()}
    answer = message.answer_json or {}
    if full:
        out["answer"] = answer
    else:  # enough for the drawer (deep or not, truncated or not), not the payload
        result = answer.get("result") or {}
        out["summary"] = {"answer_style": result.get("answer_style"),
                          "mode": result.get("mode"),
                          "deep": result.get("answer_style") == "sectioned"
                          or any(s.get("type") == "plan" for s in answer.get("steps") or []),
                          "truncated": bool(answer.get("truncated"))}
    return out
