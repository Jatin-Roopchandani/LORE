"""Worlds: the unit within which entities may be shared across books.

A world holds one or more books (a series, or a standalone book). This
module is rows only; graph and index consequences of moving a book live
elsewhere.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


class WorldError(ValueError):
    """A world operation that cannot be done (duplicate name, not empty ...)."""


@dataclass(frozen=True)
class World:
    id: int
    name: str
    created_at: str


def _row(r) -> World:
    return World(id=r[0], name=r[1], created_at=r[2])


def list_worlds(conn: sqlite3.Connection) -> list[World]:
    return [_row(r) for r in conn.execute(
        "SELECT id, name, created_at FROM worlds ORDER BY id")]


def get_world(conn: sqlite3.Connection, ref: int | str) -> World | None:
    """A world by id, or by name (ignoring case); None when there is none."""
    if isinstance(ref, int):
        r = conn.execute("SELECT id, name, created_at FROM worlds WHERE id = ?",
                         (ref,)).fetchone()
    else:
        r = conn.execute(
            "SELECT id, name, created_at FROM worlds WHERE name = ? COLLATE NOCASE",
            (ref.strip(),)).fetchone()
    return _row(r) if r else None


def _clean(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise WorldError("a world needs a name")
    return name


def create_world(conn: sqlite3.Connection, name: str) -> int:
    name = _clean(name)
    if get_world(conn, name) is not None:
        raise WorldError(f"a world named {name!r} already exists")
    cur = conn.execute("INSERT INTO worlds (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def rename_world(conn: sqlite3.Connection, world_id: int, name: str) -> None:
    name = _clean(name)
    if get_world(conn, world_id) is None:
        raise WorldError(f"no world with id {world_id}")
    clash = get_world(conn, name)
    if clash is not None and clash.id != world_id:
        raise WorldError(f"a world named {name!r} already exists")
    conn.execute("UPDATE worlds SET name = ? WHERE id = ?", (name, world_id))
    conn.commit()


def delete_world(conn: sqlite3.Connection, world_id: int) -> None:
    if get_world(conn, world_id) is None:
        raise WorldError(f"no world with id {world_id}")
    if books_in(conn, world_id):
        raise WorldError("world is not empty; move its books out first")
    conn.execute("DELETE FROM worlds WHERE id = ?", (world_id,))
    conn.commit()


def world_of(conn: sqlite3.Connection, book_id: str) -> World | None:
    r = conn.execute(
        "SELECT w.id, w.name, w.created_at FROM books b"
        " JOIN worlds w ON w.id = b.world_id WHERE b.id = ?", (book_id,)).fetchone()
    return _row(r) if r else None


def books_in(conn: sqlite3.Connection, world_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM books WHERE world_id = ? ORDER BY created_at, rowid",
        (world_id,))]


def assign(conn: sqlite3.Connection, book_id: str, world_id: int) -> bool:
    """Point a book at a world (the row only). False when there is no such book."""
    if get_world(conn, world_id) is None:
        raise WorldError(f"no world with id {world_id}")
    cur = conn.execute("UPDATE books SET world_id = ? WHERE id = ?", (world_id, book_id))
    conn.commit()
    return cur.rowcount > 0


def suggest_world(conn: sqlite3.Connection, series: str | None,
                  title: str) -> tuple[int | None, str]:
    """The default world for a new book: (world_id or None, name).

    1. its series already lives in a world: that world (series match when
       trimmed and case-folded, as migration 9 groups them);
    2. else it has a series: a new world named after the series;
    3. else a new world named after the title.
    """
    series = (series or "").strip()
    if series:
        r = conn.execute(
            "SELECT w.id, w.name FROM books b JOIN worlds w ON w.id = b.world_id"
            " WHERE lower(trim(b.series)) = lower(?) ORDER BY b.rowid LIMIT 1",
            (series,)).fetchone()
        if r:
            return r[0], r[1]
        return None, series
    return None, title.strip()


# ---- reconcile ------------------------------------------------------------


@dataclass
class Merge:
    """One group of worlds folded into `kept`."""
    kept: str
    absorbed: list[str]
    books: list[str]  # the books that changed world


@dataclass
class ReconcileReport:
    merges: list[Merge] = field(default_factory=list)

    @property
    def merged(self) -> int:
        return sum(len(m.absorbed) for m in self.merges)


def reconcile_worlds_from_graph(conn: sqlite3.Connection, kuzu) -> ReconcileReport:
    """Merge the worlds of books that share an entity in the graph.

    A library graphed before worlds existed may have fused an entity across
    books that the migration then put in different worlds. Books joined by a
    shared `Entity.books` (union-find over those lists; ids the registry does
    not know are ignored) must live in one world, so the worlds of each
    connected group are merged into one.

    The world kept is the one holding the most books; a tie goes to a world
    with a series, then to the lowest id. The others' books move to it, and
    the emptied worlds are deleted. Rows only: no graph change. Idempotent;
    a second call finds nothing to merge.
    """
    world_of_book = {r[0]: r[1] for r in conn.execute(
        "SELECT id, world_id FROM books WHERE world_id IS NOT NULL")}
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (books,) in kuzu.execute("MATCH (e:Entity) RETURN e.books"):
        ids = sorted({world_of_book[b] for b in (books or ()) if b in world_of_book})
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])
    groups: dict[int, list[int]] = {}
    for w in list(parent):
        groups.setdefault(find(w), []).append(w)

    report = ReconcileReport()
    for members in groups.values():
        if len(members) < 2:
            continue
        marks = ",".join("?" * len(members))
        size = {r[0]: r[1] for r in conn.execute(
            f"SELECT world_id, COUNT(*) FROM books WHERE world_id IN ({marks})"
            " GROUP BY world_id", members)}
        has_series = {r[0] for r in conn.execute(
            f"SELECT DISTINCT world_id FROM books WHERE world_id IN ({marks})"
            " AND trim(coalesce(series, '')) <> ''", members)}
        kept = min(members, key=lambda w: (-size.get(w, 0), w not in has_series, w))
        absorbed = sorted(w for w in members if w != kept)
        marks = ",".join("?" * len(absorbed))
        moved = [r[0] for r in conn.execute(
            f"SELECT id FROM books WHERE world_id IN ({marks}) ORDER BY rowid",
            absorbed)]
        names = {w: get_world(conn, w).name for w in members}
        with conn:
            conn.execute(
                f"UPDATE books SET world_id = ? WHERE world_id IN ({marks})",
                [kept, *absorbed])
            conn.execute(f"DELETE FROM worlds WHERE id IN ({marks})", absorbed)
        report.merges.append(Merge(kept=names[kept],
                                   absorbed=[names[w] for w in absorbed],
                                   books=moved))
    return report


def ensure_reconciled(conn: sqlite3.Connection, kuzu) -> ReconcileReport | None:
    """Run `reconcile_worlds_from_graph` once per library (the first graph
    open after migration 9), then record it in `world_reconcile`. None when it
    already ran."""
    if conn.execute("SELECT 1 FROM world_reconcile").fetchone():
        return None
    report = reconcile_worlds_from_graph(conn, kuzu)
    with conn:
        conn.execute("INSERT OR IGNORE INTO world_reconcile (id, merged) VALUES (1, ?)",
                     (report.merged,))
    return report
