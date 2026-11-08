"""lore.fs: crash-safe writes (temp file, fsync, os.replace)."""
import json
import os
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lore import fs


def test_atomic_write_text_bytes_and_json(tmp_path: Path) -> None:
    fs.atomic_write_text(tmp_path / "a" / "t.txt", "hello")
    fs.atomic_write_bytes(tmp_path / "b.bin", b"\x00\x01")
    fs.atomic_write_json(tmp_path / "c.json", {"k": [1, 2]}, indent=2)
    assert (tmp_path / "a" / "t.txt").read_text() == "hello"
    assert (tmp_path / "b.bin").read_bytes() == b"\x00\x01"
    assert json.loads((tmp_path / "c.json").read_text()) == {"k": [1, 2]}
    assert [p.name for p in tmp_path.rglob("*.tmp")] == []


def test_atomic_write_parquet_round_trips(tmp_path: Path) -> None:
    fs.atomic_write_parquet(tmp_path / "x.parquet", pa.table({"a": [1, 2]}))
    assert pq.read_table(tmp_path / "x.parquet").to_pylist() == [{"a": 1}, {"a": 2}]
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_copy(tmp_path: Path) -> None:
    (tmp_path / "src").write_bytes(b"abc")
    fs.atomic_copy(tmp_path / "src", tmp_path / "out" / "dst")
    assert (tmp_path / "out" / "dst").read_bytes() == b"abc"


def test_writes_are_fsynced_before_the_rename(tmp_path: Path, monkeypatch) -> None:
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(fs.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(fs.os, "replace",
                        lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    fs.atomic_write_text(tmp_path / "f.json", "{}")
    # The file's data first, then the rename, then the directory entry.
    assert order == ["fsync", "replace", "fsync"]


def test_a_crash_before_the_rename_keeps_the_old_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "f.json"
    fs.atomic_write_text(target, "old")

    def crash(a, b):
        raise KeyboardInterrupt("killed between write and rename")

    monkeypatch.setattr(fs.os, "replace", crash)
    with pytest.raises(KeyboardInterrupt):
        fs.atomic_write_text(target, "new")
    assert target.read_text() == "old"
    assert list(tmp_path.glob("*.tmp")) == []  # an exception cleans its own temp


def test_a_stale_temp_from_a_dead_process_is_cleaned_on_the_next_write(
    tmp_path: Path, monkeypatch
) -> None:
    fs._cleaned.clear()
    # A hard kill leaves temps behind: one from a dead pid, one of the old
    # fixed `<name>.tmp` kind, long since abandoned.
    dead = tmp_path / "f.json.999999999.0badcafe.tmp"
    dead.write_text("{")
    legacy = tmp_path / "g.json.tmp"
    legacy.write_text("")
    old = time.time() - 3600
    os.utime(legacy, (old, old))
    mine = tmp_path / f"h.json.{os.getpid()}.deadbeef.tmp"  # a live writer's
    mine.write_text("x")
    unrelated = tmp_path / "notes.tmp"
    unrelated.write_text("keep")

    fs.atomic_write_text(tmp_path / "other.json", "{}")
    assert not dead.exists() and not legacy.exists()
    assert mine.exists() and unrelated.exists()


def test_is_temp_names_lore_temps_only() -> None:
    assert fs.is_temp(Path("a.json.123.0123abcd.tmp"))
    assert fs.is_temp(Path("a.json.tmp"))
    assert not fs.is_temp(Path("a.json"))
    assert not fs.is_temp(Path("notes.tmp"))
