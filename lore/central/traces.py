"""Storing and reading question traces (release step 4).

`save` writes a lore.tracing.Trace as a `traces` row; `get` reads one back
for an admin or `lore trace`; `purge_expired` deletes rows older than
RETENTION (run at web start-up and from the worker loop, cheaply: one
indexed delete). Deleting a chat or an account deletes its traces
(lore.central.history, and the foreign keys).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from lore.central.models import Trace as TraceRow, utcnow
from lore.tracing import Trace, clean

RETENTION = timedelta(days=30)
MAX_JSON_BYTES = 1_000_000


def _capped(value, limit: int = MAX_JSON_BYTES):
    """`value` as stored: itself, or a marker past `limit` bytes of JSON."""
    if value is None:
        return None
    size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    return value if size <= limit else {"truncated": True, "bytes": size}


def save(session: Session, trace: Trace, *, user_id: str, question: str,
         world=None, depth: str | None = None, chat_id: str | None = None,
         message_id: int | None = None, now: datetime | None = None) -> TraceRow:
    row = TraceRow(
        request_id=trace.request_id, user_id=user_id, chat_id=chat_id,
        message_id=message_id, created_at=now or utcnow(),
        question=clean(question) or "", world=None if world is None else str(world)[:200],
        depth=depth, decomposition_json=_capped(trace.decomposition or None),
        steps_json=_capped(trace.steps), calls_json=_capped(trace.calls_out()),
        answer_json=_capped(trace.answer), error=trace.error,
        timings_json=trace.timings(), model_usage_json=trace.model_usage())
    session.merge(row)
    session.flush()
    return row


def get(session: Session, request_id: str) -> TraceRow | None:
    return session.get(TraceRow, request_id) if isinstance(request_id, str) else None


def owned_by(session: Session, request_id: str, user_id: str) -> bool:
    row = get(session, request_id)
    return row is not None and row.user_id == user_id


def purge_expired(session: Session, now: datetime | None = None) -> int:
    cutoff = (now or utcnow()) - RETENTION
    result = session.execute(sa.delete(TraceRow).where(TraceRow.created_at < cutoff))
    return result.rowcount or 0


def trace_out(row: TraceRow) -> dict:
    return {
        "request_id": row.request_id, "user_id": row.user_id, "chat_id": row.chat_id,
        "message_id": row.message_id, "created_at": row.created_at.isoformat(),
        "question": row.question, "world": row.world, "depth": row.depth,
        "decomposition": row.decomposition_json, "steps": row.steps_json,
        "calls": row.calls_json, "answer": row.answer_json, "error": row.error,
        "timings": row.timings_json, "model_usage": row.model_usage_json,
    }


def render_text(row: TraceRow) -> str:
    """A trace as readable text (`lore trace`): the question, the
    decomposition, each call, then the answer."""
    out = [f"Request {row.request_id}  ({row.created_at.isoformat()})",
           f"User: {row.user_id}   World: {row.world or '-'}   Depth: {row.depth or '-'}",
           f"Question: {row.question}", ""]
    if row.decomposition_json:
        out += ["Decomposition:", json.dumps(row.decomposition_json, indent=2,
                                             ensure_ascii=False), ""]
    calls = row.calls_json if isinstance(row.calls_json, list) else []
    out.append(f"Model calls ({len(calls)}):")
    for i, call in enumerate(calls, 1):
        status = "ok" if call.get("ok") else f"FAILED: {call.get('error')}"
        out.append(f"--- {i}. {call.get('stage')} on {call.get('model') or '?'}"
                   f" ({call.get('latency_ms')} ms) {status}")
        for attempt in call.get("attempts") or []:
            out.append(f"    attempt {attempt.get('model')}: "
                       + ("ok" if attempt.get("ok") else f"failed ({attempt.get('error')})"))
        out += ["  prompt:", _indent(call.get("prompt")), "  reply:", _indent(call.get("reply"))]
    out.append("")
    if row.error:
        out.append(f"Error: {row.error}")
    if row.answer_json:
        answer = row.answer_json.get("answer") if isinstance(row.answer_json, dict) else None
        out += ["Answer:", answer if isinstance(answer, str)
                else json.dumps(row.answer_json, indent=2, ensure_ascii=False)]
    out += ["", f"Timings: {json.dumps(row.timings_json)}",
            f"Requests by model: {json.dumps(row.model_usage_json)}"]
    return "\n".join(out)


def _indent(text: str | None) -> str:
    return "\n".join("    " + line for line in (text or "").splitlines()) or "    (none)"
