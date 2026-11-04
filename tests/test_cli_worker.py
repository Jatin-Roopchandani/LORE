"""`lore migrate` and `lore worker` as commands."""
import sqlalchemy as sa
from typer.testing import CliRunner

from lore.cli import app

runner = CliRunner()


def test_migrate_then_an_empty_worker_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no lore.toml here: defaults
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("LORE_DATABASE_URL", raising=False)
    (tmp_path / "data").mkdir()
    assert runner.invoke(app, ["migrate"]).exit_code == 0
    assert runner.invoke(app, ["migrate"]).exit_code == 0  # again: fine
    tables = sa.inspect(sa.create_engine(
        f"sqlite:///{tmp_path / 'data' / 'central.db'}")).get_table_names()
    assert "jobs" in tables and "users" in tables
    assert {"api_keys", "usage"} <= set(tables)
    monkeypatch.delenv("LORE_KEY_ENCRYPTION_KEY", raising=False)
    refused = runner.invoke(app, ["worker", "--once"])  # no master key: no start
    assert refused.exit_code == 2 and "LORE_KEY_ENCRYPTION_KEY" in refused.output
    monkeypatch.setenv("LORE_KEY_ENCRYPTION_KEY", "a" * 43 + "=")
    result = runner.invoke(app, ["worker", "--once"])
    assert result.exit_code == 0, result.output
