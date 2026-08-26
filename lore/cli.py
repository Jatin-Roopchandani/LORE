"""LORE CLI. Phase 0 ships a single command: `lore doctor`."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import typer

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.db.sqlite import open_db, schema_version
from lore.ingest.chunker import chunk_book
from lore.ingest.epub import parse_epub
from lore.ingest.models import IngestError, book_id_for_file
from lore.ingest.pdf import parse_pdf
from lore.ingest.registry import ingest_book, list_books
from lore.inference.ollama import OllamaProvider
from lore.kaggle_api.client import KaggleClient
from lore.kaggle_api.hello import run_hello_roundtrip
from lore.orchestrator.quota import iso_week, week_report
from lore.orchestrator.run import run_extraction

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
def ingest(path: Path) -> None:
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
        created = ingest_book(
            conn, book_id=book_id, title=parsed.title, author=parsed.author,
            source="upload", file_path=str(path), chunks=chunks,
        )
    finally:
        conn.close()
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
        typer.echo(
            f"{row['id']}  {row['title']}  [{row['status']}]  "
            f"{row['chunk_count']} chunks"
        )


@app.command()
def extract(
    book_id: str,
    resume: bool = typer.Option(False, "--resume", help="skip completed work units"),
) -> None:
    """Extract entities and relationships for an ingested book."""
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        report = run_extraction(
            config, conn, book_id, resume=resume,
            on_event=lambda message: typer.echo(f"       {message}"),
        )
    except ValueError as exc:
        typer.echo(f"extract failed: {exc}")
        raise typer.Exit(1)
    finally:
        conn.close()
    typer.echo(
        f"{report.book_id}: {report.units_completed}/{report.units_total} units, "
        f"{report.chunks_ok} chunks extracted, {report.chunks_dead} dead-lettered, "
        f"{report.gpu_hours:.2f} GPU-hours"
    )
    if report.units_failed:
        typer.echo(f"{report.units_failed} unit(s) failed — rerun with --resume")
        raise typer.Exit(1)


@app.command()
def quota() -> None:
    """Show this week's Kaggle GPU-hour budget."""
    config = LoreConfig.load()
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    try:
        report = week_report(conn, iso_week())
    finally:
        conn.close()
    typer.echo(
        f"{report['week']}: {report['used_hours']:.2f}h used, "
        f"{report['remaining_hours']:.2f}h remaining across {report['runs']} run(s)"
    )
