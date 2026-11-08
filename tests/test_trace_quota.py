"""Each user keeps at most N traces, oldest first (release step 6, task 5.4)."""
from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from lore import tracing
from lore.central import traces as T
from lore.central.models import Trace
from tests.auth_fixtures import NOW
from tests.central_fixtures import central_sessions


def save(central, rid, user, at, keep=None):
    t = tracing.Trace(request_id=rid)
    t.finish(answer={"answer": "a"})
    with central() as s:
        T.save(s, t, user_id=user, question="q", now=at, keep=keep)
        s.commit()


def ids(central, user):
    with central() as s:
        return sorted(s.scalars(sa.select(Trace.request_id).where(Trace.user_id == user)))


def test_the_oldest_go_first_past_the_quota(tmp_path):
    central, _ = central_sessions(tmp_path)
    for i in range(5):
        save(central, f"a{i}", "alice", NOW + timedelta(minutes=i), keep=3)
    assert ids(central, "alice") == ["a2", "a3", "a4"]


def test_one_users_quota_leaves_another_alone(tmp_path):
    central, _ = central_sessions(tmp_path)
    for i in range(3):
        save(central, f"b{i}", "bob", NOW + timedelta(minutes=i), keep=10)
    for i in range(4):
        save(central, f"a{i}", "alice", NOW + timedelta(minutes=i), keep=2)
    assert ids(central, "alice") == ["a2", "a3"]
    assert ids(central, "bob") == ["b0", "b1", "b2"]


def test_the_default_and_the_environment(monkeypatch):
    assert T.per_user_limit({}) == 2000
    assert T.per_user_limit({"LORE_TRACES_PER_USER": "50"}) == 50
    for bad in ("0", "x", "-3"):
        with pytest.raises(ValueError):
            T.per_user_limit({"LORE_TRACES_PER_USER": bad})


def test_the_environment_applies_to_save(tmp_path, monkeypatch):
    central, _ = central_sessions(tmp_path)
    monkeypatch.setenv("LORE_TRACES_PER_USER", "2")
    for i in range(3):
        save(central, f"a{i}", "alice", NOW + timedelta(minutes=i))
    assert ids(central, "alice") == ["a1", "a2"]


def test_the_thirty_day_expiry_still_applies(tmp_path):
    central, _ = central_sessions(tmp_path)
    save(central, "old", "alice", NOW - timedelta(days=31))
    save(central, "new", "alice", NOW)
    with central() as s:
        assert T.purge_expired(s, NOW) == 1
        s.commit()
    assert ids(central, "alice") == ["new"]
