"""`lore pull-models`: the embedding model is fetched through Ollama's HTTP API,
so the self-hosted compose file needs no Ollama image just to pull it."""
import httpx
import pytest

from lore.inference.ollama import ModelPullError, ensure_model


class Fake:
    def __init__(self, models=(), down_for=0, pull_status=200, pull_adds=True):
        self.models, self.down_for, self.pull_status, self.pull_adds = list(models), down_for, pull_status, pull_adds
        self.pulled = []

    def handler(self, request):
        if self.down_for > 0:
            self.down_for -= 1
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if request.url.path == "/api/pull":
            import json
            body = json.loads(request.content)
            self.pulled.append(body)
            if self.pull_status < 400 and self.pull_adds:
                self.models.append(body["model"] + ":latest")
            return httpx.Response(self.pull_status, json={"status": "success"})
        return httpx.Response(404)


def client(fake):
    return httpx.Client(base_url="http://ollama:11434", transport=httpx.MockTransport(fake.handler))


def test_a_model_already_there_is_not_pulled_again():
    fake = Fake(models=["nomic-embed-text:latest"])
    assert ensure_model("http://ollama:11434", "nomic-embed-text", client=client(fake)) is False
    assert fake.pulled == []


def test_a_missing_model_is_pulled_through_the_api():
    fake = Fake(models=["qwen2.5:3b"])
    assert ensure_model("http://ollama:11434", "nomic-embed-text", client=client(fake)) is True
    assert fake.pulled == [{"model": "nomic-embed-text", "stream": False}]


def test_it_waits_for_an_ollama_that_is_still_starting():
    fake, slept = Fake(down_for=3), []
    ensure_model("http://ollama:11434", "nomic-embed-text", client=client(fake),
                 sleep=slept.append, now=lambda: 0.0)
    assert len(slept) == 3 and fake.pulled


def test_an_ollama_that_never_answers_is_a_clear_error():
    fake, clock = Fake(down_for=10_000), iter(range(0, 10_000, 100))
    with pytest.raises(ModelPullError, match="LORE_OLLAMA_HOST"):
        ensure_model("http://nowhere:11434", "nomic-embed-text", client=client(fake),
                     wait_s=250, sleep=lambda s: None, now=lambda: next(clock))


@pytest.mark.parametrize("status,adds", [(500, False), (200, False)])
def test_a_failed_pull_is_a_clear_error(status, adds):
    fake = Fake(pull_status=status, pull_adds=adds)
    with pytest.raises(ModelPullError):
        ensure_model("http://ollama:11434", "nomic-embed-text", client=client(fake))


def test_the_cli_uses_the_configured_host_and_model(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from lore import cli
    seen = {}

    def fake_ensure(host, model, *, wait_s):
        seen.update(host=host, model=model, wait=wait_s)
        return True

    monkeypatch.chdir(tmp_path)  # no lore.toml: defaults
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LORE_OLLAMA_HOST", "http://my-box:11434")
    monkeypatch.setattr("lore.inference.ollama.ensure_model", fake_ensure)
    r = CliRunner().invoke(cli.app, ["pull-models", "--wait", "5"])
    assert r.exit_code == 0, r.output
    assert seen == {"host": "http://my-box:11434", "model": "nomic-embed-text", "wait": 5}
    assert "pulled on http://my-box:11434" in r.output


def test_the_cli_fails_loudly(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from lore import cli

    def fail(host, model, *, wait_s):
        raise ModelPullError("no Ollama answered")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("lore.inference.ollama.ensure_model", fail)
    r = CliRunner().invoke(cli.app, ["pull-models"])
    assert r.exit_code == 1 and "no Ollama answered" in r.output
