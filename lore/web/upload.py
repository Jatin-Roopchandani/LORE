"""POST /api/books/upload: a book uploaded in the browser (release step 5).

The body is untrusted, so the order matters:

1. Who is asking (401 signed out; CSRF is the app-wide guard), and in multi
   mode: a Gemini key (409 key_required), one upload at a time per user
   (409 busy), at most `per_hour` attempts (429 rate_limited), `per_day`
   successful uploads in 24 hours (429 daily_limit) and `max_books` books in
   the library (409 too_many_books). All of this before a byte is read.
2. The multipart body is parsed AS IT STREAMS (python-multipart, no spooling
   to /tmp): exactly a `file` part and an optional `world` part, nothing
   else. The file goes to `<library>/inbox/.upload-<random>.part`, a name the
   server makes, never one built from the upload; the bytes are counted
   (`max_file_bytes`, 413 too_large) and hashed as they pass. lore.web.limits
   counts the whole body too, and refuses a Content-Length over the limit.
3. The name must end .epub or .pdf AND the magic bytes must say the same
   type (400 not_a_book); then lore.ingest.upload_check (zip structure and
   bombs, traversal, symlinks, DRM, encrypted PDFs, page count).
4. The content hash is the book id (as `lore ingest`): a book already in
   this library is 409 already_added with its id.
5. Parse, chunk and register with `ingest_book`, then start processing
   (the caller's `start`): local mode in this process, multi mode a central
   job, and a spent Gemini budget leaves the book waiting for the reset.

Every refusal deletes what was written. The original filename is kept only
as display text (sanitised): the fallback title and the response.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import threading
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.ingest.upload_check import Limits, Refused, check_book, sniff
from lore.tenancy import CurrentUser
from lore.web.limits import UPLOAD_FILE_BYTES, UPLOAD_PATH, TooLarge

ENV_PER_DAY = "LORE_UPLOADS_PER_DAY"
ENV_PER_HOUR = "LORE_UPLOADS_PER_HOUR"
ENV_MAX_BOOKS = "LORE_MAX_BOOKS"
PER_DAY = 10
PER_HOUR = 30
MAX_BOOKS = 30
DAY = timedelta(hours=24)
_MAX_HEADER_BYTES = 8 * 1024
_MAX_FIELD_BYTES = 1024
_MAX_PARTS = 2
_DISPLAY_CHARS = 200
_KINDS = {".epub": "epub", ".pdf": "pdf"}
RATE_ATTEMPT = "upload"       # every attempt, for the hourly rate limit
RATE_ADDED = "upload_added"   # each book added, for the daily quota


def _env_int(env, name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number") from None
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


@dataclass(frozen=True)
class UploadSettings:
    """The upload's limits. The quotas apply in multi mode only."""
    max_file_bytes: int = UPLOAD_FILE_BYTES
    per_day: int = PER_DAY
    per_hour: int = PER_HOUR
    max_books: int = MAX_BOOKS
    checks: Limits = field(default_factory=Limits)

    @classmethod
    def from_env(cls, env=None) -> "UploadSettings":
        env = os.environ if env is None else env
        return cls(per_day=_env_int(env, ENV_PER_DAY, PER_DAY),
                   per_hour=_env_int(env, ENV_PER_HOUR, PER_HOUR),
                   max_books=_env_int(env, ENV_MAX_BOOKS, MAX_BOOKS))


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}, **extra},
                        status_code=status, headers={"Cache-Control": "no-store"})


def display_name(raw: str | None) -> str:
    """The uploaded name as harmless display text: its last path part, NFC,
    no control or format characters (bidi overrides included), single
    spaces, at most 200 characters. Never used to build a path."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] not in ("C", "Z")
                   or ch == " ")
    name = " ".join(name.split()).strip(" .")
    return name[:_DISPLAY_CHARS] or "book"


class _Bad(Exception):
    """The form is not what this route takes: 400 bad_upload."""


class _TooBig(Exception):
    pass


class _Form:
    """A streaming multipart/form-data reader for one `file` and one `world`.

    The file's bytes go straight to `dest` (created exclusively, mode 0600),
    counted and hashed on the way."""

    def __init__(self, boundary: bytes, dest: Path, max_file: int) -> None:
        self.dest, self.max_file = dest, max_file
        self.parser = MultipartParser(boundary, {
            "on_part_begin": self._begin, "on_header_field": self._hfield,
            "on_header_value": self._hvalue, "on_header_end": self._hend,
            "on_headers_finished": self._headers_done, "on_part_data": self._data,
            "on_part_end": self._part_end, "on_end": self._end})
        self.parts = 0
        self.names: set[str] = set()
        self.world: str | None = None
        self.filename: str | None = None
        self.size = 0
        self.head = b""
        self.hash = hashlib.sha256()
        self.ended = False
        self._file = None
        self._field: bytearray | None = None
        self._current: str | None = None
        self._hname = self._hval = b""
        self._headers: dict[bytes, bytes] = {}
        self._header_bytes = 0

    # parser callbacks -------------------------------------------------
    def _begin(self) -> None:
        self.parts += 1
        if self.parts > _MAX_PARTS:
            raise _Bad("send only the file and the world")
        self._headers, self._header_bytes = {}, 0

    def _grow(self, n: int) -> None:
        self._header_bytes += n
        if self._header_bytes > _MAX_HEADER_BYTES:
            raise _Bad("a part's headers are too long")

    def _hfield(self, data: bytes, start: int, end: int) -> None:
        self._grow(end - start)
        self._hname += data[start:end]

    def _hvalue(self, data: bytes, start: int, end: int) -> None:
        self._grow(end - start)
        self._hval += data[start:end]

    def _hend(self) -> None:
        self._headers[self._hname.strip().lower()] = self._hval.strip()
        self._hname = self._hval = b""

    def _headers_done(self) -> None:
        kind, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        if kind != b"form-data" or b"name" not in options:
            raise _Bad("each part needs a form-data name")
        name = options[b"name"].decode("utf-8", "replace")
        if name not in ("file", "world") or name in self.names:
            raise _Bad("send only the file and the world")
        self.names.add(name)
        self._current = name
        if name == "file":
            if b"filename" not in options:
                raise _Bad("the file part needs a filename")
            self.filename = options[b"filename"].decode("utf-8", "replace")
            fd = os.open(self.dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self._file = os.fdopen(fd, "wb")
        else:
            if b"filename" in options:
                raise _Bad("the world is text, not a file")
            self._field = bytearray()

    def _data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._current == "file":
            self.size += len(chunk)
            if self.size > self.max_file:
                raise _TooBig()
            if len(self.head) < 8:
                self.head = (self.head + chunk)[:8]
            self.hash.update(chunk)
            self._file.write(chunk)
        else:
            if len(self._field) + len(chunk) > _MAX_FIELD_BYTES:
                raise _Bad("the world name is too long")
            self._field.extend(chunk)

    def _part_end(self) -> None:
        if self._current == "file":
            self._file.close()
            self._file = None
        elif self._current == "world":
            self.world = bytes(self._field).decode("utf-8", "replace").strip()
        self._current = None

    def _end(self) -> None:
        self.ended = True

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def write(self, chunk: bytes) -> None:
        try:
            self.parser.write(chunk)
        except (_Bad, _TooBig):
            raise
        except Exception as exc:  # noqa: BLE001 -- python-multipart's parse errors
            raise _Bad("the form could not be read") from exc

    def finish(self) -> None:
        try:
            self.parser.finalize()
        except Exception as exc:  # noqa: BLE001
            raise _Bad("the form could not be read") from exc
        if not self.ended or self.filename is None:
            raise _Bad("send a file")


class _Duplicate(Exception):
    def __init__(self, book_id: str) -> None:
        super().__init__(book_id)
        self.book_id = book_id


@dataclass(frozen=True)
class Registered:
    book_id: str
    title: str
    world: dict
    filename: str


def _register(cfg: LoreConfig, part: Path, display: str, kind: str, book_id: str,
              world_ref: str | None, limits: Limits) -> Registered:
    """Check, parse, chunk and register the book; the file ends at
    `inbox/<random>.<kind>`. Raises Refused or _Duplicate (nothing kept)."""
    from lore.ingest.chunker import chunk_book
    from lore.ingest.epub import parse_epub
    from lore.ingest.matter import classify_book
    from lore.ingest.models import IngestError
    from lore.ingest.pdf import parse_pdf
    from lore.ingest.registry import ingest_book
    from lore.worlds import WorldError, create_world, delete_world, get_world

    conn = open_db(cfg.sqlite_path)
    try:
        if conn.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone():
            raise _Duplicate(book_id)
    finally:
        conn.close()
    check_book(part, kind, limits)
    stored = part.with_name(f"{uuid.uuid4().hex}.{kind}")
    part.rename(stored)
    try:
        parse = parse_epub if kind == "epub" else parse_pdf
        try:
            parsed = parse(stored)
            if parsed.title == stored.stem:  # no title in the file: its own name
                parsed = replace(parsed, title=display.rsplit(".", 1)[0] or display)
            chunks = chunk_book(parsed, book_id)
            sections = classify_book(parsed)
        except IngestError as exc:
            why = str(exc).replace(stored.name, display)
            raise Refused("not_a_book", why) from None
        except Exception:  # noqa: BLE001 -- a parser crash on a hostile file
            raise Refused("not_a_book", "this book could not be read") from None
        if not chunks:
            raise Refused("not_a_book", "this book has no text LORE can read")
        conn = open_db(cfg.sqlite_path)
        created: int | None = None
        try:
            world_id: int | None = None
            ref = (world_ref or "").strip()
            if ref:
                digits = ref.isascii() and ref.isdigit()
                found = get_world(conn, int(ref)) if digits else get_world(conn, ref)
                if found is None:
                    if digits:
                        raise Refused("unknown_world", f"no world with id {ref}")
                    try:
                        created = create_world(conn, ref)
                    except WorldError as exc:
                        raise Refused("invalid_world", str(exc)) from None
                    found = get_world(conn, created)
                world_id = found.id
            fresh = ingest_book(
                conn, book_id=book_id, title=parsed.title, author=parsed.author,
                source="upload", file_path=str(stored), chunks=chunks,
                series=parsed.series, series_index=parsed.series_index,
                world_id=world_id, sections=sections)
            if not fresh:  # another upload or `lore ingest` won the race
                raise _Duplicate(book_id)
            created = None  # in use now
            row = conn.execute(
                "SELECT w.id, w.name FROM books b JOIN worlds w ON w.id = b.world_id"
                " WHERE b.id = ?", (book_id,)).fetchone()
        finally:
            if created is not None:
                try:
                    delete_world(conn, created)
                except (WorldError, sqlite3.Error):
                    pass
            conn.close()
    except BaseException:
        stored.unlink(missing_ok=True)
        raise
    return Registered(book_id, parsed.title,
                      {"id": row[0], "name": row[1]} if row else None, display)


@dataclass
class UploadDeps:
    multi: bool
    current_user: Callable[[Request], CurrentUser]
    library_of: Callable[[CurrentUser], LoreConfig]
    needs_key: Callable[[CurrentUser], JSONResponse | None]
    central: Callable[[], object]  # a sessionmaker: the rate and quota counters
    start: Callable[[CurrentUser, LoreConfig, str], dict]
    now: Callable[[], datetime]
    settings: UploadSettings


def _book_count(cfg: LoreConfig) -> int:
    conn = open_db(cfg.sqlite_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    finally:
        conn.close()


def _quota_refusal(deps: UploadDeps, user: CurrentUser, cfg: LoreConfig) -> JSONResponse | None:
    """Multi mode: the attempt rate, the daily quota and the library size."""
    from lore.auth import ratelimit
    from lore.central.models import RateHit

    s, now = deps.settings, deps.now()
    with deps.central()() as db:
        if not ratelimit.allow(db, RATE_ATTEMPT, user.id, s.per_hour, now):
            db.rollback()
            return _error(429, "rate_limited", "too many uploads at once; try again in an hour")
        db.commit()
        added = db.scalar(sa.select(sa.func.count()).select_from(RateHit).where(
            RateHit.key == ratelimit._key(RATE_ADDED, user.id), RateHit.at > now - DAY))
    if added >= s.per_day:
        return _error(429, "daily_limit", f"you can add {s.per_day} books a day")
    if _book_count(cfg) >= s.max_books:
        return _error(409, "too_many_books",
                      f"your library holds at most {s.max_books} books")
    return None


def _record_added(deps: UploadDeps, user: CurrentUser) -> None:
    from lore.central.models import RateHit
    from lore.auth import ratelimit

    with deps.central()() as db:
        db.add(RateHit(key=ratelimit._key(RATE_ADDED, user.id), at=deps.now()))
        db.commit()


def build_upload_router(deps: UploadDeps) -> APIRouter:
    router = APIRouter()
    locks: dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()

    @router.post(UPLOAD_PATH)
    async def upload(request: Request):
        user = deps.current_user(request)
        cfg = deps.library_of(user)
        kind_header, options = parse_options_header(request.headers.get("content-type", ""))
        boundary = options.get(b"boundary")
        if kind_header != b"multipart/form-data" or not boundary or len(boundary) > 200:
            return _error(400, "bad_upload", "send the book as multipart/form-data")
        if deps.multi and (refused := deps.needs_key(user)) is not None:
            return refused
        with locks_guard:
            lock = locks.setdefault(user.id, threading.Lock())
        if not lock.acquire(blocking=False):
            return _error(409, "busy", "another upload is still in progress")
        try:
            if deps.multi:
                refused = await asyncio.to_thread(_quota_refusal, deps, user, cfg)
                if refused is not None:
                    return refused
            return await _receive(request, user, cfg, boundary)
        finally:
            lock.release()

    async def _receive(request: Request, user: CurrentUser, cfg: LoreConfig,
                       boundary: bytes):
        inbox = cfg.inbox_dir
        inbox.mkdir(parents=True, exist_ok=True)
        if not inbox.resolve().is_relative_to(cfg.data_dir.resolve()):
            return _error(500, "server_error", "the library's inbox is misplaced")
        part = inbox / f".upload-{uuid.uuid4().hex}.part"
        form = _Form(boundary, part, deps.settings.max_file_bytes)
        keep = False
        try:
            try:
                async for chunk in request.stream():
                    form.write(chunk)
                form.finish()
            except (TooLarge, _TooBig):
                return _error(413, "too_large",
                              f"a book can be at most {deps.settings.max_file_bytes // (1024 * 1024)} MB")
            except _Bad as exc:
                return _error(400, "bad_upload", str(exc))
            except ClientDisconnect:
                return _error(400, "bad_upload", "the upload was interrupted")
            finally:
                form.close()
            display = display_name(form.filename)
            suffix = Path(display).suffix.lower()
            kind = _KINDS.get(suffix)
            if kind is None or sniff(form.head) != kind:
                return _error(400, "not_a_book", "only EPUB and PDF books can be added")
            if form.size == 0:
                return _error(400, "not_a_book", "the file is empty")
            book_id = form.hash.hexdigest()[:16]
            try:
                done = await asyncio.to_thread(
                    _register, cfg, part, display, kind, book_id, form.world,
                    deps.settings.checks)
            except _Duplicate as dup:
                return _error(409, "already_added", "this book is already in your library",
                              book_id=dup.book_id)
            except Refused as exc:
                status = 400
                return _error(status, exc.code, exc.message)
            keep = True  # _register moved it to its stored name
            if deps.multi:
                await asyncio.to_thread(_record_added, deps, user)
            processing = await asyncio.to_thread(deps.start, user, cfg, done.book_id)
            return JSONResponse(
                {"book_id": done.book_id, "title": done.title, "world": done.world,
                 "filename": done.filename, "processing": processing},
                status_code=202, headers={"Cache-Control": "no-store"})
        finally:
            if not keep:
                part.unlink(missing_ok=True)

    return router
