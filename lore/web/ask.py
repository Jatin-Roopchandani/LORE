"""The stream behind POST /api/ask.

lore_search runs as a task; its progress events go through a queue to the
response as server-sent events, each stamped with seconds since the start.
One question at a time (Gemini's 15 RPM makes parallel asks pointless):
a request while one runs gets a `busy` error event. If the client goes
away, Starlette closes this generator, and the `finally` cancels the run
so it stops spending Gemini requests.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import AsyncIterator

from lore.mcp import tools

log = logging.getLogger(__name__)

BUSY = {"code": "busy", "message": "LORE is still answering the previous question."}


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, allow_nan=False)}\n\n".encode()


async def ask_events(config, question: str, lock: asyncio.Lock,
                     search=None, world=None, depth=None,
                     spoilers=False, on_finish=None,
                     request_id: str | None = None,
                     history=None) -> AsyncIterator[bytes]:
    """`history` (a chat's last turns, loaded by the server) makes the question
    a follow-up; passed to the search only when there is some.

    `request_id`, when given, is the first event (`request`), so the UI
    can quote it in a report. `on_finish(result, error, events)`, when given, runs once the question
    has ended, before the final event is sent: `result` is the answer (None
    when it failed), `error` the error dict (None on success; code
    `cancelled` when the client went away), `events` every event sent, each
    with its `type`. What it returns is merged into the final event (the
    chat the answer was saved to). It must not raise into the stream: a
    failure there is logged and the answer is still sent."""
    search = search or tools.lore_search
    started = time.monotonic()
    sent: list[dict] = []
    finished = False

    def stamp(payload: dict) -> dict:
        return {"t": round(time.monotonic() - started, 2), **payload}

    def emit(kind: str, payload: dict) -> bytes:
        sent.append({"type": kind, **payload})
        return sse(kind, payload)

    def finish(result, error) -> dict:
        nonlocal finished
        finished = True
        if on_finish is None:
            return {}
        try:
            return on_finish(result, error, list(sent)) or {}
        except Exception:  # noqa: BLE001 -- saving must never cost the answer
            log.exception("saving the finished question failed")
            return {}

    if lock.locked():
        yield sse("error", stamp({"error": BUSY}))
        return
    async with lock:
        if request_id is not None:
            yield emit("request", stamp({"request_id": request_id}))
        queue: asyncio.Queue[dict] = asyncio.Queue()
        getter = None
        # `world` only when given, so a search that takes none still works.
        scope = {} if world is None else {"world": world}
        if depth is not None and depth != "auto":
            scope["depth"] = depth
        if spoilers:  # only when the reader asked to reveal, for this question
            scope["spoilers"] = True
        if history:
            scope["history"] = history
        task = asyncio.create_task(search(config, question, on_event=queue.put_nowait,
                                          **scope))
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, task},
                                             return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    event = getter.result()
                    yield emit(event.pop("type"), stamp(event))
                    continue
                getter.cancel()
                break
            while not queue.empty():
                event = queue.get_nowait()
                yield emit(event.pop("type"), stamp(event))
            try:
                result = task.result()
            except Exception:
                log.exception("lore_search failed")
                error = {"code": "internal",
                         "message": "LORE hit an unexpected error while answering."
                                    " The server log has the details."}
                finish(None, error)
                yield emit("error", stamp({"error": error}))
                return
            if isinstance(result, dict) and "error" in result:
                finish(None, result["error"])
                yield emit("error", stamp({"error": result["error"]}))
            else:
                extra = finish(result, None)
                yield emit("answer", stamp({"result": result, **extra}))
        finally:
            if not finished:  # the client went away mid-answer
                finish(None, {"code": "cancelled", "message": "the client went away"})
            if getter is not None and not getter.done():
                getter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await getter
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
