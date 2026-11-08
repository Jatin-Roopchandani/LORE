"""A throwaway Postgres in docker, for the opt-in `postgres` tests.

Run them with `LORE_TEST_POSTGRES=1 pytest -m postgres tests/<file>`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from lore.central import db as central

requires_postgres = pytest.mark.skipif(
    not os.environ.get("LORE_TEST_POSTGRES") or shutil.which("docker") is None,
    reason="set LORE_TEST_POSTGRES=1 and have docker to run")


@contextmanager
def postgres_url():
    """Start a container, wait for it, migrate it; yield its URL; remove it."""
    name = f"lore-pg-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=pw",
         "-p", "127.0.0.1::5432", "postgres:16"], check=True, capture_output=True)
    try:
        port = subprocess.run(["docker", "port", name, "5432/tcp"], check=True,
                              capture_output=True, text=True).stdout.split(":")[-1].strip()
        url = f"postgresql+psycopg://postgres:pw@127.0.0.1:{port}/postgres"
        engine = central.make_engine(url)
        for _ in range(60):
            try:
                engine.connect().close()
                break
            except sa.exc.OperationalError:
                time.sleep(1)
        else:
            raise AssertionError("postgres never came up")
        engine.dispose()
        central.upgrade(url)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
