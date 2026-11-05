"""Sandboxed parsing of uploads (release step 6, task 5.3): the child's
time, CPU and memory limits, its answer checked, and the web process never
opening the untrusted file itself."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from lore.config import LoreConfig
from lore.ingest import sandbox
from lore.ingest.sandbox import SandboxLimits, parse_untrusted
from lore.ingest.upload_check import Limits, Refused
from lore.web.app import create_app
from lore.web.upload import UploadSettings
from tests.test_web_upload import FakeStage, NOW, epub_bytes, pdf_bytes, send


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "inbox" / "b.epub"
    path.parent.mkdir()
    path.write_bytes(epub_bytes(tmp_path))
    return path


def test_a_real_book_parses_in_the_child(book):
    got = parse_untrusted(book, "epub", "bid", Limits())
    assert got.title == "The Upload" and got.author == "An Author"
    assert got.chunks and all(c.book_id == "bid" for c in got.chunks)
    assert got.sections


def test_a_pdf_too(tmp_path):
    path = tmp_path / "b.pdf"
    path.write_bytes(pdf_bytes(tmp_path))
    assert parse_untrusted(path, "pdf", "p", Limits()).chunks


def test_the_wall_clock_timeout_kills_the_child(book):
    started = time.monotonic()
    with pytest.raises(Refused) as caught:
        parse_untrusted(book, "epub", "b", Limits(), SandboxLimits(wall_seconds=1),
                        _selftest="sleep")
    assert caught.value.code == "too_complex"
    assert time.monotonic() - started < 10


def test_the_cpu_limit_stops_the_child(book):
    with pytest.raises(Refused) as caught:
        parse_untrusted(book, "epub", "b", Limits(),
                        SandboxLimits(cpu_seconds=1, wall_seconds=30), _selftest="spin")
    assert caught.value.code == "too_complex"


def test_the_memory_limit_stops_the_child(book):
    with pytest.raises(Refused) as caught:
        parse_untrusted(book, "epub", "b", Limits(),
                        SandboxLimits(memory_bytes=512 * 1024 * 1024), _selftest="allocate")
    assert caught.value.code == "too_complex"


def test_an_answer_that_is_not_json_is_not_a_book(book):
    with pytest.raises(Refused) as caught:
        parse_untrusted(book, "epub", "b", Limits(), _selftest="garbage")
    assert caught.value.code == "not_a_book"


def test_an_oversized_answer_is_too_complex(book):
    with pytest.raises(Refused) as caught:
        parse_untrusted(book, "epub", "b", Limits(), SandboxLimits(max_output_bytes=100))
    assert caught.value.code == "too_complex"


@pytest.mark.parametrize("answer", [
    None, [], {"ok": True}, {"ok": True, "book": {"title": 3}, "chunks": [], "sections": []},
    {"ok": True, "book": {"title": "t"}, "chunks": [{"id": 1}], "sections": []},
    {"ok": False, "code": "something_else", "message": "x"},
])
def test_a_malformed_answer_is_not_a_book(answer):
    with pytest.raises(Refused) as caught:
        sandbox._rebuild(answer)
    assert caught.value.code == "not_a_book"


def test_refusals_keep_their_codes(tmp_path):
    path = tmp_path / "x.epub"
    path.write_bytes(b"PK\x03\x04 not really a zip")
    with pytest.raises(Refused) as caught:
        parse_untrusted(path, "epub", "x", Limits())
    assert caught.value.code == "not_a_book"


def test_limits_from_the_environment():
    got = SandboxLimits.from_env({"LORE_PARSE_CPU_SECONDS": "5", "LORE_PARSE_MEMORY_MB": "256",
                                  "LORE_PARSE_TIMEOUT_SECONDS": "9"})
    assert (got.cpu_seconds, got.memory_bytes, got.wall_seconds) == (5, 256 * 1024 * 1024, 9.0)
    assert SandboxLimits.from_env({}) == SandboxLimits()
    with pytest.raises(ValueError):
        SandboxLimits.from_env({"LORE_PARSE_MEMORY_MB": "0"})


# --- through the upload route ------------------------------------------------------

def client(tmp_path, **settings):
    cfg = LoreConfig(data_dir=tmp_path / "lib")
    app = create_app(cfg, stages_factory=lambda c: [FakeStage([])], clock=lambda: NOW,
                     upload_settings=UploadSettings(**settings))
    return TestClient(app, base_url="http://127.0.0.1:8766"), cfg


def test_the_web_process_never_opens_the_uploaded_file(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("the web process parsed an untrusted file")
    epub, pdf = epub_bytes(tmp_path), pdf_bytes(tmp_path)  # built before the trap
    import pymupdf
    monkeypatch.setattr(pymupdf, "open", forbidden)
    monkeypatch.setattr("lore.ingest.epub.parse_epub", forbidden)
    monkeypatch.setattr("lore.ingest.pdf.parse_pdf", forbidden)
    monkeypatch.setattr("lore.ingest.upload_check.check_book", forbidden)
    c, _ = client(tmp_path)
    assert send(c, epub).status_code == 202
    assert send(c, pdf, name="b.pdf").status_code == 202


def test_a_parse_that_runs_out_of_time_is_too_complex_and_keeps_nothing(tmp_path, monkeypatch):
    real = sandbox.parse_untrusted

    def slow(*args, **kwargs):
        return real(*args, _selftest="sleep", **kwargs)
    monkeypatch.setattr(sandbox, "parse_untrusted", slow)
    c, cfg = client(tmp_path, sandbox=SandboxLimits(wall_seconds=1))
    r = send(c, epub_bytes(tmp_path))
    assert r.status_code == 400 and r.json()["error"]["code"] == "too_complex"
    assert [p for p in cfg.inbox_dir.iterdir()] == []
