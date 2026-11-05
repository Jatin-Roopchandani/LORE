"""Rate limits and concurrency caps (release step 6, task 2).

Each limit, its reset, isolation between users and the global caps. The
counters live in the central database (shared by every web process); time is
the auth clock, moved by hand."""
from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from lore.central.models import InflightSlot, RateHit
from lore.config import LoreConfig
from lore.inference import key_scope
from lore.web.throttle import ASK_LEASE, RateSettings, Slots, Throttle
from tests.auth_fixtures import NOW, Clock, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    for user in ("alice", "bob"):
        give_key(sessions, user)
    return sessions


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def quick(monkeypatch):
    async def answer(config, query, on_event=None, **_):
        return {"answer": "ok", "mode": "graph", "claims": [], "passages": [],
                "edges": [], "seeds": [], "sub_questions": [query]}
    monkeypatch.setattr("lore.web.app.tools.lore_search", answer)


def build(tmp_path, central, clock, **limits):
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), central, auth_clock=clock,
                   rate_settings=RateSettings(**limits))
    return {u: sign_in(app, central, u) for u in ("alice", "bob")}


def ask(client):
    return client.post("/api/ask", json={"question": "who?"})


def slots(central):
    with central() as s:
        return s.scalars(sa.select(InflightSlot)).all()


def hold(central, kind, slot, expires):
    with central() as s:
        s.add(InflightSlot(kind=kind, slot=slot, holder=f"h{slot}", expires_at=expires))
        s.commit()


# --- settings -----------------------------------------------------------------------

def test_defaults_and_the_environment():
    assert RateSettings.from_env({}) == RateSettings(6, 1, 8, 60, 20, 2)
    env = {"LORE_ASK_PER_MINUTE": "3", "LORE_ASK_IN_FLIGHT_PER_USER": "2",
           "LORE_ASK_IN_FLIGHT": "4", "LORE_INTRO_PER_HOUR": "5",
           "LORE_REPORTS_PER_HOUR": "7", "LORE_PARSES_IN_FLIGHT": "1"}
    assert RateSettings.from_env(env) == RateSettings(3, 2, 4, 5, 7, 1)
    for bad in ("x", "-1", "1.5"):
        with pytest.raises(ValueError):
            RateSettings.from_env({"LORE_ASK_PER_MINUTE": bad})


# --- questions per minute -------------------------------------------------------------

def test_questions_per_minute_then_429_then_reset(tmp_path, central, clock, quick):
    users = build(tmp_path, central, clock, ask_per_minute=2)
    alice = users["alice"]
    assert ask(alice).status_code == 200
    clock.now = NOW + timedelta(seconds=10)
    assert ask(alice).status_code == 200
    clock.now = NOW + timedelta(seconds=20)
    r = ask(alice)
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == "rate_limited" and "2 questions a minute" in body["error"]["message"]
    assert body["retry_after"] == 40 and r.headers["retry-after"] == "40"
    assert body["error"]["request_id"] == r.headers["x-request-id"]
    # A refused question is not counted: once the first leaves the window, one more.
    clock.now = NOW + timedelta(seconds=61)
    assert ask(alice).status_code == 200
    assert ask(alice).status_code == 429


def test_one_users_limit_is_not_anothers(tmp_path, central, clock, quick):
    users = build(tmp_path, central, clock, ask_per_minute=1)
    assert ask(users["alice"]).status_code == 200
    assert ask(users["alice"]).status_code == 429
    assert ask(users["bob"]).status_code == 200


# --- in flight --------------------------------------------------------------------------

def test_one_question_in_flight_per_user(tmp_path, central, clock, quick):
    users = build(tmp_path, central, clock)
    hold(central, "ask:user:alice", 0, NOW + ASK_LEASE)  # alice's first is still running
    r = ask(users["alice"])
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "busy" and r.json()["retry_after"] == 5
    assert ask(users["bob"]).status_code == 200
    # A refused question counted nothing against the per-minute rate.
    with central() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(RateHit)) == 1


def test_a_finished_question_gives_its_slots_back(tmp_path, central, clock, quick):
    users = build(tmp_path, central, clock)
    assert ask(users["alice"]).status_code == 200
    assert slots(central) == []
    assert ask(users["alice"]).status_code == 200


def test_a_failed_question_gives_its_slots_back(tmp_path, central, clock, monkeypatch):
    async def crash(config, query, on_event=None, **_):
        raise RuntimeError("boom")
    monkeypatch.setattr("lore.web.app.tools.lore_search", crash)
    users = build(tmp_path, central, clock)
    assert ask(users["alice"]).status_code == 200  # an error event
    assert slots(central) == []


def test_a_dead_process_slot_frees_when_its_lease_ends(tmp_path, central, clock, quick):
    users = build(tmp_path, central, clock)
    hold(central, "ask:user:alice", 0, NOW + timedelta(minutes=1))
    assert ask(users["alice"]).status_code == 429
    clock.now = NOW + timedelta(minutes=2)
    assert ask(users["alice"]).status_code == 200


def test_the_global_cap_on_questions_in_flight(tmp_path, central, clock, quick):
    users = build(tmp_path, central, clock, ask_in_flight=2)
    for slot in (0, 1):
        hold(central, "ask:all", slot, NOW + ASK_LEASE)
    r = ask(users["bob"])
    assert r.status_code == 429 and r.json()["error"]["code"] == "busy"
    assert r.json()["retry_after"] == 15
    # bob's own slot was given back when the global one could not be had.
    assert {s.kind for s in slots(central)} == {"ask:all"}
    with central() as s:
        s.execute(sa.delete(InflightSlot).where(InflightSlot.slot == 1))
        s.commit()
    assert ask(users["bob"]).status_code == 200


def test_slots_are_atomic_and_counted(tmp_path, central, clock):
    pool = Slots(lambda: central, clock)
    held = [pool.acquire("k", 3) for _ in range(4)]
    assert all(held[:3]) and held[3] is None
    pool.release("k", held[0])
    assert pool.acquire("k", 3) is not None
    assert pool.acquire("unlimited", 0) == ""  # 0: no cap


# --- entity intros -----------------------------------------------------------------------

def test_intro_model_calls_per_hour(tmp_path, central, clock, monkeypatch):
    calls = []

    def intro(config, entity_id, world=None, **_):
        key_scope.note_request("gemini-flash")  # what GeminiClient does per request
        calls.append(entity_id)
        return {"entity_id": entity_id, "intro": "A detective."}
    monkeypatch.setattr("lore.web.app.explore.intro", intro)
    users = build(tmp_path, central, clock, intro_per_hour=2)
    alice = users["alice"]
    assert alice.get("/api/entity/e1/intro").status_code == 200
    assert alice.get("/api/entity/e2/intro").status_code == 200
    r = alice.get("/api/entity/e3/intro")
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert r.json()["retry_after"] == 3600
    assert calls == ["e1", "e2"]
    assert users["bob"].get("/api/entity/e3/intro").status_code == 200
    clock.now = NOW + timedelta(hours=1, seconds=1)
    assert alice.get("/api/entity/e3/intro").status_code == 200


def test_a_cached_intro_spends_nothing(tmp_path, central, clock, monkeypatch):
    monkeypatch.setattr("lore.web.app.explore.intro",
                        lambda config, entity_id, world=None, **_: {"intro": "cached"})
    users = build(tmp_path, central, clock, intro_per_hour=1)
    for _ in range(3):
        assert users["alice"].get("/api/entity/e1/intro").status_code == 200


# --- reports ------------------------------------------------------------------------------

def test_reports_per_hour(tmp_path, central, clock):
    from lore import tracing
    from lore.central import traces
    users = build(tmp_path, central, clock, reports_per_hour=2)
    with central() as s:
        for rid, user in (("r-a", "alice"), ("r-b", "bob")):
            t = tracing.Trace(request_id=rid)
            t.finish(answer={"answer": "x"})
            traces.save(s, t, user_id=user, question="q", now=NOW)
        s.commit()

    def report(user, rid):
        return users[user].post("/api/reports", json={"request_id": rid, "note": "wrong"})
    assert report("alice", "r-a").status_code == 201
    assert report("alice", "r-a").status_code == 201
    r = report("alice", "r-a")
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert report("bob", "r-b").status_code == 201
    clock.now = NOW + timedelta(hours=1, seconds=1)
    assert report("alice", "r-a").status_code == 201


# --- uploads being parsed -------------------------------------------------------------------

def test_the_global_cap_on_uploads_being_parsed(tmp_path, central, clock):
    from tests.test_web_upload import epub_bytes
    users = build(tmp_path, central, clock, parses_in_flight=1)
    hold(central, "parse:all", 0, NOW + timedelta(minutes=5))
    r = users["alice"].post("/api/books/upload",
                            files={"file": ("b.epub", epub_bytes(tmp_path), "application/epub+zip")})
    assert r.status_code == 429 and r.json()["error"]["code"] == "busy"
    with central() as s:
        s.execute(sa.delete(InflightSlot))
        s.commit()
    r = users["alice"].post("/api/books/upload",
                            files={"file": ("b.epub", epub_bytes(tmp_path), "application/epub+zip")})
    assert r.status_code == 202, r.text
    assert slots(central) == []


def test_throttle_report_counts_in_the_callers_session(central, clock):
    t = Throttle(lambda: central, RateSettings(reports_per_hour=1), clock)
    with central() as s:
        assert t.report(s, "alice") is None
        s.commit()
    with central() as s:
        assert t.report(s, "alice").status_code == 429
