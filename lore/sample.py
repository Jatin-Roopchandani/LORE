"""The sample library: one ready-made book every new reader starts with.

A template is a complete per-user library directory (the layout of
`<root>/users/<id>/`: lore.db, kuzu/, lancedb/, codex/, beats/, extractions/
and the rest) holding exactly one processed book. It lives at
`<data root>/sample/` and is never opened by the app, only copied:

* `make_template` builds one from an existing library (`lore sample make`):
  it copies the library, takes every other book out with the stores' own
  removal code (`remove_book_from_graph`, then `forget_book`), drops their
  artifacts and the reader's personal state, rebuilds the codex entries the
  removal dropped from the saved answers, compacts the stores so nothing of
  the removed books is left in their files, and checks the result.
* `seed_new_library` copies it into a brand-new user's directory, once, the
  first time that directory is made (lore.tenancy.user_data_dir).
* `add_to_empty_library` copies it into an existing library that has no
  books (the Library panel's "Add the sample book").

Every copy goes to a temp directory beside its destination and is renamed
into place, so a crash never leaves half a library. `LORE_SAMPLE_LIBRARY=0`
switches seeding and the button off.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from lore.config import LoreConfig

log = logging.getLogger(__name__)

ENV_SAMPLE = "LORE_SAMPLE_LIBRARY"
SAMPLE_DIRNAME = "sample"
DEFAULT_WORLD = "Sherlock Holmes"

# sqlite's own files: never copied byte for byte from a library in use.
_DB_FILES = ("lore.db", "lore.db-wal", "lore.db-shm", "lore.db-journal")
# What a library directory holds (LoreConfig's paths). Anything else found in
# a source library is reported, not copied blindly into every new account.
_KNOWN = {"lore.db", "kuzu", "lancedb", "inbox", "extractions", "disambiguation",
          "beats", "codex", "adjudication", "narrators", "speakers"}


class SampleError(RuntimeError):
    """The template cannot be made or copied (the message says why)."""


def enabled() -> bool:
    return os.environ.get(ENV_SAMPLE, "1").strip().lower() not in (
        "0", "false", "no", "off")


def template_dir(root: Path) -> Path:
    return Path(root) / SAMPLE_DIRNAME


def available(root: Path) -> bool:
    """Seeding is on and a template exists under this data root."""
    return enabled() and (template_dir(root) / "lore.db").is_file()


# --------------------------------------------------------------------------
# Copying a template into a library


def _copy_db(src: Path, dest: Path) -> None:
    """A consistent copy of a sqlite database; the source is never migrated
    or written. With no `-wal` beside it no connection has it open (WAL mode
    keeps that file while one does), so a byte copy is whole and leaves the
    source exactly as it was. Otherwise the backup API reads a consistent
    snapshot through a read-only connection."""
    if not src.with_name(src.name + "-wal").exists():
        shutil.copy2(src, dest)
        return
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE")
        finally:
            target.close()
    finally:
        source.close()


def _copy_library(src: Path, dest: Path) -> None:
    """`src` copied to `dest` (which must not exist); sqlite by backup."""
    shutil.copytree(src, dest, symlinks=True,
                    ignore=lambda d, names: [n for n in names if n in _DB_FILES]
                    if Path(d) == src else [])
    if (src / "lore.db").is_file():
        _copy_db(src / "lore.db", dest / "lore.db")


def rehome(conn: sqlite3.Connection, library: Path, files: Path | None = None) -> int:
    """Point every book's stored file at this library's inbox when the file is
    there. `books.file_path` is absolute (the upload's stored name in the
    library that received it), so a copied library would otherwise point
    into the library it was copied from. `files`: where the library is now,
    when it is staged before being renamed to `library`. Returns the rows
    changed."""
    changed = 0
    inbox = Path(library) / "inbox"
    present = Path(files or library) / "inbox"
    for book_id, file_path in conn.execute(
            "SELECT id, file_path FROM books").fetchall():
        if not file_path:
            continue
        name = Path(file_path).name
        if (present / name).is_file() and file_path != str(inbox / name):
            conn.execute("UPDATE books SET file_path = ? WHERE id = ?",
                         (str(inbox / name), book_id))
            changed += 1
    conn.commit()
    return changed


def _rehome_dir(staged: Path, final: Path) -> None:
    conn = sqlite3.connect(staged / "lore.db")
    try:
        rehome(conn, final, staged)
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()


def _staging(dest: Path, tag: str) -> Path:
    return dest.parent / f".{dest.name}.{tag}-{uuid.uuid4().hex[:8]}"


def seed_new_library(root: Path, dest: Path) -> bool:
    """Copy the template to `dest`, a library directory that does not exist
    yet. True when this call put it there. False (and nothing changed) when
    seeding is off, there is no template, or `dest` appeared meanwhile:
    another request for the same user won the race, and its copy stands.
    Never raises: a failed copy leaves no trace and the caller makes an
    empty library as before."""
    if dest.exists() or not available(root):
        return False
    template = template_dir(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = _staging(dest, "seed")
    try:
        _copy_library(template, staged)
        _rehome_dir(staged, dest)
        try:
            # Atomic; refused when another seed already put a library there.
            os.rename(staged, dest)
        except OSError:
            return False
        _fsync_dir(dest.parent)
        log.info("seeded %s with the sample library", dest.name)
        return True
    except Exception:  # never in the way of an account's first request
        log.exception("seeding %s with the sample library failed", dest.name)
        return False
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


def _fsync_dir(path: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class LibraryNotEmpty(SampleError):
    """The library has books (or work under way): the sample is not merged in."""


@contextlib.contextmanager
def _library_lock(library: Path):
    """One sample copy at a time per library, across processes."""
    lock = library.parent / f".{library.name}.sample.lock"
    with open(lock, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def library_busy(conn: sqlite3.Connection, library: Path) -> str | None:
    """Why the sample cannot go in now, or None. A library qualifies only
    with no books, no unfinished processing and no upload in progress."""
    if conn.execute("SELECT 1 FROM books LIMIT 1").fetchone():
        return "not_empty"
    if conn.execute("SELECT 1 FROM book_jobs WHERE state != 'done' LIMIT 1").fetchone():
        return "busy"
    inbox = Path(library) / "inbox"
    if inbox.is_dir() and any(inbox.glob(".upload-*.part")):
        return "busy"
    return None


# Rows of an empty library that are the reader's own and survive the swap:
# today's request counts (the free tier's allowance), the step timings, and
# any worlds made ahead of a first book.
_CARRIED = ("gemini_ledger", "quota_ledger", "stage_timings")


def add_to_empty_library(root: Path, library: Path) -> str:
    """Put the template in `library`, an existing library with no books.
    Returns the sample book's id. Raises SampleError when there is no
    template (or seeding is off) and LibraryNotEmpty when the library has a
    book or work under way.

    The library's top-level entries are swapped for the template's one by
    one, each an atomic rename, with lore.db last: until that rename the
    library still has no books (a crash can be retried); after it the
    library is the sample's. Only then are the old entries deleted."""
    from lore.db.sqlite import open_db

    if not available(root):
        raise SampleError("no sample library on this server")
    library = Path(library)
    with _library_lock(library):
        conn = open_db(library / "lore.db")
        try:
            why = library_busy(conn, library)
            if why is not None:
                raise LibraryNotEmpty(why)
            carried = {t: _rows(conn, t) for t in _CARRIED}
            worlds = [r[0] for r in conn.execute("SELECT name FROM worlds")]
        finally:
            conn.close()
        staged = _staging(library, "sample")
        aside = _staging(library, "old")
        try:
            _copy_library(template_dir(root), staged)
            book_id = _merge_personal(staged, library, carried, worlds)
            aside.mkdir()
            names = sorted(p.name for p in staged.iterdir() if p.name != "lore.db")
            for name in names:
                if (library / name).exists():
                    os.rename(library / name, aside / name)
                os.rename(staged / name, library / name)
            for name in ("lore.db-wal", "lore.db-shm", "lore.db-journal"):
                if (library / name).exists():  # the old database's, never the new one's
                    os.rename(library / name, aside / name)
            os.replace(staged / "lore.db", library / "lore.db")
            _fsync_dir(library)
        finally:
            shutil.rmtree(staged, ignore_errors=True)
            shutil.rmtree(aside, ignore_errors=True)
    return book_id


def _rows(conn: sqlite3.Connection, table: str) -> tuple[list[str], list[tuple]]:
    cur = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - fixed names
    return [d[0] for d in cur.description], cur.fetchall()


def _merge_personal(staged: Path, library: Path, carried: dict,
                    worlds: list[str]) -> str:
    """Write the reader's own rows into the staged copy; point its book at
    `library`. Returns the sample book's id."""
    from lore.db.sqlite import open_db

    conn = open_db(staged / "lore.db")  # migrates an older template
    try:
        with conn:
            for table, (columns, rows) in carried.items():
                cols = [c for c in columns if c != "id"]
                for row in rows:
                    values = dict(zip(columns, row))
                    conn.execute(
                        f"INSERT OR IGNORE INTO {table} ({', '.join(cols)})"  # noqa: S608
                        f" VALUES ({', '.join('?' for _ in cols)})",
                        [values[c] for c in cols])
            for name in worlds:
                conn.execute("INSERT OR IGNORE INTO worlds (name) VALUES (?)", (name,))
        rehome(conn, library, staged)
        row = conn.execute("SELECT id FROM books ORDER BY created_at LIMIT 1").fetchone()
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()
    if row is None:
        raise SampleError("the sample library has no book")
    return row[0]


# --------------------------------------------------------------------------
# Making a template


@dataclass
class TemplateSummary:
    book_id: str
    title: str
    world: str
    chunks: int = 0
    entities: int = 0
    relationships: int = 0
    beats: int = 0
    codex_entries: int = 0
    codex_missing: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # titles of the books taken out
    size_bytes: int = 0
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"book: {self.title} ({self.book_id})",
            f"world: {self.world}",
            f"removed: {', '.join(self.removed) or 'nothing'}",
            f"chunks: {self.chunks}  entities: {self.entities}"
            f"  relationships: {self.relationships}  beats: {self.beats}"
            f"  codex entries: {self.codex_entries}",
            f"size on disk: {self.size_bytes / 1e6:.1f} MB",
        ]
        if self.codex_missing:
            out.append(f"codex: {len(self.codex_missing)} entries have no saved answer"
                       f" and were not rebuilt: {', '.join(self.codex_missing[:10])}"
                       + (" ..." if len(self.codex_missing) > 10 else ""))
        out += [f"warning: {w}" for w in self.warnings]
        return out


def make_template(source: Path, out: Path, keep: str, *,
                  world_name: str | None = DEFAULT_WORLD,
                  base: LoreConfig | None = None,
                  codex_refresh: Callable[[LoreConfig, sqlite3.Connection, str], None]
                  | None = None) -> TemplateSummary:
    """Make a template at `out` from the library at `source`, keeping only
    book `keep`. `source` is only read. `out` must not exist; it appears
    complete or not at all. `codex_refresh(config, conn, book_id)` rebuilds
    the kept world's codex from saved answers (the default runs the codex
    stage with nothing left to ask; tests pass their own)."""
    source, out = Path(source).resolve(), Path(out).resolve()
    if not (source / "lore.db").is_file():
        raise SampleError(f"{source} is not a library (no lore.db)")
    if out.exists():
        raise SampleError(f"{out} already exists; pick a new --out")
    if out.is_relative_to(source) or source.is_relative_to(out):
        raise SampleError("--out must be outside the source library")
    out.parent.mkdir(parents=True, exist_ok=True)
    staged = _staging(out, "partial")
    try:
        _copy_library(source, staged)
        config = (base or LoreConfig.load()).model_copy(update={"data_dir": staged})
        summary = _reduce(config, keep, world_name, codex_refresh or _CachedCodex())
        _rehome_dir(staged, out)
        summary.warnings += [f"unexpected entry in the source library, left out: {n}"
                             for n in sorted(_unknown(staged))]
        for name in _unknown(staged):
            target = staged / name
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        summary.warnings += [f"{p.relative_to(staged)} still names the source library"
                             for p in _mentions(staged, str(source).encode())]
        summary.size_bytes = sum(p.stat().st_size for p in staged.rglob("*") if p.is_file())
        os.rename(staged, out)
        return summary
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


def _unknown(library: Path) -> list[str]:
    return [p.name for p in library.iterdir() if p.name not in _KNOWN]


def _mentions(library: Path, needle: bytes) -> list[Path]:
    """Files whose bytes contain `needle` (an absolute path that must not
    survive into a copy)."""
    found = []
    for path in sorted(library.rglob("*")):
        if not path.is_file():
            continue
        with open(path, "rb") as handle:
            tail = b""
            while block := handle.read(1 << 20):
                if needle in tail + block:
                    found.append(path)
                    break
                tail = block[-len(needle):]
    return found


def _reduce(config: LoreConfig, keep: str, world_name: str | None,
            codex_refresh: Callable) -> TemplateSummary:
    from lore import worlds
    from lore.db.kuzu_store import KuzuStore
    from lore.db.lance_store import LanceStore
    from lore.db.sqlite import open_db
    from lore.orchestrator import jobs

    conn = open_db(config.sqlite_path)
    try:
        books = {r[0]: r[1] for r in conn.execute("SELECT id, title FROM books")}
        if keep not in books:
            raise SampleError(f"no book {keep!r} in the source library"
                              f" (books: {', '.join(sorted(books)) or 'none'})")
        status = conn.execute("SELECT status FROM books WHERE id = ?", (keep,)).fetchone()[0]
        if status != "graphed":
            raise SampleError(f"{books[keep]!r} is not fully processed (status {status})")
        job = jobs.get_job(conn, keep)
        if job is not None and job.state != jobs.DONE:
            raise SampleError(f"{books[keep]!r} has unfinished processing"
                              f" ({job.state} at {job.stage}); finish it first")
        others = sorted(b for b in books if b != keep)
        summary = TemplateSummary(keep, books[keep], "")
        summary.removed = [books[b] for b in others]

        kuzu = KuzuStore(config.kuzu_dir / "graph")
        try:
            lance = LanceStore(config.lancedb_dir)
            for book_id in others:
                delete_book(config, conn, kuzu, lance, book_id)
        finally:
            kuzu.close()

        _drop_empty_worlds(conn)
        world = worlds.world_of(conn, keep)
        if world_name and world.name != world_name:
            worlds.rename_world(conn, world.id, world_name)
            world = worlds.world_of(conn, keep)
        summary.world = world.name
        _clear_personal(conn, keep)

        saved = conn.execute("SELECT * FROM book_jobs WHERE book_id = ?", (keep,)).fetchone()
        codex_refresh(config, conn, keep)
        _restore_job(conn, keep, saved)
        summary.codex_missing = list(getattr(codex_refresh, "missing", []) or [])

        _prune_caches(config, conn, keep)
        _compact(config, conn)
        _verify(config, conn, keep, summary)
    finally:
        conn.close()
    return summary


def delete_book(config: LoreConfig, conn: sqlite3.Connection, kuzu, lance,
                book_id: str) -> None:
    """Delete a book from a library completely: out of the graph and the
    vector indexes (`remove_book_from_graph`, the move's own removal), its
    processing state, its SQLite rows (`forget_book`) and its files.

    The app itself has no "delete a book" yet; this is that path, built from
    the pieces it does have."""
    from lore.graph.remove import remove_book_from_graph
    from lore.ingest.registry import forget_book

    file_path = (conn.execute("SELECT file_path FROM books WHERE id = ?",
                              (book_id,)).fetchone() or [None])[0]
    remove_book_from_graph(conn, kuzu, lance, book_id)
    with conn:
        conn.execute("DELETE FROM book_jobs WHERE book_id = ?", (book_id,))
    forget_book(conn, book_id)
    for directory in (config.extractions_dir, config.beats_dir):
        shutil.rmtree(directory / book_id, ignore_errors=True)
    store = _disambiguation(config)
    store.pending_path(book_id).unlink(missing_ok=True)
    (store.synonym_pending_dir / store.pending_path(book_id).name).unlink(missing_ok=True)
    # Narrator hints are keyed by book; speakers by chunk (`<book>:<ch>:<seq>`).
    _filter_answers(config.narrators_dir, lambda key: not key.startswith(f"{book_id}:"))
    _filter_answers(config.speakers_dir, lambda key: not key.startswith(f"{book_id}:"))
    if file_path:
        stored = config.inbox_dir / Path(file_path).name
        if stored.is_file():
            stored.unlink()


def _disambiguation(config: LoreConfig):
    from lore.resolve.disambiguation import DisambiguationStore
    return DisambiguationStore(config.disambiguation_dir)


def _filter_answers(directory: Path, keep: Callable[[str], bool]) -> None:
    """Rewrite `<dir>/answers.json` with only the entries `keep` accepts."""
    from lore import fs

    path = directory / "answers.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    kept = {k: v for k, v in data.items() if keep(str(k))}
    if len(kept) != len(data):
        fs.atomic_write_json(path, kept)


def _drop_empty_worlds(conn: sqlite3.Connection) -> None:
    from lore import worlds

    for world in worlds.list_worlds(conn):
        if not worlds.books_in(conn, world.id):
            worlds.delete_world(conn, world.id)
    with conn:
        for table in ("codex_summaries", "codex_entries", "entity_intros"):
            conn.execute(f"DELETE FROM {table} WHERE world_id NOT IN"  # noqa: S608
                         " (SELECT id FROM worlds)")


def _clear_personal(conn: sqlite3.Connection, keep: str) -> None:
    """What belongs to the reader the library came from, not to the book."""
    with conn:
        for table in ("reading_positions", "quota_ledger", "gemini_ledger", "stage_timings"):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
        # Extraction units: only the completed ones of the kept book are the
        # book's (the loader reads them); queued or failed rows are history.
        conn.execute("DELETE FROM jobs WHERE book_id IS NOT ? OR state != 'completed'",
                     (keep,))


def _restore_job(conn: sqlite3.Connection, keep: str, saved) -> None:
    """The kept book's row as it was before the codex refresh, finished,
    with nothing of the run that made it (provider, clocks, reasons)."""
    with conn:
        conn.execute("DELETE FROM book_jobs WHERE book_id = ?", (keep,))
        if saved is None:
            return
        cols = [d[1] for d in conn.execute("PRAGMA table_info(book_jobs)")]
        row = dict(zip(cols, saved))
        row.update(state="done", stopped_reason=None, resumable_at=None,
                   provider=None, stage_started_at=None, stage_base=None,
                   move_world_id=None)
        conn.execute(f"INSERT INTO book_jobs ({', '.join(row)}) VALUES"  # noqa: S608
                     f" ({', '.join('?' for _ in row)})", list(row.values()))


class _CachedCodex:
    """The codex stage with nothing to ask: it saves the entries whose answer
    is already on disk (the removal dropped every entry an other book
    touched) and leaves the rest, listed in `missing`."""

    def __init__(self, **factories) -> None:
        self._factories = factories
        self.missing: list[str] = []

    def __call__(self, config: LoreConfig, conn: sqlite3.Connection, book_id: str) -> None:
        from lore.codex import build as codex_build
        from lore.orchestrator.pipeline import JobCtl, StageContext
        from lore.orchestrator.stages import CodexStage

        outer = self

        class Stage(CodexStage):
            def _plan(self, ctx, kuzu):
                plan = super()._plan(ctx, kuzu)
                ready = []
                for item in plan:
                    if (self._saved_current(ctx, item) or codex_build.read_cached(
                            self._cache(ctx, item), item.evidence_hash) is not None):
                        ready.append(item)
                    else:
                        outer.missing.append(item.entity.name)
                return ready

        def refuse(cfg):
            raise SampleError("the sample's codex is rebuilt from saved answers only")

        self.missing = []
        stage = Stage(client_factory=refuse, **self._factories)
        ctx = StageContext(conn=conn, config=config, book_id=book_id)
        stage.run(ctx, JobCtl(ctx))
        self.missing = sorted(set(self.missing))


def cached_codex(**factories) -> _CachedCodex:
    """A codex refresh on the given stage factories (the tests' seam)."""
    return _CachedCodex(**factories)


def _prune_caches(config: LoreConfig, conn: sqlite3.Connection, keep: str) -> None:
    """Per-entity and per-chunk caches of what is gone."""
    chunk_ids = {r[0] for r in conn.execute("SELECT id FROM chunks WHERE book_id = ?",
                                            (keep,))}
    _filter_answers(config.speakers_dir,
                    lambda key: key.split(":sha256=")[0] in chunk_ids)
    if not config.codex_dir.is_dir():
        return
    # The answer each saved entry was built from; any other (an entity of a
    # removed book, or evidence that included one) quotes what is gone.
    current = {f"{entity}-{evidence}" for entity, evidence in conn.execute(
        "SELECT entity_id, evidence_hash FROM codex_summaries")}
    for path in config.codex_dir.iterdir():
        if path.is_file() and path.name.split(".", 1)[0] not in current:
            path.unlink()


def _compact(config: LoreConfig, conn: sqlite3.Connection) -> None:
    """Rewrite each store so the removed books' bytes are gone from its files,
    not merely unreachable: Kuzu keeps deleted strings in its pages, LanceDB
    keeps every old version, sqlite keeps freed pages."""
    from lore.db.kuzu_store import KuzuStore

    graph = config.kuzu_dir / "graph"
    if graph.exists():
        counts = _graph_counts(graph)
        export = config.kuzu_dir / f".export-{uuid.uuid4().hex[:8]}"
        rebuilt = config.kuzu_dir / f".graph-{uuid.uuid4().hex[:8]}"
        try:
            store = KuzuStore(graph)
            try:
                store.execute(f"EXPORT DATABASE '{export}' (format='parquet')")
            finally:
                store.close()
            import kuzu as _kuzu
            db = _kuzu.Database(str(rebuilt))
            try:
                c = _kuzu.Connection(db)
                c.execute(f"IMPORT DATABASE '{export}'")
                c.close()
            finally:
                db.close()
            if _graph_counts(rebuilt) != counts:
                raise SampleError("the rebuilt graph does not match the original")
            for p in config.kuzu_dir.glob("graph*"):
                p.unlink() if p.is_file() else shutil.rmtree(p)
            for p in list(config.kuzu_dir.glob(f"{rebuilt.name}*")):
                p.rename(config.kuzu_dir / p.name.replace(rebuilt.name, "graph", 1))
        finally:
            shutil.rmtree(export, ignore_errors=True)
            for p in config.kuzu_dir.glob(".graph-*"):
                p.unlink() if p.is_file() else shutil.rmtree(p, ignore_errors=True)
    if config.lancedb_dir.is_dir():
        import lancedb
        # Each table written again as a new dataset: an old version, or a
        # delete's transaction file (it holds the predicate, a book id), would
        # survive optimize. No table has an index to rebuild.
        db = lancedb.connect(str(config.lancedb_dir))
        for name in _table_names(db):
            table = db.open_table(name)
            data, schema, rows = table.to_arrow(), table.schema, table.count_rows()
            db.drop_table(name)
            fresh = db.create_table(name, data=data, schema=schema)
            if fresh.count_rows() != rows:
                raise SampleError(f"rewriting vector table {name} lost rows")
    conn.execute("VACUUM")


def _table_names(db) -> list[str]:
    listed = db.list_tables() if hasattr(db, "list_tables") else db.table_names()
    names = getattr(listed, "tables", listed)
    return list(names)


_COUNTED = ("Book", "Chapter", "Chunk", "Entity")
_COUNTED_RELS = ("HAS_CHAPTER", "HAS_CHUNK", "MENTIONS", "ALIAS_OF", "REL")


def _graph_counts(path: Path) -> dict[str, int]:
    from lore.db.kuzu_store import KuzuStore

    store = KuzuStore(path, read_only=True)
    try:
        out = {t: int(next(iter(store.execute(f"MATCH (n:{t}) RETURN count(n)")))[0])
               for t in _COUNTED}
        out.update({r: int(next(iter(store.execute(
            f"MATCH ()-[r:{r}]->() RETURN count(r)")))[0]) for r in _COUNTED_RELS})
        return out
    finally:
        store.close()


def _verify(config: LoreConfig, conn: sqlite3.Connection, keep: str,
            summary: TemplateSummary) -> None:
    """The template holds the one book and nothing of any other."""
    import lancedb

    from lore.db.kuzu_store import KuzuStore
    from lore.graph.loader import check_world_invariant

    books = [r[0] for r in conn.execute("SELECT id FROM books")]
    if books != [keep]:
        raise SampleError(f"the template holds books {books}, not just {keep}")
    kuzu = KuzuStore(config.kuzu_dir / "graph", read_only=True)
    try:
        check_world_invariant(conn, kuzu)
        graph_books = sorted(r[0] for r in kuzu.execute("MATCH (b:Book) RETURN b.id"))
        strays = sorted({b for r in kuzu.execute("MATCH (e:Entity) RETURN e.books")
                         for b in (r[0] or ()) if b != keep})
        if graph_books != [keep] or strays:
            raise SampleError(f"the graph still names other books: {graph_books}, {strays}")
        counts = _graph_counts_open(kuzu)
    finally:
        kuzu.close()
    db = lancedb.connect(str(config.lancedb_dir))
    left = db.open_table("chunks").count_rows(f"book_id != '{keep}'")
    if left:
        raise SampleError(f"{left} vectors of other books are still indexed")
    summary.chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    summary.entities = counts["Entity"]
    summary.relationships = counts["REL"]
    summary.beats = conn.execute("SELECT COUNT(*) FROM beats").fetchone()[0]
    summary.codex_entries = conn.execute(
        "SELECT COUNT(*) FROM codex_summaries").fetchone()[0]


def _graph_counts_open(kuzu) -> dict[str, int]:
    return {"Entity": int(next(iter(kuzu.execute("MATCH (n:Entity) RETURN count(n)")))[0]),
            "REL": int(next(iter(kuzu.execute("MATCH ()-[r:REL]->() RETURN count(r)")))[0])}
