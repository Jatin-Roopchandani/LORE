"""The book-processing stages that are not extraction (see run.py / pipeline.py).

Each stage reports where it is through `job_ctl.advance(<already done>)` at
its start (the runner resets the count to 0 on every stage start), checks the
Gemini budget before spending where it can, and turns "no model could serve"
into `pipeline.QuotaStop`, so the runner parks the job until the allowance
resets instead of marking it failed. Re-running a stage is cheap: load is
idempotent and disambiguation answers are cached per question.

The stages take their heavy dependencies (the load function, the Gemini
client factory) as arguments so the CLI can wire the real ones and tests can
wire fakes.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from lore.inference.gemini import (
    GeminiClient,
    GeminiQuotaError,
    default_key_file,
    resolve_api_key,
)
from lore.orchestrator.gemini_budget import recorder_for
from lore.orchestrator.pipeline import JobCtl, QuotaStop, StageContext
from lore.resolve.disambiguation import (
    MAX_BATCH,
    DisambiguationStore,
    ask as ask_disambiguation,
)


def quota_stop_from(exc: GeminiQuotaError) -> QuotaStop:
    """The runner's quota signal for a cascade that ran out of models."""
    return QuotaStop(f"Gemini has no model left to serve this: {exc}")


class ChunkStage:
    """No work: the book was chunked when it was ingested. Checks it was.

    An unknown book id or a book with no chunks fails here, with a message,
    before any Gemini request or job row is spent on it.
    """

    name = "chunk"

    def total(self, ctx: StageContext) -> int:
        return 0

    def run(self, ctx: StageContext, job_ctl: JobCtl) -> None:
        job_ctl.advance(0)
        row = ctx.conn.execute(
            "SELECT (SELECT COUNT(*) FROM chunks WHERE book_id = b.id)"
            " FROM books b WHERE b.id = ?", (ctx.book_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"unknown book {ctx.book_id}")
        if row[0] == 0:
            raise RuntimeError(f"book {ctx.book_id} has no chunks; re-ingest it")


class LoadStage:
    """Resolve entities and write the graph and vector index.

    `load_fn(config, conn, book_id)` does the load and returns its report,
    kept as `self.report`. The load is idempotent and its adjudication
    answers are cached, so a re-run asks only questions never answered.
    """

    name = "load"

    def __init__(self, load_fn: Callable) -> None:
        self._load_fn = load_fn
        self.report = None

    def total(self, ctx: StageContext) -> int:
        return 1

    def run(self, ctx: StageContext, job_ctl: JobCtl) -> None:
        job_ctl.advance(0)
        if ctx.config.adjudication.provider == "gemini":
            job_ctl.spend_check()
        self.reload(ctx)
        job_ctl.advance(1)

    def reload(self, ctx: StageContext) -> None:
        """Load again, keeping the fresh report (also used to apply answers)."""
        try:
            self.report = self._load_fn(ctx.config, ctx.conn, ctx.book_id)
        except GeminiQuotaError as exc:
            raise quota_stop_from(exc) from exc


class DisambiguateStage:
    """Answer the ambiguous merges the load refused, one batch per request.

    A no-op unless `[lore.disambiguation] enabled`. Each batch's answers are
    cached as soon as they are verified, so a stop after batch 2 of 4 keeps
    batches 1-2 and a resume asks only the rest. `client_factory(config)`
    returns a GeminiClient-like object (generate, aclose). When this run
    obtained any new answers, `reload(ctx)` runs once at the end to apply
    them (the load is free: its adjudication answers are cached).
    """

    name = "disambiguate"

    def __init__(self, client_factory: Callable | None = None,
                 reload: Callable[[StageContext], None] | None = None) -> None:
        self._client_factory = client_factory or _gemini_client
        self._reload = reload

    @staticmethod
    def _store(ctx: StageContext) -> DisambiguationStore:
        return DisambiguationStore(ctx.config.disambiguation_dir)

    def _counts(self, ctx: StageContext):
        """(unanswered questions, total batches, batches already answered)."""
        if not ctx.config.disambiguation.enabled:
            return [], 0, 0
        store = self._store(ctx)
        pending = store.pending()
        todo = store.unanswered(pending)
        total = -(-len(pending) // MAX_BATCH)
        return todo, total, total - (-(-len(todo) // MAX_BATCH))

    def total(self, ctx: StageContext) -> int:
        return self._counts(ctx)[1]

    def run(self, ctx: StageContext, job_ctl: JobCtl) -> None:
        todo, _total, already = self._counts(ctx)
        job_ctl.advance(already)
        if not todo:
            return
        store = self._store(ctx)
        client = self._client_factory(ctx.config)
        got_new = False

        async def _run() -> None:
            nonlocal got_new
            # One loop for the requests and the close: httpx binds its
            # sockets to the loop that opened them.
            try:
                for start in range(0, len(todo), MAX_BATCH):
                    job_ctl.spend_check()
                    answers = await ask_disambiguation(
                        client, todo[start:start + MAX_BATCH])
                    store.save_answers(answers)
                    got_new = got_new or bool(answers)
                    job_ctl.advance(already + start // MAX_BATCH + 1)
            finally:
                await client.aclose()

        try:
            asyncio.run(_run())
        except GeminiQuotaError as exc:
            raise quota_stop_from(exc) from exc
        if got_new and self._reload is not None:
            self._reload(ctx)


def _gemini_client(config) -> GeminiClient:
    api_key = resolve_api_key(key_file=default_key_file())
    if api_key is None:
        raise RuntimeError(
            "disambiguation needs a Gemini API key (GEMINI_API_KEY or"
            " ~/.config/lore/gemini.env)")
    return GeminiClient(api_key, models=config.disambiguation.models,
                        recorder=recorder_for(config.sqlite_path))


class BeatsStage:
    """Placeholder until the story-beats spec; does nothing."""

    name = "beats"

    def total(self, ctx: StageContext) -> int:
        return 0

    def run(self, ctx: StageContext, job_ctl: JobCtl) -> None:
        job_ctl.advance(0)
