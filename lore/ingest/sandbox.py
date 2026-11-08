"""Parsing an uploaded book in a sandboxed subprocess (release step 6, task 5.3).

An uploaded EPUB or PDF is untrusted, and its parsers are big (MuPDF is C).
So the web process never opens one itself: `parse_untrusted` runs
`python -I -m lore.ingest.sandbox` on it, and the child, before it reads a
byte, limits itself:

- CPU time (RLIMIT_CPU, `cpu_seconds`): past it the kernel stops the child;
- address space (RLIMIT_AS, `memory_bytes`, 1 GiB by default): an allocation
  past it fails with MemoryError;
- no core dumps;
and the parent adds a wall-clock timeout (`wall_seconds`) and a cap on the
answer's size, killing the child's whole process group past either.

The child runs every check (lore.ingest.upload_check: zip structure, bombs,
traversal, DRM, encrypted PDFs, page count), then parses, chunks and
classifies, and prints the result as JSON (never pickle: a child that a
hostile file took over must not be able to run code in the parent). The
parent checks every field's type as it rebuilds the dataclasses.

Failures: a refusal from the checks keeps its code (`not_a_book`,
`unsafe_file`, `encrypted`, `too_many_pages`); a parser error or crash, or
an answer that is not what it should be, is `not_a_book`; running out of
time or memory is `too_complex`.

| Limit | Default | Variable |
|---|---|---|
| CPU seconds | 60 | LORE_PARSE_CPU_SECONDS |
| address space | 1024 MB | LORE_PARSE_MEMORY_MB |
| wall-clock seconds | 120 | LORE_PARSE_TIMEOUT_SECONDS |
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from lore.ingest.matter import Classified
from lore.ingest.models import Chunk
from lore.ingest.upload_check import Limits, Refused

ENV_CPU = "LORE_PARSE_CPU_SECONDS"
ENV_MEMORY = "LORE_PARSE_MEMORY_MB"
ENV_TIMEOUT = "LORE_PARSE_TIMEOUT_SECONDS"
MAX_OUTPUT = 768 * 1024 * 1024  # the answer: chunk text is at most the book's own text


@dataclass(frozen=True)
class SandboxLimits:
    cpu_seconds: int = 60
    memory_bytes: int = 1024 * 1024 * 1024
    wall_seconds: float = 120.0
    max_output_bytes: int = MAX_OUTPUT

    @classmethod
    def from_env(cls, env=None) -> "SandboxLimits":
        env = os.environ if env is None else env

        def num(name: str, default: int) -> int:
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(f"{name} must be a whole number") from None
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            return value

        return cls(cpu_seconds=num(ENV_CPU, 60),
                   memory_bytes=num(ENV_MEMORY, 1024) * 1024 * 1024,
                   wall_seconds=float(num(ENV_TIMEOUT, 120)))


@dataclass(frozen=True)
class Parsed:
    title: str
    author: str | None
    series: str | None
    series_index: float | None
    chunks: list[Chunk]
    sections: list[Classified]


TOO_COMPLEX = Refused("too_complex", "this book is too large or complex to read safely")
UNREADABLE = Refused("not_a_book", "this book could not be read")


def parse_untrusted(path: Path, kind: str, book_id: str, checks: Limits,
                    limits: SandboxLimits | None = None, *,
                    _selftest: str | None = None) -> Parsed:
    """Check and parse `path` in a limited child process. Raises Refused."""
    limits = limits or SandboxLimits()
    request = {"path": str(path), "kind": kind, "book_id": book_id,
               "checks": asdict(checks),
               "cpu": limits.cpu_seconds, "memory": limits.memory_bytes}
    if _selftest:  # tests only: make the child misbehave on purpose
        request["selftest"] = _selftest
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "lore.ingest.sandbox"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=str(path.parent), start_new_session=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"})
    out = bytearray()
    over = threading.Event()

    def read() -> None:
        while True:
            chunk = proc.stdout.read(1 << 16)
            if not chunk:
                return
            if len(out) + len(chunk) > limits.max_output_bytes:
                over.set()
                _kill(proc)
                return
            out.extend(chunk)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        proc.stdin.write(json.dumps(request).encode())
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=limits.wall_seconds)
    except subprocess.TimeoutExpired:
        _kill(proc)
        proc.wait()
        reader.join(5)
        raise TOO_COMPLEX from None
    reader.join(5)
    if over.is_set():
        raise TOO_COMPLEX
    if proc.returncode in (-signal.SIGXCPU, -signal.SIGKILL):
        raise TOO_COMPLEX
    try:
        answer = json.loads(bytes(out))
    except ValueError:
        raise UNREADABLE from None
    return _rebuild(answer)


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _str(v) -> str:
    if not isinstance(v, str):
        raise TypeError
    return v


def _opt_str(v) -> str | None:
    return None if v is None else _str(v)


def _int(v) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise TypeError
    return v


def _rebuild(answer) -> Parsed:
    if not isinstance(answer, dict):
        raise UNREADABLE
    if answer.get("ok") is not True:
        code, message = answer.get("code"), answer.get("message")
        if code in ("not_a_book", "unsafe_file", "encrypted", "too_many_pages", "too_complex") \
                and isinstance(message, str):
            raise Refused(code, message[:500])
        raise UNREADABLE
    try:
        book = answer["book"]
        index = book.get("series_index")
        if index is not None and (not isinstance(index, (int, float)) or isinstance(index, bool)):
            raise TypeError
        chunks = [Chunk(id=_str(c["id"]), book_id=_str(c["book_id"]),
                        chapter_seq=_int(c["chapter_seq"]), scene_seq=_int(c["scene_seq"]),
                        seq=_int(c["seq"]), text=_str(c["text"]))
                  for c in answer["chunks"]]
        sections = [Classified(seq=_int(s["seq"]), matter=_str(s["matter"]),
                               reason=_str(s["reason"]), heading=_opt_str(s["heading"]))
                    for s in answer["sections"]]
        return Parsed(_str(book["title"]), _opt_str(book.get("author")),
                      _opt_str(book.get("series")), index, chunks, sections)
    except (KeyError, TypeError, AttributeError):
        raise UNREADABLE from None


# --- the child ------------------------------------------------------------------

def _limit_self(cpu: int, memory: int) -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))


def _child() -> dict:
    request = json.loads(sys.stdin.buffer.read())
    _limit_self(int(request["cpu"]), int(request["memory"]))
    selftest = request.get("selftest")
    if selftest == "sleep":
        import time
        time.sleep(3600)
    elif selftest == "spin":
        while True:
            pass
    elif selftest == "allocate":
        _ = bytearray(64 * 1024 * 1024 * 1024)
    elif selftest == "garbage":
        sys.stdout.write("not json")
        sys.exit(0)

    from lore.ingest.chunker import chunk_book
    from lore.ingest.epub import parse_epub
    from lore.ingest.matter import classify_book
    from lore.ingest.models import IngestError
    from lore.ingest.pdf import parse_pdf
    from lore.ingest.upload_check import check_book

    path, kind = Path(request["path"]), request["kind"]
    try:
        check_book(path, kind, Limits(**request["checks"]))
    except Refused as exc:
        return {"ok": False, "code": exc.code, "message": exc.message}
    try:
        parsed = (parse_epub if kind == "epub" else parse_pdf)(path)
        chunks = chunk_book(parsed, request["book_id"])
        sections = classify_book(parsed)
    except IngestError as exc:
        return {"ok": False, "code": "not_a_book", "message": str(exc).replace(str(path), path.name)}
    return {"ok": True,
            "book": {"title": parsed.title, "author": parsed.author, "series": parsed.series,
                     "series_index": parsed.series_index},
            "chunks": [asdict(c) for c in chunks],
            "sections": [asdict(s) for s in sections]}


def main() -> None:
    try:
        answer = _child()
    except MemoryError:
        answer = {"ok": False, "code": "too_complex", "message": "out of memory"}
    except Exception:  # noqa: BLE001 -- a parser crash on a hostile file
        answer = {"ok": False, "code": "not_a_book", "message": "this book could not be read"}
    try:
        sys.stdout.write(json.dumps(answer))
        sys.stdout.flush()
    except MemoryError:
        os._exit(3)


if __name__ == "__main__":
    main()
