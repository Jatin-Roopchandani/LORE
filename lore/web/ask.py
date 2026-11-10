"""The stream behind POST /api/ask.

lore_search runs as a task; its progress events go through a queue to the
response as server-sent events, each stamped with seconds since the start.
One question at a time (Gemini's 15 RPM makes parallel asks pointless):
a request while one runs gets a `busy` error event.

Stopping: the run is cancelled, so no further model call is made, either
when the client goes away (Starlette cancels or closes this generator, and
the `finally` cancels the run) or when `stop` is set (the reader pressed
Stop: POST /api/ask/{request_id}/cancel, which works even where a proxy such
as Cloudflare Tunnel does not pass the disconnect on promptly). A stopped
run ends with a `stopped` event; neither way saves it as an answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

import anyio

from lore.mcp import tools

log = logging.getLogger(__name__)

BUSY = {"code": "busy", "message": "LORE is still answering the previous question."}
# How a question that did not finish is recorded (its trace's error, and the
# `error` handed to on_finish): the reader pressed Stop, or the client went away.
STOPPED = {"code": "stopped", "message": "the reader stopped the answer"}
CANCELLED = {"code": "cancelled", "message": "the client went away"}
# How long a cancelled run may take to unwind (its `finally` blocks: closing
# model clients) before the stream ends without waiting for it.
UNWIND_S = 5.0


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, allow_nan=False)}\n\n".encode()


async def cancel_and_wait(task: asyncio.Future | None, timeout: float = UNWIND_S) -> None:
    """Cancel `task` and give it up to `timeout` seconds to unwind, shielded
    from a cancellation of the caller (Starlette cancels the response's scope
    when the client goes away, and keeps cancelling every await inside it).
    The run sees its CancelledError at its own await, so no further model
    call starts, and what it spent is recorded before the caller carries on."""
    if task is None or task.done():
        return
    task.cancel()
    with anyio.CancelScope(shield=True):
        await asyncio.wait({task}, timeout=timeout)
    if task.done() and not task.cancelled():
        task.exception()  # retrieved, so asyncio does not log it as never retrieved


async def ask_events(config, question: str, lock: asyncio.Lock,
                     search=None, world=None, depth=None,
                     spoilers=False, on_finish=None,
                     request_id: str | None = None,
                     history=None, public=None,
                     stop: asyncio.Event | None = None) -> AsyncIterator[bytes]:
    """`history` (a chat's last turns, loaded by the server) makes the question
    a follow-up; passed to the search only when there is some.

    `request_id`, when given, is the first event (`request`), so the UI
    can quote it in a report (and stop it). `on_finish(result, error, events)`, when given, runs once the question
    has ended, before the final event is sent: `result` is the answer (None
    when it failed), `error` the error dict (None on success; code
    `cancelled` when the client went away, `stopped` when `stop` was set),
    `events` every event sent, each
    with its `type`. What it returns is merged into the final event (the
    chat the answer was saved to). It must not raise into the stream: a
    failure there is logged and the answer is still sent.

    `public(kind, payload)`, when given (multi mode: lore.web.friendly), is
    what the client sees of each event: no model names, no raw errors. The
    events handed to `on_finish` are those public ones too (they become the
    chat's replay); `result` and `error` stay raw for the trace.

    `stop`, when set while the question runs, cancels it: no further model
    call is made, on_finish gets the `stopped` error, and the stream ends
    with a `stopped` event."""
    search = search or tools.lore_search
    started = time.monotonic()
    sent: list[dict] = []
    finished = False

    def stamp(payload: dict) -> dict:
        return {"t": round(time.monotonic() - started, 2), **payload}

    def emit(kind: str, payload: dict) -> bytes:
        if public is not None:
            payload = public(kind, payload)
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
        busy = stamp({"error": BUSY})
        yield sse("error", public("error", busy) if public is not None else busy)
        return
    async with lock:
        if request_id is not None:
            yield emit("request", stamp({"request_id": request_id}))
        queue: asyncio.Queue[dict] = asyncio.Queue()
        getter = None
        stopper = asyncio.ensure_future(stop.wait()) if stop is not None else None
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
                waits = {getter, task} if stopper is None else {getter, task, stopper}
                done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    event = getter.result()
                    yield emit(event.pop("type"), stamp(event))
                    continue
                getter.cancel()
                if task not in done:  # stopped: the run ends here, mid-answer
                    await cancel_and_wait(task)
                    log.info("the answer was stopped by the reader")
                    finish(None, STOPPED)
                    yield emit("stopped", stamp({}))
                    return
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
            await cancel_and_wait(getter)
            await cancel_and_wait(stopper)
            # Then the run, so no further model call starts and the calls it
            # made are in its trace before that is saved.
            await cancel_and_wait(task)
            if not finished:  # the client went away mid-answer
                finish(None, CANCELLED)
