"""`lore disambiguate` — the only command allowed to spend the daily quota.

Every test stubs the client. The free tier allows 20 requests a DAY, so a
test that reached the network would cost the user a fifth of a day's
budget per run.
"""
from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from lore.cli import app
from tests.fixtures import seed_book
from lore.config import LoreConfig
from lore.inference.gemini import GeminiQuotaError
from lore.resolve.disambiguation import Candidate, DisambiguationStore, Question

runner = CliRunner()


class _FakeClient:
    """Stands in for GeminiClient; records prompts, never opens a socket."""

    def __init__(self, replies=None, error: Exception | None = None) -> None:
        self.prompts: list[str] = []
        self._replies = list(replies or [])
        self._error = error
        self.requests_made = 0
        self.last_model: str | None = None
        self.daily_limit = 500
        self.attempts: dict[str, int] = {}

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.requests_made += 1
        if self._error is not None:
            raise self._error
        self.last_model = "gemini-3.1-flash-lite"
        self.attempts["gemini-3.1-flash-lite"] = self.requests_made
        return self._replies.pop(0)

    async def aclose(self) -> None:
        return None


def _seed(tmp_path: Path) -> tuple[LoreConfig, list[Question]]:
    config = LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})
    config.ensure_dirs()
    questions = [
        Question(
            mention="Sholto", kind="Character", book_id="b1",
            evidence="Major Sholto had been dead for years",
            candidates=(Candidate(id="id-thaddeus", name="Thaddeus Sholto"),
                        Candidate(id="id-bartholomew", name="Bartholomew Sholto")),
        ),
        Question(
            mention="Ferrier", kind="Character", book_id="b1",
            evidence="Ferrier looked at the girl",
            candidates=(Candidate(id="id-john", name="John Ferrier"),
                        Candidate(id="id-lucy", name="Lucy Ferrier")),
        ),
    ]
    store = DisambiguationStore(config.disambiguation_dir)
    for question in questions:
        store.record(question)
    store.flush("b1")
    # The command asks in the store's own canonical order (by cache key),
    # not in creation order — the batch numbering follows THAT, so a test
    # that assumed creation order would be asserting a lie.
    return config, DisambiguationStore(config.disambiguation_dir).pending()


def _reply(questions, verdicts) -> str:
    """A well-formed answer set in the store's own question order."""
    return "\n".join(
        f"{n}. {q.mention} -> {verdict}"
        for n, (q, verdict) in enumerate(zip(questions, verdicts), start=1)
    )


def _run(tmp_path, monkeypatch, argv, client=None, key="AIza-test"):
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    # Hermetic: never read the developer's real ~/.config/lore/gemini.env.
    monkeypatch.setenv("HOME", str(tmp_path))
    if key is None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GEMINI_API_KEY", key)
    if client is None:
        return runner.invoke(app, argv)
    with patch("lore.cli.GeminiClient", return_value=client):
        return runner.invoke(app, argv)


def test_no_key_spends_nothing_and_says_what_to_set(
    tmp_path: Path, monkeypatch
) -> None:
    _seed(tmp_path)
    result = _run(tmp_path, monkeypatch, ["disambiguate"], key=None)
    assert result.exit_code != 0
    assert "GEMINI_API_KEY" in result.output
    assert "0 request" in result.output


def test_the_key_file_is_read_like_ask_reads_it(tmp_path: Path, monkeypatch) -> None:
    # Task R1: the graph rebuild runs `lore disambiguate` with the key kept
    # in ~/.config/lore/gemini.env, as `ask`, `extract` and `process` read it.
    _seed(tmp_path)
    seen: dict = {}

    def fake_client(api_key, **kwargs):
        seen["key"] = api_key
        raise RuntimeError("stop before any request")

    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    key_file = tmp_path / ".config" / "lore" / "gemini.env"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("GEMINI_API_KEY=from-file\n")
    key_file.chmod(0o600)
    with patch("lore.cli.GeminiClient", side_effect=fake_client):
        runner.invoke(app, ["disambiguate"])
    assert seen.get("key") == "from-file"


def test_nothing_pending_spends_nothing(tmp_path: Path, monkeypatch) -> None:
    config = LoreConfig.load().model_copy(update={"data_dir": tmp_path / "data"})
    config.ensure_dirs()
    result = _run(tmp_path, monkeypatch, ["disambiguate"])
    assert result.exit_code == 0, result.output
    assert "nothing to ask" in result.output
    assert "0 request" in result.output


def test_dry_run_shows_the_questions_and_spends_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    _seed(tmp_path)
    client = _FakeClient()
    result = _run(tmp_path, monkeypatch, ["disambiguate", "--dry-run"], client)
    assert result.exit_code == 0, result.output
    assert client.prompts == []
    assert "Sholto" in result.output and "Ferrier" in result.output
    assert "0 request" in result.output


def test_one_request_answers_the_whole_batch_and_writes_the_cache(
    tmp_path: Path, monkeypatch
) -> None:
    config, questions = _seed(tmp_path)
    # Distinct verdicts per question, so a mapping that slipped by one
    # would produce a visibly different cache rather than a plausible one.
    client = _FakeClient([_reply(questions, ["1", "none"])])
    result = _run(tmp_path, monkeypatch, ["disambiguate"], client)
    assert result.exit_code == 0, result.output
    assert len(client.prompts) == 1, "the whole corpus is one request"
    assert "2 question(s)" in result.output
    assert "1 request" in result.output
    assert "500" in result.output, "the daily budget is named"

    store = DisambiguationStore(config.disambiguation_dir)
    first, second = questions
    assert store.answer_for(first) == first.candidates[0].id
    assert store.answer_for(second) is None
    assert store.unanswered(questions) == [], "both are settled"


def test_a_reload_of_the_same_questions_spends_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    _config, questions = _seed(tmp_path)
    first = _FakeClient([_reply(questions, ["1", "none"])])
    assert _run(tmp_path, monkeypatch, ["disambiguate"], first).exit_code == 0

    second = _FakeClient()
    result = _run(tmp_path, monkeypatch, ["disambiguate"], second)
    assert result.exit_code == 0, result.output
    assert second.prompts == [], "a cached question is never re-sent"
    assert "0 request" in result.output


def test_a_quota_error_is_actionable_and_leaves_the_cache_alone(
    tmp_path: Path, monkeypatch
) -> None:
    config, questions = _seed(tmp_path)
    client = _FakeClient(error=GeminiQuotaError(
        "HTTP 429 from gemini-3.7-flash: the free tier allows 20 requests/day"
    ))
    result = _run(tmp_path, monkeypatch, ["disambiguate"], client)
    assert result.exit_code != 0
    assert "20 requests/day" in result.output
    assert "cache" in result.output.lower()
    assert not (config.disambiguation_dir / "answers.json").exists()


def test_a_mis_mapped_answer_set_is_refused_and_nothing_is_cached(
    tmp_path: Path, monkeypatch
) -> None:
    config, questions = _seed(tmp_path)
    # Rotated: well-formed, right count, wrong questions.
    client = _FakeClient([
        f"1. {questions[1].mention} -> 1\n2. {questions[0].mention} -> 1"
    ])
    result = _run(tmp_path, monkeypatch, ["disambiguate"], client)
    assert result.exit_code != 0
    assert not (config.disambiguation_dir / "answers.json").exists()
    assert DisambiguationStore(config.disambiguation_dir).unanswered(
        questions
    ) == questions


def test_a_short_answer_set_is_refused(tmp_path: Path, monkeypatch) -> None:
    config, questions = _seed(tmp_path)
    client = _FakeClient([_reply(questions[:1], ["1"])])
    result = _run(tmp_path, monkeypatch, ["disambiguate"], client)
    assert result.exit_code != 0
    assert not (config.disambiguation_dir / "answers.json").exists()


def test_a_disabled_config_still_says_how_to_switch_it_on(
    tmp_path: Path, monkeypatch
) -> None:
    _config, questions = _seed(tmp_path)
    client = _FakeClient([_reply(questions, ["1", "none"])])
    result = _run(tmp_path, monkeypatch, ["disambiguate"], client)
    assert "disambiguation" in result.output
    assert "enabled" in result.output


def test_process_wires_no_store_when_disambiguation_is_off(
    tmp_path: Path, monkeypatch
) -> None:
    from lore.graph.loader import LoadReport

    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)  # no lore.toml: default-off
    seed_book("b")
    with (
        patch("lore.cli.run_extraction"),
        patch("lore.cli.load_book",
              new=AsyncMock(return_value=LoadReport(book_id="b"))) as load,
    ):
        result = runner.invoke(app, ["process", "b", "--skip-extract"])
    assert result.exit_code == 0, result.output
    assert load.call_args.kwargs["disambiguation"] is None


def test_process_wires_a_store_when_disambiguation_is_on(
    tmp_path: Path, monkeypatch
) -> None:
    from lore.graph.loader import LoadReport

    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    seed_book("b")
    (tmp_path / "lore.toml").write_text(
        "[lore.disambiguation]\nenabled = true\n"
    )
    with (
        patch("lore.cli.run_extraction"),
        patch("lore.cli.load_book",
              new=AsyncMock(return_value=LoadReport(book_id="b"))) as load,
    ):
        result = runner.invoke(app, ["process", "b", "--skip-extract"])
    assert result.exit_code == 0, result.output
    assert isinstance(
        load.call_args.kwargs["disambiguation"], DisambiguationStore
    )


def test_a_cascade_fallthrough_is_reported_against_each_models_budget(
    tmp_path: Path, monkeypatch
) -> None:
    """The real client, a stubbed transport, and no network.

    A run that fell through a 429 and a 503 spent one request against each
    of THOSE models' daily budgets and one against the model that
    answered. Reporting all three against the winner would be a lie, and
    the winner's budget (20/day) is the one the user is about to run out
    of.
    """
    import functools
    import json as _json

    import httpx

    from lore.inference.gemini import DEFAULT_MODELS, GeminiClient

    config, questions = _seed(tmp_path)
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = str(request.url).rsplit("/", 1)[-1].split(":")[0]
        tried.append(model)
        assert request.headers["x-goog-api-key"] == "AIza-test"
        if model == DEFAULT_MODELS[0]:
            return httpx.Response(429, text="RESOURCE_EXHAUSTED")
        if model == DEFAULT_MODELS[1]:
            return httpx.Response(503, text="overloaded")
        prompt = _json.loads(request.content)["contents"][0]["parts"][0]["text"]
        assert prompt.count("\n   sentence:") == len(questions)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": _reply(questions, ["1", "none"])}
        ]}}]})

    factory = functools.partial(
        GeminiClient, transport=httpx.MockTransport(handler)
    )
    with patch("lore.cli.GeminiClient", factory):
        result = _run(tmp_path, monkeypatch, ["disambiguate"])
    assert result.exit_code == 0, result.output
    assert tried == list(DEFAULT_MODELS[:3])
    assert f"{DEFAULT_MODELS[0]} 1/500" in result.output
    assert f"{DEFAULT_MODELS[1]} 1/500" in result.output
    assert f"{DEFAULT_MODELS[2]} 1/20" in result.output
    assert DisambiguationStore(config.disambiguation_dir).answer_for(
        questions[0]
    ) == questions[0].candidates[0].id


def test_an_exhausted_cascade_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    import functools

    import httpx

    from lore.inference.gemini import DEFAULT_MODELS, GeminiClient

    config, questions = _seed(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="RESOURCE_EXHAUSTED")

    factory = functools.partial(
        GeminiClient, transport=httpx.MockTransport(handler)
    )
    with patch("lore.cli.GeminiClient", factory):
        result = _run(tmp_path, monkeypatch, ["disambiguate"])
    assert result.exit_code != 0
    for model in DEFAULT_MODELS:
        assert model in result.output
    assert not (config.disambiguation_dir / "answers.json").exists()
