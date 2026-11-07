"""Crash-safe file writes for every artifact and cache LORE keeps on disk.

A plain `write_text`, or even write-temp-then-rename without an fsync, is
not enough: when the machine (or WSL) is killed, the rename can reach the
disk before the data does, and the file comes back 0 bytes. So every write
here goes:

  1. to a temp file in the SAME directory (`<name>.<pid>.<hex8>.tmp`, so two
     processes writing one file never share a temp),
  2. flush + fsync of that file,
  3. `os.replace` onto the real name (atomic on POSIX),
  4. fsync of the directory, so the rename itself is durable.

A reader therefore sees the old file or the new one, never a half one.

A temp left by a hard kill is never read (readers open real names only)
and is removed by the next write into its directory: a temp whose pid is
dead, one older than a day, or one of the old fixed `<name>.tmp` kind
older than ten minutes. Each directory is swept once per process.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

_TEMP = re.compile(r"^.+\.(\d+)\.[0-9a-f]{8}\.tmp$")
_LEGACY_TEMP = re.compile(r"^.+\.(json|jsonl|attempts|parquet)\.tmp$")
_DAY = 86400.0
_LEGACY_AGE = 600.0

# Directories already swept by this process.
_cleaned: set[str] = set()


def is_temp(path: Path) -> bool:
    """Whether `path` is one of LORE's write temps (current or older kind)."""
    name = Path(path).name
    return bool(_TEMP.match(name) or _LEGACY_TEMP.match(name))


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, OSError):
        return True
    return True


def clean_stale_temps(directory: Path) -> None:
    """Remove temps a crashed writer left in `directory`. Best effort."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    now = time.time()
    for entry in entries:
        name = entry.name
        if not name.endswith(".tmp"):
            continue
        try:
            age = now - entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        match = _TEMP.match(name)
        if match:
            stale = age > _DAY or not _pid_alive(int(match.group(1)))
        elif _LEGACY_TEMP.match(name):
            stale = age > _LEGACY_AGE
        else:
            continue
        if stale:
            try:
                os.unlink(entry.path)
            except OSError:
                pass


def _sweep_once(directory: Path) -> None:
    key = str(directory)
    if key not in _cleaned:
        _cleaned.add(key)
        clean_stale_temps(directory)


def fsync_dir(directory: Path) -> None:
    """Make a rename in `directory` durable. A no-op where unsupported."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _temp_for(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")


def _commit(temporary: Path, path: Path) -> None:
    os.replace(temporary, path)
    fsync_dir(path.parent)


def _write_via(path: Path, fill) -> None:
    """Shared body: `fill(temp_path)` writes and fsyncs the temp's content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _sweep_once(path.parent)
    temporary = _temp_for(path)
    try:
        fill(temporary)
        _commit(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    def fill(temporary: Path) -> None:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    _write_via(path, fill)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path, obj: object, **dumps_kwargs) -> None:
    atomic_write_text(path, json.dumps(obj, **dumps_kwargs))


def atomic_write_parquet(path: Path, table) -> None:
    """`pq.write_table` to a temp, fsync it, then rename into place."""
    import pyarrow.parquet as pq

    def fill(temporary: Path) -> None:
        pq.write_table(table, str(temporary))
        fd = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    _write_via(path, fill)


def atomic_copy(source: Path, dest: Path) -> None:
    """Copy `source` to `dest` with the same guarantees as a write."""
    atomic_write_bytes(dest, Path(source).read_bytes())
