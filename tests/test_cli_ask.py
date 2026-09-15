"""`lore ask`: the gate, the output, the opt-in Gemini path and its key.

Every test isolates HOME so the real ~/.config/lore/gemini.env is never read.
"""
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from lore.cli import app
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.db.sqlite import open_db
from lore.query.models import Answer, Passage

runner = CliRunner()
SECRET = "AIza-not-a-real-key-0000"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _library(root: Path, *, graphed: bool, status: str = "chunked") -> None:
    data = root / "data"
    conn = open_db(data / "lore.db")
    conn.execute("INSERT INTO books (id, title, author, source, file_path, status)"
                 " VALUES ('b1', 'A Study in Scarlet', NULL, 'upload', '/x', ?)", (status,))
    conn.commit()
    conn.close()
    LanceStore(data / "lancedb")  # a load always leaves both vector tables
    kuzu = KuzuStore(data / "kuzu" / "graph")
    if graphed:
        kuzu.execute("CREATE (:Book {id: 'b1', title: 'A Study in Scarlet', author: ''})")
    kuzu.close()


def _gemini_toml(root: Path) -> None:
    (root / "lore.toml").write_text('[lore.synthesis]\nprovider = "gemini"\n')


ANSWER = Answer(
    text="Holmes opposes Moriarty [1].",
    passages=(Passage(n=1, chunk_id="b1:1:0", book_id="b1", chapter_seq=7, text="x"),
              Passage(n=2, chunk_id="zz:2:0", book_id="zz", chapter_seq=2, text="y")),
    mode="graph", template="by_predicate", note=None,
    model="qwen2.5:3b-instruct-q4_K_M",
)


def _returning(answer):
    calls = []

    async def fake_answer(*args, **kwargs):
        calls.append((args, kwargs))
        return answer

    return fake_answer, calls


# --- the queryable-book gate ------------------------------------------------

def test_ask_refuses_when_no_book_is_in_the_graph(env: Path) -> None:
    # status says 'graphed' but the graph holds no Book: ground truth wins.
    _library(env, graphed=False, status="graphed")
    fake, calls = _returning(ANSWER)
    with patch("lore.cli.answer_question", fake):
        result = runner.invoke(app, ["ask", "who is holmes?"])
    assert result.exit_code == 1
    assert "lore process" in result.output
    assert calls == []


def test_ask_refuses_an_empty_data_dir_without_creating_it(env: Path) -> None:
    result = runner.invoke(app, ["ask", "who is holmes?"])
    assert result.exit_code == 1
    assert "lore process" in result.output
    assert not (env / "data").exists()


def test_a_book_in_the_graph_is_queryable_whatever_its_status(env: Path) -> None:
    # The real corpus: both graphed novels still read 'chunked', loaded
    # before `process` set 'graphed'. The gate must not depend on a backfill.
    _library(env, graphed=True, status="chunked")
    fake, calls = _returning(ANSWER)
    with patch("lore.cli.answer_question", fake):
        result = runner.invoke(app, ["ask", "q"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


# --- output -----------------------------------------------------------------

def test_ask_prints_the_answer_titled_sources_mode_and_model(env: Path) -> None:
    _library(env, graphed=True)
    fake, calls = _returning(ANSWER)
    with patch("lore.cli.answer_question", fake):
        result = runner.invoke(app, ["ask", "q"])
    assert result.exit_code == 0, result.output
    assert "Holmes opposes Moriarty [1]." in result.output
    assert "[1] A Study in Scarlet, chapter 7" in result.output
    assert "[2] zz, chapter 2" in result.output  # unknown book: its id
    assert "b1:1:0" not in result.output
    lines = result.output.strip().splitlines()
    assert lines[-1] == ("mode: graph via by_predicate"
                         " · answered by qwen2.5:3b-instruct-q4_K_M")


def test_ask_surfaces_the_fallback_note(env: Path) -> None:
    _library(env, graphed=True)
    answer = Answer(text="a", passages=(), mode="vector-fallback", template=None,
                    note="the graph contributed nothing", model="m")
    fake, _ = _returning(answer)
    with patch("lore.cli.answer_question", fake):
        result = runner.invoke(app, ["ask", "q"])
    assert result.exit_code == 0
    assert "note: the graph contributed nothing" in result.output
    assert result.output.strip().splitlines()[-1] == "mode: vector-fallback · answered by m"


def test_ask_with_no_model_asked_says_only_the_mode(env: Path) -> None:
    _library(env, graphed=True)
    answer = Answer(text="nothing", passages=(), mode="empty", template=None, note=None)
    fake, _ = _returning(answer)
    with patch("lore.cli.answer_question", fake):
        result = runner.invoke(app, ["ask", "q"])
    assert result.output.strip().splitlines()[-1] == "mode: empty"


def test_baseline_is_passed_through(env: Path) -> None:
    _library(env, graphed=True)
    fake, calls = _returning(ANSWER)
    with patch("lore.cli.answer_question", fake):
        runner.invoke(app, ["ask", "q", "--baseline"])
    assert calls[0][1]["baseline"] is True


def test_a_model_that_is_down_is_one_clear_line_not_a_traceback(env: Path) -> None:
    _library(env, graphed=True)

    async def down(*args, **kwargs):
        raise httpx.ConnectError("All connection attempts failed")

    with patch("lore.cli.answer_question", down):
        result = runner.invoke(app, ["ask", "q"])
    assert result.exit_code == 1
    assert "ask failed" in result.output
    assert "Traceback" not in result.output


def test_ask_writes_nothing_to_the_library(env: Path) -> None:
    _library(env, graphed=True)
    data = env / "data"
    for path in data.rglob("*"):
        os.utime(path, (1_000_000, 1_000_000))
    before = {p: p.stat().st_mtime for p in data.rglob("*")}
    fake, _ = _returning(ANSWER)
    with patch("lore.cli.answer_question", fake):
        result = runner.invoke(app, ["ask", "q"])
    assert result.exit_code == 0, result.output
    after = {p: p.stat().st_mtime for p in data.rglob("*")}
    # A read-only SQLite open may create its -wal/-shm side files; the
    # database, the graph and everything else must be untouched.
    side = {p for p in after if p.name.endswith(("-wal", "-shm"))}
    assert {p: m for p, m in after.items() if p not in side} == \
        {p: m for p, m in before.items() if p not in side}


# --- opt-in Gemini ------------------------------------------------------------

def test_gemini_without_a_key_exits_and_does_not_fall_back(env: Path) -> None:
    _library(env, graphed=True)
    _gemini_toml(env)
    fake, calls = _returning(ANSWER)
    with patch("lore.cli.answer_question", fake):
        result = runner.invoke(app, ["ask", "q"])
    assert result.exit_code == 2
    assert calls == [], "never silently answered by the local model"
    assert "GEMINI_API_KEY" in result.output
    assert "gemini.env" in result.output


def test_gemini_refuses_a_key_file_others_can_read(env: Path) -> None:
    _library(env, graphed=True)
    _gemini_toml(env)
    key_file = env / "home" / ".config" / "lore" / "gemini.env"
    key_file.parent.mkdir(parents=True)
    key_file.write_text(f"GEMINI_API_KEY={SECRET}\n")
    key_file.chmod(0o644)
    fake, calls = _returning(ANSWER)
    with patch("lore.cli.answer_question", fake):
        result = runner.invoke(app, ["ask", "q"])
    assert result.exit_code == 2
    assert calls == []
    assert "chmod 600" in result.output
    assert SECRET not in result.output


def test_gemini_uses_the_key_file_and_never_prints_the_key(env: Path) -> None:
    _library(env, graphed=True)
    _gemini_toml(env)
    key_file = env / "home" / ".config" / "lore" / "gemini.env"
    key_file.parent.mkdir(parents=True)
    key_file.write_text(f"GEMINI_API_KEY={SECRET}\n")
    key_file.chmod(0o600)
    seen = {}
    real = __import__("lore.cli", fromlist=["make_query_models"]).make_query_models

    def spy(config, api_key=None, **kwargs):
        seen["provider"], seen["key"] = config.synthesis.provider, api_key
        return real(config, api_key, **kwargs)

    answer = Answer(text="Mordecai Smith [1].", passages=(), mode="graph",
                    template="by_predicate", note=None, model="gemini-3.1-flash-lite")
    fake, _ = _returning(answer)
    with patch("lore.cli.answer_question", fake), patch("lore.cli.make_query_models", spy):
        result = runner.invoke(app, ["ask", "q"])
    assert result.exit_code == 0, result.output
    assert seen == {"provider": "gemini", "key": SECRET}
    assert SECRET not in result.output
    assert result.output.strip().splitlines()[-1] == \
        "mode: graph via by_predicate · answered by gemini-3.1-flash-lite"


def test_the_default_config_never_resolves_a_key(env: Path) -> None:
    _library(env, graphed=True)
    with patch("lore.cli.resolve_api_key") as resolve, \
            patch("lore.cli.answer_question", _returning(ANSWER)[0]):
        result = runner.invoke(app, ["ask", "q"])
    assert result.exit_code == 0, result.output
    assert not resolve.called


# --- the CLI as a whole --------------------------------------------------------

def test_every_existing_subcommand_still_resolves() -> None:
    # A single @app.command() would collapse Typer into single-command
    # mode and silently break the whole CLI.
    import re

    source = (Path(__file__).parent.parent / "lore" / "cli.py").read_text()
    commands = re.findall(r"@app\.command\(\)\ndef (\w+)\(", source)
    assert set(commands) >= {"doctor", "ingest", "books", "extract", "quota",
                             "process", "disambiguate", "graph", "ask"}
    for name in commands:
        result = runner.invoke(app, [name, "--help"])
        assert result.exit_code == 0, (name, result.output)
