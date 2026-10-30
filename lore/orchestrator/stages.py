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
from dataclasses import dataclass

from lore.beats import build as beats_build
from lore.beats.store import relink as relink_beats, save_beats
from lore.codex import build as codex_build
from lore.codex import store as codex_store
from lore.inference.gemini import (
    GeminiClient,
    GeminiOutputError,
    GeminiQuotaError,
    default_key_file,
    resolve_api_key,
)
from lore.orchestrator import jobs
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


class RemoveStage:
    """Take the book out of the graph and indexes, keeping chunks and caches.

    `remove_fn(config, conn, book_id)` does it and returns its report, kept
    as `self.report`. The default opens the stores and calls
    `remove_book_from_graph`; both stores are closed before the stage ends,
    so the load that follows can open its own. Idempotent, so a stop in
    this stage simply runs it again.
    """

    name = "remove"

    def __init__(self, remove_fn: Callable | None = None) -> None:
        self._remove_fn = remove_fn or _remove_from_stores
        self.report = None

    def total(self, ctx: StageContext) -> int:
        return 1

    def run(self, ctx: StageContext, job_ctl: JobCtl) -> None:
        job_ctl.advance(0)
        self.report = self._remove_fn(ctx.config, ctx.conn, ctx.book_id)
        job_ctl.advance(1)


def _remove_from_stores(config, conn, book_id: str):
    from lore.db.kuzu_store import KuzuStore
    from lore.graph.remove import remove_book_from_graph

    kuzu = KuzuStore(config.kuzu_dir / "graph")
    try:
        return remove_book_from_graph(conn, kuzu, _open_vectors(config), book_id)
    finally:
        kuzu.close()


class ReassignStage:
    """Put the book in another world (the registry row; nothing else).

    Runs after `remove`: an entity must never list books of two worlds, and
    the load that follows writes the book into the new world's graph.
    """

    name = "reassign"

    def __init__(self, world_id: int) -> None:
        self._world_id = world_id

    def total(self, ctx: StageContext) -> int:
        return 1

    def run(self, ctx: StageContext, job_ctl: JobCtl) -> None:
        from lore import worlds

        job_ctl.advance(0)
        if not worlds.assign(ctx.conn, ctx.book_id, self._world_id):
            raise RuntimeError(f"unknown book {ctx.book_id}")
        job_ctl.advance(1)


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
            # Answers saved by an earlier run may never have been applied.
            self._apply(ctx)
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
                    if answers and self._reload is not None:
                        # Durable before anything else can stop the run.
                        jobs.set_needs_reload(ctx.conn, ctx.book_id)
                    got_new = got_new or bool(answers)
                    job_ctl.advance(already + start // MAX_BATCH + 1)
            finally:
                await client.aclose()

        try:
            asyncio.run(_run())
        except GeminiQuotaError as exc:
            raise quota_stop_from(exc) from exc
        if got_new:
            self._apply(ctx)

    def _apply(self, ctx: StageContext) -> None:
        """Reload if answers await it; clear the marker only after success."""
        if self._reload is None or not jobs.needs_reload(ctx.conn, ctx.book_id):
            return
        self._reload(ctx)
        jobs.set_needs_reload(ctx.conn, ctx.book_id, False)


def _gemini_client(config) -> GeminiClient:
    api_key = resolve_api_key(key_file=default_key_file())
    if api_key is None:
        raise RuntimeError(
            "disambiguation needs a Gemini API key (GEMINI_API_KEY or"
            " ~/.config/lore/gemini.env)")
    return GeminiClient(api_key, models=config.disambiguation.models,
                        recorder=recorder_for(config.sqlite_path))


@dataclass(frozen=True)
class _BeatPart:
    chapter: int
    index: int
    count: int
    chunks: tuple
    entities: tuple

    @property
    def label(self) -> str:
        return f"{self.chapter}-{self.index}"


def _beats_client(config) -> GeminiClient:
    api_key = resolve_api_key(key_file=default_key_file())
    if api_key is None:
        raise RuntimeError(
            "story beats need a Gemini API key (GEMINI_API_KEY or"
            " ~/.config/lore/gemini.env)")
    return GeminiClient(api_key, models=config.extraction.gemini_models,
                        recorder=recorder_for(config.sqlite_path))


def _codex_client(config) -> GeminiClient:
    api_key = resolve_api_key(key_file=default_key_file())
    if api_key is None:
        raise RuntimeError(
            "the codex needs a Gemini API key (GEMINI_API_KEY or"
            " ~/.config/lore/gemini.env)")
    return GeminiClient(api_key, models=config.extraction.gemini_models,
                        recorder=recorder_for(config.sqlite_path))


def _local_embedder(config):
    """(embed, aclose) on the local Ollama embedder, as the loader uses."""
    from lore.graph.adjudicate import _embedder
    from lore.inference.ollama import OllamaProvider

    provider = OllamaProvider(config.ollama)
    return _embedder(config, provider), provider.aclose


def _open_graph(config):
    """The graph read-only, or None when the book has not been graphed."""
    from lore.db.kuzu_store import KuzuStore

    path = config.kuzu_dir / "graph"
    return KuzuStore(path, read_only=True) if path.exists() else None


def _open_vectors(config):
    from lore.db.lance_store import LanceStore

    return LanceStore(config.lancedb_dir)


class BeatsStage:
    """Build the book's story beats, one request per chapter part.

    A no-op unless `[lore.beats] enabled`. Items are chapter parts that have
    at least one listed entity. Each part's raw answer is cached on disk
    (`data/beats/<book>/<chapter>-<part>.json`, keyed by prompt and text
    hashes) the moment it arrives, so a stop keeps every answer already
    paid for and a resume requests only the rest. A part is stored (embedded,
    written to LanceDB, then saved to SQLite, which marks it done) from its
    verified beats, and the entity names are relinked to the current ids
    when the run ends, however it ends.

    The factories are the seams for tests: `client_factory(config)` returns a
    GeminiClient-like object (generate_json, aclose); `embed_factory(config)`
    returns (embed, aclose); `kuzu_factory(config)` returns a graph (or None
    for "no graph") with execute and close; `lance_factory(config)` returns
    a store with upsert_beats and delete_beats.
    """

    name = "beats"

    def __init__(self, client_factory: Callable | None = None,
                 embed_factory: Callable | None = None,
                 kuzu_factory: Callable | None = None,
                 lance_factory: Callable | None = None) -> None:
        self._client_factory = client_factory or _beats_client
        self._embed_factory = embed_factory or _local_embedder
        self._kuzu_factory = kuzu_factory or _open_graph
        self._lance_factory = lance_factory or _open_vectors
        self.skipped: list[str] = []

    def _plan(self, ctx: StageContext, kuzu) -> list[_BeatPart]:
        from lore.ingest.registry import chunks_for_book

        if kuzu is None:
            return []
        plan: list[_BeatPart] = []
        chapters = beats_build.part_chapters(chunks_for_book(ctx.conn, ctx.book_id))
        for chapter, chunks in chapters.items():
            entities = beats_build.chapter_entities(kuzu, ctx.book_id, chapter)
            if not entities:
                continue
            parts = beats_build.chapter_parts(chunks, beats_build.MAX_PART_CHARS)
            for index, part in enumerate(parts):
                plan.append(_BeatPart(chapter, index, len(parts), tuple(part),
                                      tuple(entities)))
        return plan

    def _cache(self, ctx: StageContext, part: _BeatPart):
        return beats_build.cache_path(ctx.config.beats_dir, ctx.book_id,
                                      part.chapter, part.index)

    def _plan_for(self, ctx: StageContext) -> list[_BeatPart]:
        if not ctx.config.beats.enabled:
            return []
        kuzu = self._kuzu_factory(ctx.config)
        try:
            return self._plan(ctx, kuzu)
        finally:
            if kuzu is not None:
                kuzu.close()

    def total(self, ctx: StageContext) -> int:
        return len(self._plan_for(ctx))

    def run(self, ctx: StageContext, job_ctl: JobCtl) -> None:
        self.skipped = []
        if not ctx.config.beats.enabled:
            job_ctl.advance(0)
            return
        kuzu = self._kuzu_factory(ctx.config)
        try:
            plan = self._plan(ctx, kuzu)
            cached = {p.label: beats_build.read_cached(self._cache(ctx, p), p.chunks)
                      for p in plan}
            done = sum(a is not None for a in cached.values())
            job_ctl.advance(done)
            if not plan:
                return
            try:
                asyncio.run(self._run(ctx, job_ctl, plan, cached, done))
            except GeminiQuotaError as exc:
                raise quota_stop_from(exc) from exc
        finally:
            try:
                if kuzu is not None:
                    relink_beats(ctx.conn, kuzu)
            finally:
                if kuzu is not None:
                    kuzu.close()

    async def _run(self, ctx, job_ctl, plan, cached, done: int) -> None:
        title = ctx.conn.execute("SELECT title FROM books WHERE id = ?",
                                 (ctx.book_id,)).fetchone()[0]
        client = embed = embed_close = lance = None
        try:
            # One loop for the requests, the embeds and the closes: httpx
            # binds its sockets to the loop that opened them.
            for part in plan:
                answer = cached[part.label]
                fresh = answer is None
                if fresh:
                    job_ctl.spend_check()
                    if client is None:
                        client = self._client_factory(ctx.config)
                    prompt = beats_build.render_prompt(
                        title, part.chapter, part.index, part.count,
                        part.entities, part.chunks)
                    try:
                        answer = await client.generate_json(
                            prompt, beats_build.BeatsAnswer,
                            exhausted_hint="Continue the job when a daily"
                                           " budget resets.")
                    except GeminiOutputError:
                        # Not cached: the next run asks this part again.
                        self.skipped.append(f"{part.label}: unusable answer")
                        done += 1
                        job_ctl.advance(done)
                        continue
                    beats_build.write_cached(self._cache(ctx, part), part.chunks,
                                             answer)
                beats = beats_build.verify_beats(
                    answer, ctx.book_id, part.chapter, part.chunks, part.entities)
                existing = {r[0] for r in ctx.conn.execute(
                    "SELECT id FROM beats WHERE book_id = ? AND chapter_seq = ?"
                    " AND part = ?", (ctx.book_id, part.chapter, part.index))}
                ids = {b.id for b in beats}
                if beats and (fresh or ids != existing):
                    if embed is None:
                        embed, embed_close = self._embed_factory(ctx.config)
                        lance = self._lance_factory(ctx.config)
                    vectors = await embed([b.text for b in beats])
                    lance.upsert_beats([
                        {"id": b.id, "book_id": ctx.book_id, "vector": v}
                        for b, v in zip(beats, vectors)])
                if existing - ids:
                    if lance is None:
                        lance = self._lance_factory(ctx.config)
                    lance.delete_beats(sorted(existing - ids))
                # SQLite last: a part is done once its rows are saved.
                save_beats(ctx.conn, ctx.book_id, part.chapter, part.index, beats)
                if fresh:
                    done += 1
                    job_ctl.advance(done)
        finally:
            try:
                if client is not None:
                    await client.aclose()
            finally:
                if embed_close is not None:
                    await embed_close()


@dataclass(frozen=True)
class _CodexItem:
    world_id: int
    entity: codex_build.CodexEntity
    evidence: codex_build.Evidence
    evidence_hash: str


class _DeferredLance:
    """A Lance store opened only if something is deleted from it."""

    def __init__(self, factory: Callable) -> None:
        self._factory = factory

    def delete_codex(self, ids: list[str]) -> None:
        self._factory().delete_codex(ids)


class CodexStage:
    """Build the codex of the book's world: one profile per significant
    entity, one request each.

    A no-op unless `[lore.codex] enabled`. Items are the world's significant
    entities (kinds Concept, Faction, Place, Object, Character, mentioned by
    enough chunks, at most CODEX_MAX_PER_BOOK per book of the world). Each
    entity's raw answer is cached on disk (`data/codex/<entity>-<evidence
    hash>.json`, also keyed by the prompt hash) the moment it arrives, so a
    stop keeps every answer already paid for and a resume requests only the
    rest; an entity whose evidence is unchanged since its entry was saved
    costs nothing. An entry is stored (embedded, written to LanceDB, then
    saved to SQLite, which marks it done) from its verified statements, and
    entity links are relinked to the current ids when the run ends, however it
    ends.

    `all_worlds` also visits every other world that already has a codex, so a
    move, which changes the evidence of the world the book left, rebuilds
    that world's dropped entries too (from the cache when their evidence is
    back to what it was).

    The factories are the seams for tests, as for BeatsStage: `client_factory`
    (generate_json, aclose), `embed_factory` (embed, aclose), `kuzu_factory`
    (execute, close; None for "no graph") and `lance_factory` (upsert_codex,
    delete_codex).
    """

    name = "codex"

    def __init__(self, client_factory: Callable | None = None,
                 embed_factory: Callable | None = None,
                 kuzu_factory: Callable | None = None,
                 lance_factory: Callable | None = None,
                 all_worlds: bool = False) -> None:
        self._client_factory = client_factory or _codex_client
        self._embed_factory = embed_factory or _local_embedder
        self._kuzu_factory = kuzu_factory or _open_graph
        self._lance_factory = lance_factory or _open_vectors
        self._all_worlds = all_worlds
        self.skipped: list[str] = []

    def _worlds(self, ctx: StageContext) -> list[int]:
        from lore import worlds

        own = worlds.world_of(ctx.conn, ctx.book_id)
        ids = [own.id] if own is not None else []
        if self._all_worlds:
            ids += [w for w in codex_store.world_ids_with_codex(ctx.conn)
                    if w not in ids]
        return ids

    def _plan(self, ctx: StageContext, kuzu) -> list[_CodexItem]:
        from lore import worlds
        from lore.ingest.registry import book_rows

        if kuzu is None:
            return []
        books = book_rows(ctx.conn)
        plan: list[_CodexItem] = []
        for world_id in self._worlds(ctx):
            book_ids = worlds.books_in(ctx.conn, world_id)
            entities = codex_build.significant_entities(
                kuzu, book_ids, codex_build.CODEX_MAX_PER_BOOK * len(book_ids))
            for entity in entities:
                evidence = codex_build.evidence_pack(
                    ctx.conn, kuzu, entity, book_ids, books)
                if not evidence.passages:
                    continue
                plan.append(_CodexItem(
                    world_id, entity, evidence,
                    codex_build.evidence_hash(entity, evidence)))
        return plan

    def _plan_for(self, ctx: StageContext) -> list[_CodexItem]:
        if not ctx.config.codex.enabled:
            return []
        kuzu = self._kuzu_factory(ctx.config)
        try:
            return self._plan(ctx, kuzu)
        finally:
            if kuzu is not None:
                kuzu.close()

    def total(self, ctx: StageContext) -> int:
        return len(self._plan_for(ctx))

    @staticmethod
    def _saved_current(ctx: StageContext, item: _CodexItem) -> bool:
        saved = codex_store.summary_for(ctx.conn, item.entity.id)
        return (saved is not None
                and saved.prompt_hash == codex_build.prompt_hash()
                and saved.evidence_hash == item.evidence_hash
                and saved.world_id == item.world_id)

    @staticmethod
    def _cache(ctx: StageContext, item: _CodexItem):
        return codex_build.cache_path(ctx.config.codex_dir, item.entity.id,
                                      item.evidence_hash)

    def run(self, ctx: StageContext, job_ctl: JobCtl) -> None:
        self.skipped = []
        if not ctx.config.codex.enabled:
            job_ctl.advance(0)
            return
        kuzu = self._kuzu_factory(ctx.config)
        try:
            plan = self._plan(ctx, kuzu)
            cached = {i.entity.id: codex_build.read_cached(
                self._cache(ctx, i), i.evidence_hash) for i in plan}
            current = {i.entity.id for i in plan if self._saved_current(ctx, i)}
            done = sum(cached[i.entity.id] is not None or i.entity.id in current
                       for i in plan)
            job_ctl.advance(done)
            if not plan:
                return
            try:
                asyncio.run(self._run(ctx, job_ctl, plan, cached, current, done))
            except GeminiQuotaError as exc:
                raise quota_stop_from(exc) from exc
        finally:
            try:
                if kuzu is not None:
                    codex_store.relink(ctx.conn, kuzu,
                                       _DeferredLance(self._lance_factory))
            finally:
                if kuzu is not None:
                    kuzu.close()

    async def _run(self, ctx, job_ctl, plan, cached, current, done: int) -> None:
        client = embed = embed_close = lance = None
        try:
            # One loop for the requests, the embeds and the closes: httpx
            # binds its sockets to the loop that opened them.
            for item in plan:
                entity = item.entity
                if entity.id in current:
                    continue
                answer = cached[entity.id]
                fresh = answer is None
                if fresh:
                    job_ctl.spend_check()
                    if client is None:
                        client = self._client_factory(ctx.config)
                    try:
                        answer = await client.generate_json(
                            codex_build.render_prompt(entity, item.evidence),
                            codex_build.CodexAnswer,
                            exhausted_hint="Continue the job when a daily"
                                           " budget resets.")
                    except GeminiOutputError:
                        # Not cached: the next run asks this entity again.
                        self.skipped.append(f"{entity.name}: unusable answer")
                        done += 1
                        job_ctl.advance(done)
                        continue
                    codex_build.write_cached(self._cache(ctx, item),
                                             item.evidence_hash, answer)
                entry = codex_build.verify_entry(
                    answer, entity, item.world_id, item.evidence, item.evidence_hash)
                old = codex_store.statement_ids(ctx.conn, [entity.id])
                if lance is None and (entry is not None or old):
                    lance = self._lance_factory(ctx.config)
                new: list[str] = []
                if entry is not None:
                    if embed is None:
                        embed, embed_close = self._embed_factory(ctx.config)
                    new = [codex_store.statement_id(entity.id, i, s.text)
                           for i, s in enumerate(entry.statements)]
                    vectors = await embed(
                        [f"{entity.name} ({s.aspect}): {s.text}"
                         for s in entry.statements])
                    lance.upsert_codex([{"id": i, "vector": v}
                                        for i, v in zip(new, vectors)])
                if set(old) - set(new):
                    lance.delete_codex(sorted(set(old) - set(new)))
                # SQLite last: an entity is done once its rows are saved.
                if entry is not None:
                    codex_store.save_entry(ctx.conn, entry)
                elif old:
                    codex_store.delete_entities(ctx.conn, [entity.id])
                if fresh:
                    done += 1
                    job_ctl.advance(done)
        finally:
            try:
                if client is not None:
                    await client.aclose()
            finally:
                if embed_close is not None:
                    await embed_close()
