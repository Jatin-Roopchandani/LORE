"""One lock file per library that keeps a book's delete apart from the work
that writes the same stores.

Processing runs (`pipeline.run_book`, so the web app's thread, the worker
and the CLI alike) and uploads hold it SHARED for as long as they run, and
any number of them may. Deleting a book holds it EXCLUSIVE, so a delete
starts only when nothing else is writing the library, and nothing starts
while it runs. Every acquire is non-blocking: whoever finds it taken is
refused (LibraryBusy) and says so, rather than waiting minutes behind an
upload or hours behind a run.

The file is `<library>/.books.lock`, an flock (released by the kernel when
its holder exits, so a crash never leaves the library locked). It is never
copied into a sample template (lore.sample ignores it).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path

LOCK_NAME = ".books.lock"


class LibraryBusy(RuntimeError):
    """Another holder has the library's lock in a conflicting mode."""


@contextlib.contextmanager
def hold(library: Path, *, exclusive: bool):
    library = Path(library)
    library.mkdir(parents=True, exist_ok=True)
    fd = os.open(library / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LibraryBusy("the library is busy") from None
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock
