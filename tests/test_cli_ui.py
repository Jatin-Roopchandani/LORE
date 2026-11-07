from typer.testing import CliRunner

from lore.cli import app


def test_ui_refuses_a_non_loopback_host():
    result = CliRunner().invoke(app, ["ui", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "loopback" in result.output


def test_ui_starts_uvicorn_on_loopback(monkeypatch):
    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, **kw: seen.update(host=host, port=port))
    result = CliRunner().invoke(app, ["ui"])
    assert result.exit_code == 0, result.output
    assert seen == {"host": "127.0.0.1", "port": 8766}


def test_ui_in_multi_mode_without_its_secrets_says_what_is_missing(monkeypatch):
    for name in ("LORE_SESSION_SECRET", "LORE_CSRF_SECRET", "LORE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    started = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: started.append(1))
    result = CliRunner().invoke(app, ["ui"])
    assert result.exit_code == 2 and not started
    assert "LORE_SESSION_SECRET" in result.output


def test_ui_in_multi_mode_may_bind_every_interface(monkeypatch):
    """A container's web process (deploy/compose.yaml) listens on 0.0.0.0:
    multi mode authenticates every request, so the loopback rule (made for
    the unauthenticated local mode) does not apply."""
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    made, seen = {}, {}
    monkeypatch.setattr("lore.web.app.create_app",
                        lambda config, host, port, dev: made.update(host=host, port=port))
    monkeypatch.setattr("uvicorn.run",
                        lambda app, host, port, **kw: seen.update(host=host, port=port))
    result = CliRunner().invoke(app, ["ui", "--host", "0.0.0.0", "--port", "8000"])
    assert result.exit_code == 0, result.output
    assert seen == {"host": "0.0.0.0", "port": 8000}
    assert made == {"host": "0.0.0.0", "port": 8000}


def test_ui_in_local_mode_still_refuses_every_interface(monkeypatch):
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    result = CliRunner().invoke(app, ["ui", "--host", "0.0.0.0", "--port", "8000"])
    assert result.exit_code == 2
    assert "loopback" in result.output
