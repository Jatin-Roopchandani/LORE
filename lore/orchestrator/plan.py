"""The step list a processing book shows: every stage it will run, in the
pipeline's own order, with a time estimate for each and for the whole.

ONE SOURCE OF TRUTH. The steps are the names of the stages the runner would
run for this book (`make_stages(config)`, or the move's stages), in that
order, minus the ones that do no work for this book or config: a stage may
say so with `shown(config) -> bool` (the chunk stage, done at upload; the
disambiguation, beats and codex stages when switched off). The web app only
labels them (messages.ts STAGE_LABEL).

ESTIMATES. Every stage is measured in seconds per chunk of the book: the
chunk count is the one size every book has before processing (entities and
sections only exist after the load), and each stage's work grows with it.

- The current step: from its measured rate (seconds per item since the
  stage started or resumed) once MIN_ITEMS items are done.
- An upcoming step: the user's own average for that stage, provider and
  model (table `stage_timings`, written when a stage completes), else the
  same stage on any provider/model, else DEFAULT_SECONDS_PER_CHUNK, else
  unknown (None: the page says "a few minutes", never invented precision).

Seconds are returned raw; the page rounds them to friendly values.

GEMINI'S FREE TIER. Working time is not the whole story there: the free tier
allows so many requests a day, so a big book stops when they run out and goes
on after the reset (midnight Pacific). With `free_tier` given, the list also
estimates the requests the book still needs (`requests_left`) and, when they
do not fit in what is left today, carries `free_tier`: where today's run out
and when the book should finish. Paid plans and other providers have no daily
limit and pass none.
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from lore.orchestrator import jobs

MIN_ITEMS = 3      # items done in this stage before its own rate is trusted
RECENT = 20        # timings averaged per (stage, provider, model)

# Documented defaults, seconds per chunk, used only before anything was
# measured. Extraction is one request per chunk; Gemini's free tier allows 15
# requests a minute per Lite model and the cascade has two, so at best ~2 s a
# chunk; 4 s allows for the throttle and retries. Nothing else has a
# defensible default: unknown until measured.
DEFAULT_SECONDS_PER_CHUNK: dict[str, float] = {"extract": 4.0}


def stage_names(stages: Iterable, config) -> list[str]:
    """The stages that will do work, in run order."""
    names = []
    for stage in stages:
        shown = getattr(stage, "shown", None)
        if shown is not None and not shown(config):
            continue
        names.append(stage.name)
    return names


def stage_notes(stages: Iterable, config) -> dict[str, str]:
    """Each shown stage's note, when it says it will do no work for a reason
    the reader should know (`skip_note(config)`: no model configured)."""
    notes = {}
    for stage in stages:
        skip_note = getattr(stage, "skip_note", None)
        note = skip_note(config) if skip_note is not None else None
        if note:
            notes[stage.name] = note
    return notes


def _chunks(conn: sqlite3.Connection, book_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM chunks WHERE book_id = ?",
                       (book_id,)).fetchone()
    return int(row[0]) if row else 0


def seconds_per_chunk(conn: sqlite3.Connection, stage: str, provider: str | None,
                      model: str | None) -> float | None:
    """The user's measured average, most specific first; else the default."""
    for where, params in (
            ("stage = ? AND provider IS ? AND model IS ?", (stage, provider, model)),
            ("stage = ? AND provider IS ?", (stage, provider)),
            ("stage = ?", (stage,))):
        row = conn.execute(
            "SELECT SUM(seconds), SUM(chunks) FROM (SELECT seconds, chunks FROM"
            f" stage_timings WHERE {where} ORDER BY id DESC LIMIT {RECENT})",
            params).fetchone()
        if row and row[1]:
            return float(row[0]) / float(row[1])
    return DEFAULT_SECONDS_PER_CHUNK.get(stage)


def record_stage(conn: sqlite3.Connection, book_id: str, stage: str,
                 provider: str | None, model: str | None, now: datetime) -> None:
    """A stage just completed: store its seconds per chunk for later estimates.

    Only the part done in this run is measured (from the stage's first
    progress report to now), scaled to the share of the book it covered.
    Nothing is stored for a stage that did no work.
    """
    row = conn.execute(
        "SELECT stage_started_at, stage_base, done_items, total_items FROM book_jobs"
        " WHERE book_id = ?", (book_id,)).fetchone()
    if row is None or row[0] is None:
        return
    started = datetime.fromisoformat(row[0])
    base, done, total = int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)
    worked = done - base
    chunks = _chunks(conn, book_id)
    if worked <= 0 or total <= 0 or chunks <= 0:
        return
    seconds = (jobs._aware(now) - started).total_seconds()
    covered = chunks * worked / total
    if seconds <= 0 or covered <= 0:
        return
    with conn:
        conn.execute(
            "INSERT INTO stage_timings (stage, provider, model, seconds, chunks)"
            " VALUES (?, ?, ?, ?, ?)", (stage, provider, model, seconds, covered))


def _current_left(conn, book_id: str, job: jobs.BookJob, now: datetime) -> float | None:
    row = conn.execute("SELECT stage_started_at, stage_base FROM book_jobs"
                       " WHERE book_id = ?", (book_id,)).fetchone()
    if row is None or row[0] is None or job.total_items <= 0:
        return None
    worked = job.done_items - int(row[1] or 0)
    if worked < MIN_ITEMS:
        return None
    elapsed = (jobs._aware(now) - datetime.fromisoformat(row[0])).total_seconds()
    if elapsed <= 0:
        return None
    return max(0.0, elapsed / worked * (job.total_items - job.done_items))


# --- requests, for Gemini's free daily limit ------------------------------------------

# Requests per chunk for steps whose own count is unknown until they run. Rough
# guesses (marked `rough`): the load asks its adjudication questions in
# batches, a few per hundred chunks; disambiguation asks in batches of 25.
ROUGH_REQUESTS_PER_CHUNK: dict[str, float] = {"load": 0.02, "disambiguate": 0.02}
# Codex: one request per entity profile, at most CODEX_MAX_PER_BOOK; about one
# entity worth a profile per five chunks until the cap (rough).
CODEX_CHUNKS_PER_ENTITY = 5
# Seconds per request when nothing was measured (as DEFAULT_SECONDS_PER_CHUNK).
DEFAULT_SECONDS_PER_REQUEST = 4.0


@dataclass(frozen=True)
class FreeTier:
    """What Gemini's free tier leaves for processing: requests left today and
    per full day (both less the answer reserve), and the next reset (UTC)."""

    today_left: int
    per_day: int
    reset: datetime


@dataclass(frozen=True)
class RequestsLeft:
    by_stage: dict[str, int]
    extract_done: int
    extract_left: int
    rough: bool

    @property
    def total(self) -> int:
        return sum(self.by_stage.values())


def _beat_requests(conn: sqlite3.Connection, book_id: str) -> int:
    """The beats step's own count: one request per part of a chapter of at most
    MAX_PART_CHARS characters."""
    from lore.beats.build import MAX_PART_CHARS
    return sum(max(1, math.ceil(int(chars or 0) / MAX_PART_CHARS)) for (chars,) in conn.execute(
        "SELECT SUM(LENGTH(text)) FROM chunks WHERE book_id = ? GROUP BY chapter_seq", (book_id,)))


def requests_left(conn: sqlite3.Connection, book_id: str, job: jobs.BookJob,
                  names: list[str], notes: dict[str, str] | None = None) -> RequestsLeft:
    """The model requests the book's remaining steps should make. The current
    step's own counts when it has them (one request per item), else per-chunk
    figures; `rough` when a guess makes up a fair part of the total."""
    from lore.codex.build import CODEX_MAX_PER_BOOK
    notes = notes or {}
    chunks = _chunks(conn, book_id)
    current = job.stage if job.stage in names else jobs_order(names, job.stage)
    upcoming = names[names.index(current):] if current in names else []
    by_stage: dict[str, int] = {}
    guessed = 0
    extract_done, extract_left = 0, 0
    for name in upcoming:
        if name in notes:
            continue  # skipped: asks nothing
        counted = name == job.stage and job.total_items > 1
        if name == "extract":
            extract_done = job.done_items if counted else 0
            extract_left = (job.total_items - job.done_items) if counted else chunks
            by_stage[name] = extract_left
        elif counted and name in ("beats", "codex", "disambiguate"):
            by_stage[name] = max(0, job.total_items - job.done_items)
        elif name == "beats":
            by_stage[name] = _beat_requests(conn, book_id)
        elif name == "codex":
            by_stage[name] = min(CODEX_MAX_PER_BOOK, math.ceil(chunks / CODEX_CHUNKS_PER_ENTITY))
            guessed += by_stage[name]
        elif name in ROUGH_REQUESTS_PER_CHUNK:
            by_stage[name] = math.ceil(chunks * ROUGH_REQUESTS_PER_CHUNK[name])
            guessed += by_stage[name]
    total = sum(by_stage.values())
    return RequestsLeft(by_stage, extract_done, extract_left,
                        rough=total > 0 and guessed / total > 0.1)


def free_tier_finish(need: RequestsLeft, seconds: float | None, tier: FreeTier) -> dict | None:
    """When `need` does not fit in what is left today: where today's requests
    run out (a part number, while extraction is still going), how many resets
    it waits for, and when it should finish (ISO, UTC). None when it fits."""
    total = need.total
    today = max(0, tier.today_left)
    if total <= today or tier.per_day <= 0:
        return None
    per_request = (seconds / total) if seconds and total else DEFAULT_SECONDS_PER_REQUEST
    rest = total - today
    days = math.ceil(rest / tier.per_day)
    last_day = rest - (days - 1) * tier.per_day
    finish = tier.reset + timedelta(days=days - 1, seconds=last_day * per_request)
    runs_out = need.extract_done + today if today < need.extract_left else None
    return {"requests_left": total, "today_left": today, "per_day": tier.per_day,
            "days": days, "finish_at": finish.isoformat(), "runs_out_at_part": runs_out,
            "rough": need.rough}


def steps(conn: sqlite3.Connection, book_id: str, job: jobs.BookJob | None,
          names: list[str], now: datetime, *, provider: str | None = None,
          model: str | None = None, notes: dict[str, str] | None = None,
          free_tier: FreeTier | None = None) -> dict | None:
    """The step list for a book's job: each step's state (done, current,
    upcoming, or skipped: an upcoming step with a note from `notes`, e.g.
    "skipped: no model configured for story beats"), the current one's
    counts, seconds left per step (None: unknown) and in all
    (`seconds_left`: the known part; `unknown_left`: some step has no
    estimate)."""
    notes = notes or {}
    if job is None or job.state == jobs.DONE or not names:
        return None
    current = job.stage if job.stage in names else None
    if current is None:  # a stage not shown (chunk): the next shown one is up
        order = jobs_order(names, job.stage)
        current = order
    chunks = _chunks(conn, book_id)
    out, seen_current, known, unknown = [], False, 0.0, False
    for name in names:
        if name == current:
            seen_current = True
            left = _current_left(conn, book_id, job, now) if job.stage == name else None
            if left is None:
                rate = seconds_per_chunk(conn, name, provider, model)
                share = 1.0
                if job.stage == name and job.total_items > 0:
                    share = max(0.0, 1 - job.done_items / job.total_items)
                left = rate * chunks * share if rate is not None and chunks else None
            out.append({"stage": name, "state": "current",
                        "done": job.done_items if job.stage == name else 0,
                        "total": job.total_items if job.stage == name else 0,
                        "seconds_left": left})
        elif not seen_current:
            out.append({"stage": name, "state": "done", "seconds_left": 0})
            continue
        elif name in notes:
            out.append({"stage": name, "state": "skipped", "note": notes[name],
                        "seconds_left": 0})
            continue
        else:
            rate = seconds_per_chunk(conn, name, provider, model)
            left = rate * chunks if rate is not None and chunks else None
            out.append({"stage": name, "state": "upcoming", "seconds_left": left})
        if left is None:
            unknown = True
        else:
            known += left
    index = next((i for i, s in enumerate(out) if s["state"] == "current"), len(out) - 1)
    result = {"steps": out, "step": index + 1, "of": len(out),
              "seconds_left": round(known) if known or not unknown else None,
              "unknown_left": unknown}
    if free_tier is not None:
        late = free_tier_finish(requests_left(conn, book_id, job, names, notes),
                                known or None, free_tier)
        if late is not None:
            result["free_tier"] = late
    return result


def jobs_order(names: list[str], stage: str) -> str | None:
    """The first shown stage at or after `stage` in the pipeline's order."""
    from lore.orchestrator.pipeline import STAGES
    if stage not in STAGES:
        return names[0] if names else None
    at = STAGES.index(stage)
    for name in names:
        if name in STAGES and STAGES.index(name) >= at:
            return name
    return names[-1] if names else None


def names_for(make: Callable[[], list], config) -> list[str]:
    """Stage names from a stage factory; [] when it cannot be built."""
    try:
        return stage_names(make(), config)
    except Exception:  # noqa: BLE001 - no step list rather than no Library
        return []


def notes_for(make: Callable[[], list], config) -> dict[str, str]:
    """Stage notes from a stage factory; {} when it cannot be built."""
    try:
        return stage_notes(make(), config)
    except Exception:  # noqa: BLE001 - no notes rather than no Library
        return {}
