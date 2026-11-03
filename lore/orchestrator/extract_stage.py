"""The extraction stage of resumable processing (Gemini tier).

Items are chunks. A unit saves each chunk as it returns (see
`run_unit_gemini`), so a stop costs at most the request in flight and a
resume requests only the chunks that have no saved part. A spent daily
allowance becomes `QuotaStop`, which the runner parks as waiting_quota.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from lore.config import LoreConfig
from lore.ingest.registry import chunks_for_book
from lore.orchestrator.local import make_gemini_tier, parts_done
from lore.orchestrator.pipeline import JobCtl, QuotaStop, StageContext
from lore.orchestrator.run import RunReport, _run_unit_here, _UnitHalted, unit_state
from lore.orchestrator.workunit import WorkUnit, pack_units


class ExtractStage:
    name = "extract"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        gemini_tier_factory: Callable[[LoreConfig], object] | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self._api_key = api_key
        self._factory = gemini_tier_factory
        self._on_event = on_event
        # The RunReport-like summary `lore process` prints; set by run().
        self.report: RunReport | None = None

    def _units(self, ctx: StageContext) -> list[WorkUnit]:
        chunks = chunks_for_book(ctx.conn, ctx.book_id)
        if not chunks:
            raise ValueError(f"no chunks registered for book {ctx.book_id}")
        return pack_units(ctx.book_id, chunks, ctx.config.extraction.work_unit_size)

    def total(self, ctx: StageContext) -> int:
        return len(chunks_for_book(ctx.conn, ctx.book_id))

    def run(self, ctx: StageContext, job_ctl: JobCtl) -> None:
        config = ctx.config
        if config.extraction_provider != "gemini":
            raise ValueError(
                'resumable processing needs extraction_provider = "gemini"')
        units = self._units(ctx)
        completed = {u.unit_id for u in units
                     if unit_state(ctx.conn, u.unit_id) == "completed"}
        report = self.report = RunReport(
            ctx.book_id, units_total=len(units), units_completed=len(completed))
        base = sum(len(u.chunks) for u in units if u.unit_id in completed)
        pending = [u for u in units if u.unit_id not in completed]
        # The runner starts the job at done=0, even on a resume.
        dests = {u.unit_id: config.extractions_dir / u.book_id / u.unit_id
                 for u in pending}
        done_now = base + sum(parts_done(dests[u.unit_id], u) for u in pending)
        job_ctl.advance(done_now)
        if not pending:
            return
        make = self._factory or (lambda cfg: make_gemini_tier(cfg, self._api_key))
        tier = make(config)
        with asyncio.Runner() as loop:
            try:
                for unit in pending:
                    dest = dests[unit.unit_id]
                    others = done_now - parts_done(dest, unit)

                    def on_chunk(n: int, others: int = others) -> None:
                        job_ctl.advance(others + n)

                    try:
                        ok, _, n_ok, n_dead = _run_unit_here(
                            ctx.conn, unit,
                            lambda unit=unit, dest=dest, on_chunk=on_chunk: loop.run(
                                tier.run(unit, dest,
                                         before_request=job_ctl.spend_check,
                                         on_chunk=on_chunk)),
                            self._on_event,
                        )
                    except _UnitHalted as halt:
                        report.stopped_reason = str(halt)
                        if halt.quota:
                            raise QuotaStop(str(halt)) from halt
                        raise RuntimeError(str(halt)) from halt
                    if not ok:
                        report.units_failed += 1
                        raise RuntimeError(
                            f"unit {unit.unit_id} failed; see the jobs table")
                    report.units_completed += 1
                    report.chunks_ok += n_ok
                    report.chunks_dead += n_dead
                    done_now = others + len(unit.chunks)
                    job_ctl.advance(done_now)
            finally:
                loop.run(tier.aclose())
