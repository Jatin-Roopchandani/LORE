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
                     search=None, world=None, depth=None) -> AsyncIterator[bytes]:
    search = search or tools.lore_search
    started = time.monotonic()

    def stamp(payload: dict) -> dict:
        return {"t": round(time.monotonic() - started, 2), **payload}

    if lock.locked():
        yield sse("error", stamp({"error": BUSY}))
        return
    async with lock:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        getter = None
        # `world` only when given, so a search that takes none still works.
        scope = {} if world is None else {"world": world}
        if depth is not None and depth != "auto":
            scope["depth"] = depth
        task = asyncio.create_task(search(config, question, on_event=queue.put_nowait,
                                          **scope))
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, task},
                                             return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    event = getter.result()
                    yield sse(event.pop("type"), stamp(event))
                    continue
                getter.cancel()
                break
            while not queue.empty():
                event = queue.get_nowait()
                yield sse(event.pop("type"), stamp(event))
            try:
                result = task.result()
            except Exception:
                log.exception("lore_search failed")
                yield sse("error", stamp({"error": {
                    "code": "internal",
                    "message": "LORE hit an unexpected error while answering."
                              " The server log has the details."}}))
                return
            if isinstance(result, dict) and "error" in result:
                yield sse("error", stamp({"error": result["error"]}))
            else:
                yield sse("answer", stamp({"result": result}))
        finally:
            if getter is not None and not getter.done():
                getter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await getter
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
