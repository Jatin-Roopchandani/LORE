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
