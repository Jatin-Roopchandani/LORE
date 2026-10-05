import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.sqlite import schema_version
from lore.mcp import stores as stores_module
from lore.mcp.guards import TIMEOUT_MS
from lore.mcp.stores import StoresMissing, StoresUnavailable, open_stores
from tests.mcp_fixtures import build_store_triple


@pytest.fixture
def store_config(tmp_path: Path) -> LoreConfig:
    """A small but real store triple, built the way the CLI builds them."""
    return build_store_triple(tmp_path, entities=[
        ("e1", "Holmes", "Character", [], []),
        ("e2", "Watson", "Character", [], []),
    ])


def test_open_stores_yields_all_three(store_config):
    with open_stores(store_config) as stores:
        assert stores.conn.execute("SELECT 1").fetchone() == (1,)
        assert list(stores.kuzu.execute("MATCH (e:Entity) RETURN count(e)"))
        assert stores.lance.table_names()


def test_stores_are_closed_on_exit(store_config):
    with open_stores(store_config) as stores:
        conn = stores.conn
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_missing_graph_raises_stores_missing(tmp_path):
    with pytest.raises(StoresMissing, match="lore graph load"):
        with open_stores(LoreConfig(data_dir=tmp_path / "empty")):
            pass


@pytest.mark.parametrize("victim", ["lore.db", "lancedb"])
def test_any_one_missing_store_raises_stores_missing(store_config, victim):
    import shutil
    target = store_config.data_dir / victim
    shutil.rmtree(target) if target.is_dir() else target.unlink()
    with pytest.raises(StoresMissing):
        with open_stores(store_config):
            pass


def test_the_graph_is_opened_read_only(store_config):
    with open_stores(store_config) as stores:
        with pytest.raises(RuntimeError, match="read-only"):
            stores.kuzu.execute("CREATE (n:Entity {id: 'x', name: 'x'})")


def test_sqlite_is_not_migrated(store_config):
    # A read-only tool must not migrate the library as a side effect.
    conn = sqlite3.connect(store_config.sqlite_path)
    conn.execute("DELETE FROM schema_migrations WHERE version = ?",
                 (schema_version(conn),))
    conn.commit()
    before = schema_version(conn)
    conn.close()
    with open_stores(store_config):
        pass
    conn = sqlite3.connect(store_config.sqlite_path)
    assert schema_version(conn) == before
    conn.close()


def test_the_query_timeout_is_applied(store_config, monkeypatch):
    seen = []
    real = KuzuStore.set_query_timeout
    monkeypatch.setattr(KuzuStore, "set_query_timeout",
                        lambda self, ms: (seen.append(ms), real(self, ms))[1])
    with open_stores(store_config):
        pass
    assert seen == [TIMEOUT_MS]


# Review Focus 5: two requests in flight at once.
def test_two_open_stores_coexist(store_config):
    with open_stores(store_config) as first:
        with open_stores(store_config) as second:
            assert list(first.kuzu.execute("MATCH (e:Entity) RETURN count(e)"))
            assert list(second.kuzu.execute("MATCH (e:Entity) RETURN count(e)"))


def _writer_opens(path: Path) -> subprocess.CompletedProcess:
    # A separate process, because that is who `lore graph load` is: the file
    # lock is per-process, so an in-process KuzuStore would never be refused.
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from pathlib import Path;"
         "from lore.db.kuzu_store import KuzuStore;"
         "KuzuStore(Path(sys.argv[1])).close()", str(path)],
        capture_output=True, text=True, timeout=60,
    )


# The regression guard for the whole design decision.
def test_a_writer_can_open_the_graph_between_requests(store_config):
    path = store_config.kuzu_dir / "graph"
    # Control: while a request is in flight the writer IS locked out. Without
    # this, the assertion below could pass simply because nothing ever locks.
    with open_stores(store_config) as stores:
        held = _writer_opens(path)
        assert held.returncode != 0
        assert "lock" in held.stderr.lower()
    # After the request, the lock is gone. `stores` is deliberately still
    # referenced: otherwise refcounting would free the handle and pass this
    # even if open_stores never closed it.
    released = _writer_opens(path)
    assert released.returncode == 0, released.stderr
    assert stores.kuzu is not None


def test_the_lock_is_released_when_the_body_raises(store_config):
    with pytest.raises(ValueError):
        with open_stores(store_config):
            raise ValueError("tool blew up")
    assert _writer_opens(store_config.kuzu_dir / "graph").returncode == 0


def test_the_lock_is_released_when_a_later_open_fails(store_config, monkeypatch):
    # Kuzu opens, then LanceDB fails: the Kuzu handle must still be closed.
    def boom(path):
        raise OSError("lance exploded")
    monkeypatch.setattr(stores_module, "LanceStore", boom)
    with pytest.raises(OSError, match="lance exploded"):
        with open_stores(store_config):
            pass
    assert _writer_opens(store_config.kuzu_dir / "graph").returncode == 0


def test_kuzu_store_close_is_idempotent_enough_to_release_the_lock(tmp_path):
    store = KuzuStore(tmp_path / "g")
    store.close()
    KuzuStore(tmp_path / "g").close()


def test_a_rebuild_holding_the_lock_raises_stores_unavailable(store_config):
    path = store_config.kuzu_dir / "graph"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; from pathlib import Path;"
         "from lore.db.kuzu_store import KuzuStore;"
         "s = KuzuStore(Path(sys.argv[1])); print('ready', flush=True);"
         "sys.stdin.readline(); s.close()", str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        # Block until the writer really holds the lock: no race with exit.
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(StoresUnavailable) as info:
            with open_stores(store_config):
                pass
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=60)
    assert "rebuild" in str(info.value)
    assert isinstance(info.value, StoresMissing)
    assert isinstance(info.value.__cause__, RuntimeError)
    assert str(info.value.__cause__) in str(info.value)


def test_a_corrupt_graph_raises_stores_unavailable(store_config):
    path = store_config.kuzu_dir / "graph"
    path.unlink() if path.is_file() else __import__("shutil").rmtree(path)
    path.write_bytes(b"not a kuzu database" * 100)
    with pytest.raises(StoresUnavailable):
        with open_stores(store_config):
            pass


def test_the_lock_is_released_when_set_query_timeout_raises(store_config, monkeypatch):
    def boom(self, ms):
        raise ValueError("timeout refused")
    monkeypatch.setattr(KuzuStore, "set_query_timeout", boom)
    with pytest.raises(ValueError, match="timeout refused"):
        with open_stores(store_config):
            pass
    assert _writer_opens(store_config.kuzu_dir / "graph").returncode == 0


def test_a_body_error_is_not_rewrapped(store_config):
    with pytest.raises(RuntimeError, match="from the tool") as info:
        with open_stores(store_config):
            raise RuntimeError("from the tool")
    assert not isinstance(info.value, StoresMissing)
