"""Progress from deep inside a long step, to whoever is watching (no import cycle).

A stage binds a callback for the work it runs (`with progress.bound(cb):`);
code far below it (the entity resolver, one embedding per mention) calls
`progress.report(done, total)` without knowing who listens. Nothing bound:
a report is a no-op. A ContextVar, so it follows the work into `asyncio.run`.
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextvars import ContextVar

_BOUND: ContextVar[Callable[[int, int], None] | None] = ContextVar(
    "lore_progress", default=None)


@contextlib.contextmanager
def bound(callback: Callable[[int, int], None]) -> Iterator[None]:
    token = _BOUND.set(callback)
    try:
        yield
    finally:
        _BOUND.reset(token)


def report(done: int, total: int) -> None:
    callback = _BOUND.get()
    if callback is not None:
        callback(done, total)
