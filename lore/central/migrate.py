"""Run the Alembic migrations from code (no alembic.ini needed)."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

SCRIPTS = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPTS))
    cfg.attributes["url"] = url
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)
