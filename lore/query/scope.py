"""Which books a query may read: a world's, or every world's one at a time.

A world is the unit within which entities are shared across books, so a
question is answered inside one world at a time. `resolve_scopes` turns the
optional `world` argument (id or name) into the scopes to search:

- a named world: that one scope;
- no world, one world in the library: that world (spec 7);
- no world (or "all"), several: "all worlds", one scope per world, so each
  world is searched on its own and entities are never merged across them (a
  book with no world, which only a half-migrated library has, is its own scope);
- no worlds at all (a library from before worlds): one unscoped scope.

A scope whose books are the whole library is unscoped (`books=None`): it
filters nothing, so a one-world library takes exactly the code path it took
before worlds existed.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from lore.worlds import World, list_worlds

ALL_WORLDS = "all"


class UnknownWorld(ValueError):
    """`world` names no world in the library."""


@dataclass(frozen=True)
class Scope:
    """`books` None means the whole library; otherwise the books it may read."""

    books: frozenset[str] | None = None
    world_id: int | None = None
    world: str | None = None

    def allows(self, book_id: str) -> bool:
        return self.books is None or book_id in self.books

    def allows_any(self, book_ids: Iterable[str]) -> bool:
        return self.books is None or any(b in self.books for b in book_ids)

    def book_ids(self) -> list[str]:
        """The books as a sorted list; [] for the whole library."""
        return sorted(self.books) if self.books else []

    def tag(self) -> dict | None:
        """The `{id, name}` a node carries in all-worlds mode."""
        return None if self.world_id is None else {"id": self.world_id, "name": self.world}


def _find(worlds: list[World], ref: int | str) -> World | None:
    text = str(ref).strip()
    if isinstance(ref, int) or text.isdigit():
        found = next((w for w in worlds if w.id == int(text)), None)
        if found is not None:
            return found
    return next((w for w in worlds if w.name.casefold() == text.casefold()), None)


def _blank(world) -> bool:
    return world is None or (isinstance(world, str) and not world.strip())


def resolve_scopes(conn: sqlite3.Connection, world: int | str | None = None) -> list[Scope]:
    """The scopes to search, in world-id order (see the module docstring).
    Raises UnknownWorld for a `world` that is not one of the library's."""
    try:
        worlds = list_worlds(conn)
    except sqlite3.OperationalError:  # a library from before worlds
        worlds = []
    everything = {r[0] for r in conn.execute("SELECT id FROM books")}
    explicit = not _blank(world) and str(world).strip().casefold() != ALL_WORLDS
    if explicit:
        found = _find(worlds, world)
        if found is None:
            raise UnknownWorld(f"no world {world!r}"
                               + (f"; the worlds are {', '.join(w.name for w in worlds)}"
                                  if worlds else ""))
        scopes = [Scope(frozenset(_books_of(conn, found.id)), found.id, found.name)]
    elif not worlds:
        return [Scope()]
    else:
        # Every world, each on its own; a book with no world is its own scope.
        scopes = [Scope(frozenset(_books_of(conn, w.id)), w.id, w.name) for w in worlds]
        scopes = [s for s in scopes if s.books] or scopes[:1]
        orphans = [r[0] for r in conn.execute("SELECT id FROM books WHERE world_id IS NULL")]
        if orphans:
            scopes.append(Scope(frozenset(orphans)))
    if len(scopes) == 1 and scopes[0].books >= everything:
        only = scopes[0]
        return [Scope(None, only.world_id, only.world)]
    return scopes


def _books_of(conn: sqlite3.Connection, world_id: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id FROM books WHERE world_id = ?", (world_id,))]


def union(scopes: list[Scope]) -> frozenset[str] | None:
    """Every book any scope may read; None when one of them reads them all."""
    if any(s.books is None for s in scopes):
        return None
    return frozenset().union(*(s.books for s in scopes))


def world_books(conn: sqlite3.Connection, world: int | str | None) -> frozenset[str] | None:
    """The books a read-only lookup may use: the named world's, or None (no
    filter) when no world is named. Raises UnknownWorld."""
    if _blank(world) or str(world).strip().casefold() == ALL_WORLDS:
        return None
    return union(resolve_scopes(conn, world))
