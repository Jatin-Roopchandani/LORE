"""Bounded concurrency for book processing's LLM requests.

On the server the extract stage spent most of its time waiting: the CPU
embedded a unit, then the LLM answered its chunks one at a time while the CPU
sat idle. A remote model billed per token (Claude, OpenAI, a paid Gemini
plan) serves several requests at once, so processing keeps up to
`llm_concurrency(config)` of them in flight. Free-tier Gemini stays at one:
its per-model RPM throttle and the daily ledger's reserve check
(`JobCtl.spend_check`) are written for one request at a time.

`bounded_fetch` is the one pattern every stage uses (extraction's chunks,
codex entities, beat parts, disambiguation batches):

  * at most `limit` fetches run at once, started in the given key order;
  * each key's outcome lands on its own future, so the caller consumes the
    results in its own (deterministic) order, whatever order they finished;
  * the first exception that escapes a fetch stops new fetches from
    starting (a fetch reports per-item failures it can live with as a value,
    never by raising); fetches already in flight finish, so an answer that
    was paid for is still saved, and keys never started are cancelled.

With `hold=True` a key keeps its slot until the caller `take`s the next key,
i.e. until the caller has finished with its result. At `limit` 1 that is
exactly the sequential loop it replaces (the next request starts only once
the previous item was fully handled); at `limit` n the item being handled
and n - 1 requests ahead of it share the bound.
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable, Iterable
from contextlib import asynccontextmanager
from typing import Generic, TypeVar

from lore.inference.providers import factory as providers

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


def llm_concurrency(config) -> int:
    """How many processing requests may be in flight at once for the provider
    the current unit of work runs on (the bound user's, or `[lore.llm]`)."""
    processing = config.processing
    if providers.active_provider(config) == providers.OPENAI_COMPATIBLE:
        return max(1, config.llm.concurrency)  # a self-hosted server: its own setting
    if providers.on_gemini(config):
        return max(1, processing.gemini_free_concurrency)
    return max(1, processing.llm_concurrency)


class Fetched(Generic[K]):
    """What `bounded_fetch` yields: a future per key, and `take`."""

    def __init__(self, futures: dict[K, asyncio.Future],
                 slots: asyncio.Semaphore | None) -> None:
        self._futures = futures
        self._slots = slots
        self._holding = False

    def __getitem__(self, key: K) -> asyncio.Future:
        return self._futures[key]

    def __contains__(self, key: K) -> bool:
        return key in self._futures

    def values(self):
        return self._futures.values()

    async def take(self, key: K):
        """The key's result (or its exception). With `hold`, this also frees
        the slot the previously taken key held."""
        if self._slots is not None and self._holding:
            self._slots.release()
        self._holding = True
        return await self._futures[key]


@asynccontextmanager
async def bounded_fetch(
    keys: Iterable[K],
    fetch: Callable[[K], Awaitable[V]],
    limit: int,
    *,
    hold: bool = False,
) -> AsyncIterator[Fetched[K]]:
    """Run `fetch(key)` for every key, at most `limit` at once, in key order.

    Yields a `Fetched`: `fetched[key]` is the key's future (raising its
    fetch's exception; a key never started because an earlier fetch raised
    is a cancelled future), `await fetched.take(key)` its result. With
    `hold`, consume the keys with `take`, in key order. Leaving the block
    (normally, or because the caller raised) stops new fetches and waits for
    the ones in flight; if the block is itself cancelled, those are
    cancelled too.
    """
    order = list(keys)
    loop = asyncio.get_running_loop()
    futures: dict[K, asyncio.Future] = {key: loop.create_future() for key in order}
    if len(futures) != len(order):
        raise ValueError("bounded_fetch needs unique keys")
    queue = deque(order)
    slots = asyncio.Semaphore(limit) if hold else None
    stopped = False

    async def worker() -> None:
        nonlocal stopped
        while queue and not stopped:
            if slots is not None:
                await slots.acquire()
                if not queue or stopped:
                    return
            key = queue.popleft()
            future = futures[key]
            try:
                value = await fetch(key)
            except Exception as exc:  # noqa: BLE001 - handed to the consumer
                stopped = True
                future.set_exception(exc)
            else:
                future.set_result(value)
            finally:
                # A CancelledError or a BaseException ends the worker; the
                # consumer must still never wait forever on this key.
                if not future.done():
                    stopped = True
                    future.cancel()

    count = max(1, min(limit, len(order)))
    workers = [asyncio.create_task(worker()) for _ in range(count)]
    try:
        yield Fetched(futures, slots)
    finally:
        stopped = True
        while queue:
            futures[queue.popleft()].cancel()
        if slots is not None:
            for _ in workers:  # wake any worker waiting for a slot: it sees `stopped`
                slots.release()
        try:
            await asyncio.gather(*workers, return_exceptions=True)
        except asyncio.CancelledError:
            for task in workers:
                task.cancel()
            raise
        finally:
            for future in futures.values():
                if future.done() and not future.cancelled():
                    future.exception()  # retrieved: no "never retrieved" noise
