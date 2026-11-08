"""Release step 6 on a real Postgres: in-flight slots raced by many threads,
the bound invite, and an account deletion across every table. Opt-in:
    LORE_TEST_POSTGRES=1 pytest -m postgres tests/test_postgres_hardening.py
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from lore.auth import accounts
from lore.auth.tokens import hash_token
from lore.central import db as central
from lore.central import erase, queue
from lore.central.models import (
    AdminEvent, AuthSession, InflightSlot, Invite, Job, RateHit, User,
)
from lore.web.throttle import Slots, take
from tests.postgres_fixtures import postgres_url, requires_postgres

pytestmark = [pytest.mark.postgres, requires_postgres]
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def sessions():
    with postgres_url() as url:
        engine = central.make_engine(url)
        yield central.make_session_factory(engine)
        engine.dispose()


@pytest.fixture(autouse=True)
def clean(sessions):
    yield
    with sessions() as s:
        s.execute(sa.text("TRUNCATE jobs, identities, invites, sessions, inflight_slots,"
                          " rate_hits, admin_events, users CASCADE"))
        s.commit()


def test_racing_threads_never_take_more_slots_than_the_cap(sessions):
    pool = Slots(lambda: sessions, lambda: NOW)
    got, barrier = [], threading.Barrier(12)

    def grab():
        barrier.wait()
        got.append(pool.acquire("ask:all", 3))
    threads = [threading.Thread(target=grab) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for g in got if g) == 3
    with sessions() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(InflightSlot)) == 3


def test_a_rate_window_on_postgres(sessions):
    with sessions() as s:
        for _ in range(2):
            assert take(s, "ask", "alice", 2, NOW, timedelta(minutes=1)) is None
        assert take(s, "ask", "alice", 2, NOW, timedelta(minutes=1)) == 60
        s.commit()


def test_a_bound_invite_is_spent_only_by_its_email(sessions):
    with sessions() as s:
        code = accounts.create_invite(s, None, NOW, email="reader@example.org")
        s.commit()
    with sessions() as s:
        out = accounts.sign_in_with_identity(s, "email", "x@example.org", "x@example.org",
                                             None, hash_token(code), True, NOW)
        assert out.outcome is accounts.Outcome.NEEDS_INVITE
        s.rollback()
        out = accounts.sign_in_with_identity(s, "email", "reader@example.org",
                                             "reader@example.org", None, hash_token(code), True, NOW)
        assert out.outcome is accounts.Outcome.OK
        s.commit()


def test_erasing_an_account_on_postgres(sessions, tmp_path):
    from lore.auth import sessions as auth_sessions
    with sessions() as s:
        s.add_all([User(id="alice", email="a@example.org"), User(id="bob", email="b@example.org")])
        s.flush()
        for uid in ("alice", "bob"):
            auth_sessions.create_session(s, uid, NOW)
            queue.enqueue(s, uid, "book", "process")
        s.add(AdminEvent(admin_id="bob", action="disable", target_user_id="alice", at=NOW))
        s.commit()
    (tmp_path / "users" / "alice").mkdir(parents=True)
    done = erase.erase_user(sessions, tmp_path, "alice", now=NOW)
    assert done.library_removed and not (tmp_path / "users" / "alice").exists()
    with sessions() as s:
        assert s.get(User, "alice") is None and s.get(User, "bob") is not None
        assert [j.user_id for j in s.scalars(sa.select(Job))] == ["bob"]
        assert [r.user_id for r in s.scalars(sa.select(AuthSession))] == ["bob"]
        assert s.scalars(sa.select(AdminEvent)).one().target_user_id == "alice"
