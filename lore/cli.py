"""LORE CLI."""
from __future__ import annotations

import asyncio
import math
import sqlite3
import tempfile
from pathlib import Path

import httpx
import typer

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.db.sqlite import open_db, schema_version
from lore.graph.adjudicate import (
    make_gemini_first_person,
    make_gemini_pair,
    make_ollama_pair,
)
from lore.graph.loader import PartialLoadError, load_book
from lore.ingest.chunker import chunk_book
from lore.ingest.epub import parse_epub
from lore.ingest.models import IngestError, book_id_for_file
from lore.ingest.pdf import parse_pdf
from lore.ingest.registry import find_books, forget_book, ingest_book, list_books, set_series
from lore.inference.base import ProviderOutputError
from lore.inference.gemini import (
    GeminiClient,
    GeminiError,
    GeminiKeyFileError,
    default_key_file,
    limits_for,
    resolve_api_key,
)
from lore.inference.ollama import OllamaProvider
from lore.kaggle_api.client import KaggleClient
from lore.kaggle_api.hello import run_hello_roundtrip
from lore.orchestrator.gemini_budget import day_report, recorder_for, today
from lore.orchestrator.local import missing_gemini_key_message
from lore.orchestrator.quota import iso_week, week_report
from lore.orchestrator.run import run_extraction
from lore.query.agent import answer_question, make_query_models
from lore.resolve.disambiguation import (
    MAX_BATCH,
    AnswerMismatchError,
    DisambiguationStore,
)
# Aliased: `ask` is also this CLI's query command below, and the later def
# would silently shadow the import and break `lore disambiguate`.
from lore.resolve.disambiguation import ask as ask_disambiguation

app = typer.Typer(no_args_is_help=True)


def _report(name: str, ok: bool, detail: str = "") -> bool:
    tag = "[ ok ]" if ok else "[fail]"
    typer.echo(f"{tag} {name}" + (f" — {detail}" if detail else ""))
    return ok


@app.callback()
def main() -> None:
    """LORE - Local Orchestration & Relational Extraction."""


@app.command()
def doctor(kaggle: bool = typer.Option(False, "--kaggle",
                                       help="also run the free-tier GPU round trip (slow)")) -> None:
    """Verify every Phase 0 subsystem is alive."""
    ok = True

    try:
        config = LoreConfig.load()
        config.ensure_dirs()
        ok &= _report("config", True, f"data_dir={config.data_dir}")
    except Exception as exc:
        _report("config", False, str(exc))
        raise typer.Exit(1)

    try:
        conn = open_db(config.sqlite_path)
        ok &= _report("sqlite", True, f"schema v{schema_version(conn)}")
        conn.close()
    except Exception as exc:
        ok &= _report("sqlite", False, str(exc))

    try:
        store = KuzuStore(config.kuzu_dir / "graph")
        ok &= _report("kuzu", True, f"{len(store.table_names())} tables")
        store.close()
    except Exception as exc:
        ok &= _report("kuzu", False, str(exc))

    try:
        lance = LanceStore(config.lancedb_dir)
        ok &= _report("lancedb", True, f"tables={sorted(lance.table_names())}")
    except Exception as exc:
        ok &= _report("lancedb", False, str(exc))

    async def _check_ollama() -> bool:
        # healthy() and aclose() must share one event loop: the client's
        # connections belong to the loop that opened them.
        provider = OllamaProvider(config.ollama)
        try:
            return await provider.healthy()
        finally:
            await provider.aclose()

    try:
        healthy = asyncio.run(_check_ollama())
        ok &= _report("ollama", healthy,
                      config.ollama.model if healthy else f"no server at {config.ollama.host}")
    except Exception as exc:
        ok &= _report("ollama", False, str(exc))

    client = KaggleClient(config.kaggle)
    ok &= _report("kaggle-auth", client.authenticated(),
                  "" if client.authenticated()
                  else "no ~/.kaggle/kaggle.json, ~/.kaggle/access_token, or KAGGLE_API_TOKEN")

    if kaggle:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = run_hello_roundtrip(
                    config.kaggle, Path(tmp),
                    on_tick=lambda s: typer.echo(f"       kaggle kernel: {s}"),
                )
            ok &= _report("kaggle-roundtrip", True,
                          f"sum={result['sum']} on {result['device_name']}")
        except Exception as exc:
            ok &= _report("kaggle-roundtrip", False, str(exc))

    raise typer.Exit(0 if ok else 1)


@app.command()
def ingest(
    path: Path,
    force: bool = typer.Option(
        False,
        "--force",
        help="drop the book's existing rows first and re-ingest it",
    ),
) -> None:
    """Parse, chunk, and register an EPUB or PDF."""
    config = LoreConfig.load()
    config.ensure_dirs()
    parsers = {".epub": parse_epub, ".pdf": parse_pdf}
    parser = parsers.get(path.suffix.lower())
    if parser is None:
        typer.echo(f"unsupported file type: {path.suffix} (need .epub or .pdf)")
        raise typer.Exit(2)
    try:
        book_id = book_id_for_file(path)
        parsed = parser(path)
        chunks = chunk_book(parsed, book_id)
    except IngestError as exc:
        typer.echo(f"ingest failed: {exc}")
        raise typer.Exit(1)
    conn = open_db(config.sqlite_path)
    try:
        # Drop first, then re-insert, so a re-chunk lands cleanly instead
        # of colliding with the old content-addressed chunk IDs.
        forgotten = forget_book(conn, book_id) if force else False
        created = ingest_book(
            conn, book_id=book_id, title=parsed.title, author=parsed.author,
            source="upload", file_path=str(path), chunks=chunks,
            series=parsed.series, series_index=parsed.series_index,
        )
    finally:
        conn.close()
    if forgotten:
        typer.echo(f"cleared previous ingest of {book_id}")
    if not created:
        typer.echo(f"{parsed.title} ({book_id}) already ingested — no-op")
        return
    typer.echo(
        f"{parsed.title} ({book_id}): "
        f"{len(parsed.chapters)} chapters, {len(chunks)} chunks"
    )


@app.command()
def books() -> None:
    """List the ingested library."""
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        rows = list_books(conn)
    finally:
        conn.close()
    if not rows:
        typer.echo("library is empty")
        return
    for row in rows:
        where = (f"  ({row['series']} #{row['series_index']:g})"
                 if row["series"] and row["series_index"] is not None else
                 f"  ({row['series']})" if row["series"] else "")
        typer.echo(
            f"{row['id']}  {row['title']}  [{row['status']}]  "
            f"{row['chunk_count']} chunks{where}"
        )


book_app = typer.Typer(help="Per-book settings.", no_args_is_help=True)
app.add_typer(book_app, name="book")


@book_app.command("series")
def book_series(
    book: str = typer.Argument(..., help="book id, or its title (or part of it)"),
    series: str | None = typer.Option(None, "--series", help="the series' name"),
    index: float | None = typer.Option(
        None, "--index", help="the book's place in the series: 1, 2, 2.5 ..."),
    clear: bool = typer.Option(False, "--clear", help="forget the book's series"),
) -> None:
    """Set or clear a book's place in a series.

    Answers put facts from books of one series in story order. Books are
    ordered against each other only when both have the same series and an
    index; otherwise their order is treated as unknown.
    """
    if clear == bool(series or index is not None):
        typer.echo("give --series NAME with --index N, or --clear")
        raise typer.Exit(2)
    if not clear and (not (series or "").strip() or index is None):
        typer.echo("--series and --index go together")
        raise typer.Exit(2)
    if index is not None and not math.isfinite(index):
        typer.echo("--index must be a finite number")
        raise typer.Exit(2)
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        matches = find_books(conn, book)
        if not matches:
            typer.echo(f"no book matches {book!r} (see: lore books)")
            raise typer.Exit(1)
        if len(matches) > 1:
            typer.echo(f"more than one book matches {book!r}; use its id:")
            for m in matches:
                typer.echo(f"  {m['id']}  {m['title']}")
            raise typer.Exit(1)
        target = matches[0]
        set_series(conn, target["id"], None if clear else series.strip(),
                   None if clear else index)
    finally:
        conn.close()
    typer.echo(f"{target['title']}: series cleared" if clear
               else f"{target['title']}: {series.strip()} #{index:g}")


def _graph_build_key(
    config: LoreConfig, command: str, *, extracting: bool, loading: bool
) -> str | None:
    """The Gemini key the graph build needs, or None if no step is on Gemini.

    Checked before any unit is dispatched or any store is opened. Never
    falls back to a local model: the user chose Gemini, and a silent
    downgrade would hide the quality drop.
    """
    settings: list[str] = []
    alternatives: list[str] = []
    if extracting and config.extraction_provider == "gemini":
        settings.append('extraction_provider = "gemini"')
        alternatives.append('extraction_provider = "kaggle" or "ollama"')
    if loading and config.adjudication.provider == "gemini":
        settings.append('[lore.adjudication] provider = "gemini"')
        alternatives.append('[lore.adjudication] provider = "ollama"')
    if not settings:
        return None
    try:
        api_key = resolve_api_key(key_file=default_key_file())
    except GeminiKeyFileError as exc:
        typer.echo(f"{command} failed: {exc}")
        raise typer.Exit(2)
    if api_key is None:
        typer.echo(missing_gemini_key_message(
            settings, "set " + ", or ".join(alternatives) + " to run without Gemini"
        ))
        raise typer.Exit(2)
    return api_key


@app.command()
def extract(
    book_id: str,
    resume: bool = typer.Option(False, "--resume", help="skip completed work units"),
) -> None:
    """Extract entities and relationships for an ingested book."""
    config = LoreConfig.load()
    api_key = _graph_build_key(config, "extract", extracting=True, loading=False)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        report = run_extraction(
            config, conn, book_id, resume=resume,
            on_event=lambda message: typer.echo(f"       {message}"),
            api_key=api_key,
        )
    except ValueError as exc:
        typer.echo(f"extract failed: {exc}")
        raise typer.Exit(1)
    finally:
        conn.close()
    summary = (
        f"{report.book_id}: {report.units_completed}/{report.units_total} units, "
        f"{report.chunks_ok} chunks extracted, {report.chunks_dead} dead-lettered"
    )
    if config.extraction_provider == "kaggle":
        summary += f", {report.gpu_hours:.2f} GPU-hours"
    typer.echo(summary)
    if report.stopped_reason:
        typer.echo(f"stopped early — {report.stopped_reason}")
        if report.units_completed < report.units_total:
            raise typer.Exit(1)
    if report.units_failed:
        typer.echo(f"{report.units_failed} unit(s) failed — rerun with --resume")
        raise typer.Exit(1)


@app.command()
def quota() -> None:
    """Show this week's Kaggle GPU-hour budget and today's Gemini requests."""
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        report = week_report(conn, iso_week())
        day = today()
        gemini = day_report(conn, day)
    finally:
        conn.close()
    typer.echo(
        f"{report['week']}: {report['used_hours']:.2f}h used, "
        f"{report['remaining_hours']:.2f}h remaining across {report['runs']} run(s)"
    )
    if not gemini:
        typer.echo(f"{day}: no Gemini requests recorded")
        return
    typer.echo(f"{day}: Gemini requests")
    for row in gemini:
        typer.echo(
            f"  {row['model']}: {row['count']} of {row['limit']} used,"
            f" {row['remaining']} remaining"
        )


def _load_book_sync(config: LoreConfig, conn, book_id: str, api_key: str | None = None):
    """Own the event loop and the store handles for one load."""
    # The pair is built first: a missing key refuses before a store opens.
    # Gemini answers are cached in data/adjudication/, so a reload asks only
    # questions it has never had answered.
    # Task 10c: first-person speaker resolution follows the same provider.
    # On Ollama it is skipped (and `process` says so): a wrong speaker is a
    # false fact in the graph, and the local 3B's record on judgement
    # questions here is the reason adjudication moved to Gemini.
    first_person = None
    close_first_person = None
    if config.adjudication.provider == "gemini":
        embed, adjudicate, aclose = make_gemini_pair(config, api_key)
        try:
            first_person, close_first_person = make_gemini_first_person(config, api_key)
        except BaseException:
            asyncio.run(aclose())
            raise
    else:
        embed, adjudicate, aclose = make_ollama_pair(config)
    kuzu = KuzuStore(config.kuzu_dir / "graph")
    lance = LanceStore(config.lancedb_dir)
    # Opt-in, default-off, and offline either way: the store only records
    # the merges the resolver refused and reads answers a previous `lore
    # disambiguate` already paid for. No API call happens in a load.
    disambiguation = (
        DisambiguationStore(config.disambiguation_dir)
        if config.disambiguation.enabled else None
    )

    async def _run():
        # The close MUST share the loop the requests ran on: httpx binds its
        # sockets to the running loop, and closing them from a second
        # asyncio.run() raises "Event loop is closed" *after* a successful
        # load — turning a loaded graph into a spurious `process failed`.
        try:
            return await load_book(
                config, conn, kuzu, lance, book_id, embed, adjudicate,
                disambiguation=disambiguation, first_person=first_person,
            )
        finally:
            try:
                if close_first_person is not None:
                    await close_first_person()
            finally:
                await aclose()

    try:
        return asyncio.run(_run())
    finally:
        kuzu.close()


@app.command()
def process(
    book_id: str,
    resume: bool = typer.Option(False, "--resume", help="skip completed work units"),
    skip_extract: bool = typer.Option(
        False, "--skip-extract", help="reload the graph from existing artifacts"
    ),
) -> None:
    """Extract, resolve entities, and load the graph for a book.

    Note: even with --skip-extract, loading re-runs the resolver's
    adjudication against whatever the graph already holds, so a reload
    still does real work — it is not instantaneous. On the local model that
    is real LLM work; on Gemini ([lore.adjudication] provider = "gemini")
    answered questions come from data/adjudication/answers.json and only
    new ones spend requests.
    """
    config = LoreConfig.load()
    api_key = _graph_build_key(
        config, "process", extracting=not skip_extract, loading=True
    )
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    extraction = None
    try:
        if not skip_extract:
            extraction = run_extraction(
                config, conn, book_id, resume=resume,
                on_event=lambda message: typer.echo(f"       {message}"),
                api_key=api_key,
            )
        # A Gemini run stops when no model can serve (quota, key, network).
        # Loading the half-extracted book now would spend adjudication
        # requests on questions the finished book will ask differently, so
        # the load waits for --resume.
        halted = (extraction is not None and config.extraction_provider == "gemini"
                  and bool(extraction.stopped_reason))
        if not halted:
            report = _load_book_sync(config, conn, book_id, api_key=api_key)
    except PartialLoadError as exc:
        # The graph committed but the vector write did not: the book is
        # genuinely half-loaded. Never report success or mark it ready —
        # a plain re-run repairs it because the load is idempotent.
        typer.echo(f"process failed: {exc}")
        typer.echo(
            f"book {book_id} is NOT fully processed — re-run "
            f"`lore process {book_id} --skip-extract` to repair it"
        )
        raise typer.Exit(1)
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"process failed: {exc}")
        raise typer.Exit(1)
    finally:
        conn.close()
    if halted:
        typer.echo(f"extraction stopped early — {extraction.stopped_reason}")
        typer.echo(
            f"extraction: {extraction.units_completed}/{extraction.units_total}"
            f" units run; the graph was NOT loaded. Rerun `lore process"
            f" {book_id} --resume` to finish it."
        )
        raise typer.Exit(1)
    typer.echo(
        # "canonical" because each merged alias is ALSO persisted as its own
        # node, linked by ALIAS_OF: `lore graph` counts those too, so the
        # two commands legitimately report different Entity totals.
        f"{report.book_id}: {report.entities} canonical entities "
        f"({report.alias_links} aliases merged), "
        f"{report.rel_edges} relationships, {report.chunks} chunks indexed"
    )
    if report.dropped_edges:
        typer.echo(
            f"{report.dropped_edges} edge(s) dropped — an endpoint was named"
            " nowhere in the book"
        )
    if report.self_loop_edges:
        typer.echo(
            f"{report.self_loop_edges} self-loop edge(s) dropped — subject and"
            " object are the same entity"
        )
    if report.first_person_checked:
        typer.echo(
            f"{report.first_person_rewrites} first-person endpoint(s) rewritten to"
            f" their speaker, {report.first_person_unresolved} left unresolved and"
            f" dropped ({report.speaker_requests} speaker request(s);"
            f" narrator hint: {report.narrator or 'none'})"
        )
        if report.speaker_mismatches:
            typer.echo(
                f"{report.speaker_mismatches} passage(s) got a misaligned speaker"
                " answer; left unresolved and not cached"
            )
    elif config.adjudication.provider == "ollama":
        typer.echo(
            "first-person speaker resolution skipped — it runs only with"
            ' [lore.adjudication] provider = "gemini"'
        )
    if report.dropped_mentions:
        typer.echo(
            f"{report.dropped_mentions} mention(s) dropped — not a probable"
            " entity (a definite description or unnamed fragment)"
        )
    if report.boilerplate_chunks:
        typer.echo(
            f"{report.boilerplate_chunks} chunk(s) skipped — Gutenberg"
            " header or licence boilerplate, not story text"
        )
    if extraction is not None:
        # Always, the way `extract` does. "N chunks indexed" above is the
        # book's full chunk count (every chunk is embedded regardless of
        # extraction outcome), so it is no signal at all about how much of
        # the book actually made it into the graph. These two are.
        typer.echo(
            f"extraction: {extraction.units_completed}/{extraction.units_total}"
            f" units run, {extraction.chunks_ok} chunks extracted,"
            f" {extraction.chunks_dead} dead-lettered"
        )
    if extraction is not None and (extraction.units_failed or extraction.stopped_reason
                                   or extraction.chunks_dead):
        # The load above only ever reads completed units, so it is correct
        # and worth keeping — but the graph it produced is partial, and a
        # bare success line here would tell the user their book is in the
        # graph when it is not. Say so, and never exit 0 for this.
        #
        # `chunks_dead` is the third signal and the easiest to miss: a unit
        # whose every chunk dead-lettered still COMPLETES, so it shows up
        # in neither `units_failed` nor `stopped_reason`. Note the residual
        # — `chunks_dead` only counts units run by THIS invocation, so a
        # `--resume` that re-runs nothing reports clean even if a previous
        # run dead-lettered chunks. `units_completed < units_total` is not
        # the fix for that: it false-fires on exactly that legitimate
        # resume, where 0/3 units pending is a complete success.
        reason = (
            extraction.stopped_reason
            or (f"{extraction.units_failed} unit(s) failed"
                if extraction.units_failed else "")
            or f"{extraction.chunks_dead} chunk(s) dead-lettered"
        )
        typer.echo(
            f"extraction incomplete ({reason}): the graph above reflects"
            f" only what was successfully extracted. Rerun `lore process"
            f" {book_id} --resume` to finish it."
        )
        raise typer.Exit(1)

    # Success path only: every failure above has already exited non-zero.
    # Spec 5. `ask` does not gate on this (it asks the graph itself), so a
    # book loaded before this line existed stays queryable without a backfill.
    conn = open_db(config.sqlite_path)
    try:
        conn.execute("UPDATE books SET status = 'graphed' WHERE id = ?", (book_id,))
        conn.commit()
    finally:
        conn.close()


@app.command()
def disambiguate(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="show the pending questions and spend nothing"
    ),
) -> None:
    """Answer the ambiguous merges a load refused — one batched API call.

    This is the ONLY command that spends the Gemini free tier, and that
    tier is counted per DAY: 500 requests on the Lite models, 20 on
    Flash. Every pending question goes in one request and every answer is
    cached, so a whole corpus costs ONE request on first use and ZERO on
    every reload. Deliberately separate from `lore process`, which only
    records these questions.
    """
    config = LoreConfig.load()
    config.ensure_dirs()
    store = DisambiguationStore(config.disambiguation_dir)

    if not config.disambiguation.enabled:
        typer.echo(
            "note: disambiguation is off — set `enabled = true` under"
            " [lore.disambiguation] in lore.toml so `lore process` records"
            " questions and applies these answers"
        )

    pending = store.pending()
    todo = store.unanswered(pending)
    if not todo:
        typer.echo(
            f"nothing to ask — {len(pending)} pending question(s),"
            f" {len(pending)} already answered (0 requests spent)"
        )
        return

    if dry_run:
        batches = -(-len(todo) // MAX_BATCH)
        typer.echo(
            f"would ask {len(todo)} question(s) in {batches} request(s)"
            " (0 requests spent):"
        )
        for index, question in enumerate(todo, start=1):
            options = " | ".join(c.name for c in question.candidates)
            typer.echo(f"  {index}. {question.mention} ({question.kind})"
                       f" -> {options} | none")
            typer.echo(f"     {question.evidence}")
        return

    try:
        api_key = resolve_api_key(key_file=default_key_file())
    except GeminiKeyFileError as exc:
        typer.echo(f"disambiguate failed: {exc}")
        typer.echo(f"{len(todo)} question(s) still pending; 0 requests spent.")
        raise typer.Exit(2)
    if api_key is None:
        typer.echo(
            "no API key: set GEMINI_API_KEY (or GOOGLE_API_KEY) in your"
            " environment, or put one line `GEMINI_API_KEY=...` in"
            " ~/.config/lore/gemini.env with mode 600. A free key comes from"
            " https://aistudio.google.com/apikey — it is never read from"
            " lore.toml, so it cannot end up in git."
        )
        typer.echo(
            f"{len(todo)} question(s) still pending; 0 requests spent."
        )
        raise typer.Exit(2)

    batches = -(-len(todo) // MAX_BATCH)
    # A cascade, not one model: a 429/503/404 moves to the next entry
    # instead of sleeping, which is what turns five per-model daily quotas
    # into aggregate capacity.
    client = GeminiClient(api_key, models=config.disambiguation.models,
                          recorder=recorder_for(config.sqlite_path))

    async def _run():
        # Same loop for the request and the close: httpx binds its sockets
        # to the loop that opened them.
        try:
            return await ask_disambiguation(client, todo)
        finally:
            await client.aclose()

    try:
        answers = asyncio.run(_run())
    except (GeminiError, AnswerMismatchError) as exc:
        typer.echo(f"disambiguate failed: {exc}")
        # Nothing is written until a complete, verified answer set is in
        # hand, so a failure here can never leave a half-answered cache
        # that quietly mis-merges entities on the next load.
        typer.echo(
            "the answer cache was not modified — re-run `lore disambiguate`"
            " to try again"
        )
        raise typer.Exit(1)

    store.save_answers(answers)
    merges = sum(1 for value in answers.values() if value)
    typer.echo(
        f"asked {len(todo)} question(s) in {batches} request(s);"
        f" answered by {client.last_model}"
        f" ({client.requests_made} HTTP call(s) across the cascade)"
    )
    typer.echo(
        f"recorded {len(answers)} answer(s): {merges} merge(s),"
        f" {len(answers) - merges} refusal(s)"
    )
    typer.echo(f"cached in {store.answers_path} — a reload spends 0 requests")
    # Per model, because a cascade that fell through a 429 spent one
    # request against THAT model's budget, not against the one that
    # answered. Reporting the total against the winner would be a lie.
    spent = ", ".join(
        f"{model} {count}/{limits_for(model).rpd}"
        for model, count in getattr(client, "attempts", {}).items()
    )
    typer.echo(f"free-tier requests/day used by this run: {spent}")
    typer.echo(
        f"re-run `lore process <book> --skip-extract` to apply {merges} merge(s)"
    )


@app.command()
def graph() -> None:
    """Show what is in the knowledge graph."""
    config = LoreConfig.load()
    config.ensure_dirs()
    store = KuzuStore(config.kuzu_dir / "graph")
    try:
        for label in ("Book", "Chapter", "Chunk", "Entity"):
            rows = list(store.execute(f"MATCH (n:{label}) RETURN COUNT(n)"))
            typer.echo(f"{label:>8}: {rows[0][0] if rows else 0}")
        for rel in ("REL", "MENTIONS", "ALIAS_OF"):
            rows = list(store.execute(f"MATCH ()-[r:{rel}]->() RETURN COUNT(r)"))
            typer.echo(f"{rel:>8}: {rows[0][0] if rows else 0}")
        typer.echo("busiest entities:")
        busiest = list(store.execute(
            "MATCH (e:Entity)-[r:REL]-() RETURN e.name, COUNT(r) AS c"
            " ORDER BY c DESC LIMIT 5"
        ))
        for row in busiest:
            typer.echo(f"  {row[0]}  ({row[1]})")
        if not busiest:
            typer.echo("  (none — 0 relationships loaded)")
    finally:
        store.close()


_NO_GRAPH = "no books are loaded into the graph — run `lore process <book_id>`"


@app.command()
def ask(
    question: str,
    baseline: bool = typer.Option(
        False, "--baseline", help="answer from vector search alone, no graph"
    ),
) -> None:
    """Answer a question from the knowledge graph, with citations.

    Note: every question makes at least two LLM calls (choosing how to
    search, then writing the answer), so this is not instantaneous. Each
    runs on the local model unless lore.toml sets [lore.selection] or
    [lore.synthesis] provider = "gemini" (this project's lore.toml sets
    both), which sends the question, its seed entity names and its evidence
    passages to Google.

    Your books, graph and vectors are not modified. When a step runs on
    Gemini, each request is recorded in the gemini_ledger table of lore.db,
    and that write may first apply a pending schema migration (v3 to v4).
    """
    if not question.strip():
        # Checked before anything opens or any model is asked: a blank
        # question still embeds to a vector, and Task 9's `lore ask ""`
        # came back with a confident answer about Jonathan Small.
        typer.echo("ask failed: the question is blank; ask a question in words.")
        raise typer.Exit(2)
    config = LoreConfig.load()

    api_key = None
    remote = [f"[lore.{name}]" for name, section in (
        ("selection", config.selection), ("synthesis", config.synthesis))
        if section.provider == "gemini"]
    if remote:
        try:
            api_key = resolve_api_key(key_file=default_key_file())
        except GeminiKeyFileError as exc:
            typer.echo(f"ask failed: {exc}")
            raise typer.Exit(2)
        if api_key is None:
            # Never fall back to the local model: the user chose Gemini, and
            # a silent downgrade would hide the quality drop.
            typer.echo(
                f'{" and ".join(remote)} provider = "gemini" but no API key:'
                " set GEMINI_API_KEY (or GOOGLE_API_KEY), or put one line"
                " `GEMINI_API_KEY=...` in ~/.config/lore/gemini.env with mode"
                ' 600. Or set provider = "ollama" to run that step locally.'
            )
            raise typer.Exit(2)

    # The library itself is opened read-only below, never via open_db, so a
    # question can never create one or touch its content. The recorder is
    # the one narrow exception: `recorder_for` (gemini_budget.py) writes
    # through `open_db`, so when [lore.selection]/[lore.synthesis] send this
    # question to Gemini, that one bookkeeping write may apply a pending
    # schema migration to lore.db and add a row to gemini_ledger — it is the
    # same shared daily ledger `lore_search` over MCP spends. That is only
    # possible at all because the guard just below already requires
    # `config.sqlite_path.is_file()`: `open_db` here migrates a library that
    # already exists, it can never bring one into being. The shared-ledger
    # design (one budget for the CLI and MCP) makes this concession
    # necessary; nothing else about `ask` writes anything.
    graph_path = config.kuzu_dir / "graph"
    if not (graph_path.exists() and config.sqlite_path.is_file()
            and config.lancedb_dir.is_dir()):
        typer.echo(_NO_GRAPH)
        raise typer.Exit(1)
    try:
        kuzu = KuzuStore(graph_path, read_only=True)
    except RuntimeError as exc:
        typer.echo(f"ask failed: could not open the graph: {exc}")
        raise typer.Exit(1)
    conn = None
    try:
        # Queryable means "has a Book node in the graph": ground truth, not
        # books.status, which predates the graph for some loaded books.
        graphed = {
            row[0]: row[1]
            for row in kuzu.execute("MATCH (b:Book) RETURN b.id, b.title")
        }
        if not graphed:
            typer.echo(_NO_GRAPH)
            raise typer.Exit(1)
        conn = sqlite3.connect(f"file:{config.sqlite_path}?mode=ro", uri=True)
        titles = {**graphed, **{b["id"]: b["title"] for b in list_books(conn)}}
        lance = LanceStore(config.lancedb_dir)
        models = make_query_models(config, api_key,
                                   recorder=recorder_for(config.sqlite_path))

        async def _run():
            # The close MUST share the loop the requests ran on (see
            # _load_book_sync): a second asyncio.run() around aclose()
            # raises "Event loop is closed" after a successful answer.
            try:
                return await answer_question(conn, kuzu, lance, question, models,
                                             baseline=baseline)
            finally:
                await models.aclose()

        answer = asyncio.run(_run())
    except httpx.ConnectError as exc:
        typer.echo(f"ask failed: could not reach a model server"
                   f" ({config.ollama.host} for Ollama): {exc}")
        raise typer.Exit(1)
    except (ValueError, RuntimeError, sqlite3.Error, httpx.HTTPError,
            ProviderOutputError) as exc:
        # GeminiError is a RuntimeError; its messages never carry the key.
        typer.echo(f"ask failed: {exc}")
        raise typer.Exit(1)
    finally:
        kuzu.close()
        if conn is not None:
            conn.close()

    typer.echo(answer.text)
    if answer.note:
        typer.echo(f"\nnote: {answer.note}")
    if answer.passages:
        typer.echo("\nsources:")
        for passage in answer.passages:
            title = titles.get(passage.book_id) or passage.book_id
            typer.echo(f"  [{passage.n}] {title}, chapter {passage.chapter_seq}")
    line = f"mode: {answer.mode}"
    if answer.template:
        line += f" via {answer.template}"
    # One name when one model did both jobs, both when they differ.
    selected, answered = answer.selection_model, answer.model
    if selected and selected == answered:
        line += f" · selected and answered by {answered}"
    else:
        if selected:
            line += f" · selected by {selected}"
        if answered:
            line += f" · answered by {answered}"
    typer.echo(f"\n{line}")
    if len(answer.sub_questions) > 1:
        # One line, so a multi-part answer shows which parts it searched for.
        typer.echo("sub-questions: " + " | ".join(answer.sub_questions))


@app.command(name="mcp")
def mcp_command(
    http: bool = typer.Option(False, "--http",
                              help="serve Streamable HTTP instead of stdio"),
    host: str = typer.Option(
        "127.0.0.1",
        help="HTTP bind address; loopback only (127.0.0.0/8, localhost, ::1)"),
    port: int = typer.Option(8765, help="HTTP port"),
) -> None:
    """Serve LORE's seven query tools over MCP.

    No tool modifies your books, graph or vectors. lore_search records its
    Gemini requests in the gemini_ledger table of lore.db, and that write
    may first apply a pending schema migration (v3 to v4). Store handles
    open and close per request, so `lore graph load` keeps working while
    this is running. --http binds loopback only: there is no authentication.
    """
    from lore.mcp.server import NonLoopbackHost, _check_loopback, run_http, run_stdio

    config = LoreConfig.load()
    if http:
        # run_http checks this too (so nothing can bypass it); checking here
        # first turns the refusal into a clean message instead of a traceback.
        try:
            _check_loopback(host)
        except NonLoopbackHost as exc:
            typer.echo(f"lore mcp: {exc}", err=True)
            raise typer.Exit(2)
        # stderr: in stdio mode stdout is the protocol channel, and the habit
        # is worth keeping in both.
        typer.echo(f"lore mcp: streamable-http on http://{host}:{port}", err=True)
        run_http(config, host, port)
    else:
        run_stdio(config)


@app.command(name="ui")
def ui_command(
    host: str = typer.Option(
        "127.0.0.1",
        help="bind address; loopback only (127.0.0.0/8, localhost, ::1)"),
    port: int | None = typer.Option(None, help="port (default: [lore.web] port, 8766)"),
    dev: bool = typer.Option(False, "--dev",
                             help="also accept the Vite dev server's origin (port 5173)"),
) -> None:
    """Serve LORE's web app: ask questions, read cited answers, explore the graph.

    Read-only on your books, graph and vectors; asking a question spends
    Gemini requests like `lore ask`. Loopback only: there is no
    authentication.
    """
    import uvicorn

    from lore.web.app import create_app
    from lore.web.security import NonLoopbackHost, check_loopback

    config = LoreConfig.load()
    port = port or config.web.port
    try:
        check_loopback(host)
    except NonLoopbackHost as exc:
        typer.echo(f"lore ui: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(f"lore ui: http://{host}:{port}", err=True)
    uvicorn.run(create_app(config, host=host, port=port, dev=dev),
                host=host, port=port, log_level="warning")
