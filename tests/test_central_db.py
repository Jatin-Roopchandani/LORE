"""The central database: migrations apply on SQLite, and the models agree
with them; users and jobs round-trip."""
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from lore.central import db as central
from lore.central.models import Base, Invite, Job, User, Waitlist
from lore.config import LoreConfig

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def url(tmp_path):
    u = f"sqlite:///{tmp_path / 'central.db'}"
    central.upgrade(u)
    return u


@pytest.fixture
def session(url):
    engine = central.make_engine(url)
    with central.make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def test_default_url_is_a_sqlite_file_beside_the_data_root(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_DATABASE_URL", raising=False)
    cfg = LoreConfig(data_dir=tmp_path / "d")
    assert central.database_url(cfg) == f"sqlite:///{(tmp_path / 'd').resolve()}/central.db"
    monkeypatch.setenv("LORE_DATABASE_URL", "postgresql://u:p@h/lore")
    assert central.database_url(cfg) == "postgresql://u:p@h/lore"
    assert central.make_engine("postgresql://u:p@h/lore").url.drivername == "postgresql+psycopg"


def test_migrations_create_every_table(url):
    insp = sa.inspect(central.make_engine(url))
    assert {"users", "jobs", "waitlist", "invites", "alembic_version"} <= set(
        insp.get_table_names())


def test_migrations_are_idempotent_and_match_the_models(url):
    central.upgrade(url)  # a second run changes nothing
    engine = central.make_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_user_round_trip_and_unique_email(session):
    session.add(User(id="u1", email="a@x.org", name="Ann", created_at=NOW))
    session.commit()
    got = session.get(User, "u1")
    assert (got.role, got.status, got.created_at) == ("user", "active", NOW)
    session.add(User(id="u2", email="a@x.org"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_job_round_trip(session):
    session.add(User(id="u1", email="a@x.org"))
    session.flush()  # no relationship() tells the unit of work to insert users first
    session.add(Job(user_id="u1", book_id="b1", kind="move", payload={"world": 3}))
    session.commit()
    job = session.scalars(sa.select(Job)).one()
    assert (job.state, job.attempts, job.payload) == ("queued", 0, {"world": 3})
    assert job.created_at.tzinfo is not None


def test_a_book_has_one_live_job(session):
    session.add(User(id="u1", email="a@x.org"))
    session.flush()  # no relationship() tells the unit of work to insert users first
    session.add(Job(user_id="u1", book_id="b1", kind="process"))
    session.commit()
    session.add(Job(user_id="u1", book_id="b1", kind="process"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add(Job(user_id="u1", book_id="b2", kind="process"))  # another book is fine
    session.commit()


def test_a_finished_job_does_not_block_a_new_one(session):
    session.add(User(id="u1", email="a@x.org"))
    session.flush()  # no relationship() tells the unit of work to insert users first
    session.add(Job(user_id="u1", book_id="b1", kind="process", state="done"))
    session.add(Job(user_id="u1", book_id="b1", kind="process"))
    session.commit()


def test_bad_kind_and_naive_datetimes_are_refused(session):
    session.add(User(id="u1", email="a@x.org"))
    session.flush()  # no relationship() tells the unit of work to insert users first
    session.add(Job(user_id="u1", book_id="b1", kind="bogus"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add(Waitlist(email="w@x.org", created_at=datetime(2026, 1, 1)))
    with pytest.raises(sa.exc.StatementError):
        session.commit()


def test_waitlist_and_invites_are_schema_ready(session):
    session.add(User(id="u1", email="a@x.org"))
    session.flush()
    session.add(Waitlist(email="w@x.org", reading="Dracula"))
    session.add(Invite(code_hash="abc", created_by="u1"))
    session.commit()
    assert session.scalars(sa.select(Waitlist)).one().status == "pending"
    assert session.get(Invite, "abc").used_by is None


@pytest.mark.postgres
@pytest.mark.postgres
@pytest.mark.skipif(not os.environ.get("LORE_TEST_POSTGRES")
                    or shutil.which("docker") is None,
                    reason="set LORE_TEST_POSTGRES=1 and have docker to run")
def test_migrations_apply_on_a_throwaway_postgres():
    name = f"lore-pg-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e",
         "POSTGRES_PASSWORD=pw", "-p", "127.0.0.1::5432", "postgres:16"],
        check=True, capture_output=True)
    try:
        port = subprocess.run(["docker", "port", name, "5432/tcp"], check=True,
                              capture_output=True, text=True).stdout.split(":")[-1].strip()
        url = f"postgresql+psycopg://postgres:pw@127.0.0.1:{port}/postgres"
        engine = central.make_engine(url)
        for _ in range(60):  # wait for the server to accept connections
            try:
                engine.connect().close()
                break
            except sa.exc.OperationalError:
                time.sleep(1)
        else:
            raise AssertionError("postgres never came up")
        central.upgrade(url)
        with engine.connect() as conn:
            assert not compare_metadata(MigrationContext.configure(conn), Base.metadata)
        with central.make_session_factory(engine)() as s:
            # No relationship() ties Job to User, so the unit of work does not order
            # the inserts by the foreign key: flush the user first, as the app does.
            s.add(User(id="u1", email="a@x.org"))
            s.flush()
            s.add(Job(user_id="u1", book_id="b1", kind="process"))
            s.commit()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
