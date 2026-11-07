"""A user's Gemini key never appears in logs, responses, events, error text or
reprs, including when the provider fails (release step 3). Uses a fake key and
the log redaction step 2 added; no network."""
import json
import logging

import httpx
import pytest
import sqlalchemy as sa

from lore.auth.redact import KEY_MASK, install_log_redaction, redact_keys
from lore.central import keys as K
from lore.central.models import ApiKey
from lore.config import LoreConfig
from lore.inference import key_scope
from lore.inference.gemini import (
    GeminiAuthError, GeminiClient, GeminiError, GeminiTransportError,
)
from lore.inference.key_scope import KeyScope, gemini_api_key
from tests.auth_fixtures import FAKE_KEY, NOW, FakeKeyChecker, make_app, master_keys, sign_in
from tests.central_fixtures import central_sessions

ODD_KEY = "not-google-shaped-but-still-a-secret-0123456789"  # a future key format


@pytest.fixture(autouse=True)
def capture_everything(caplog):
    caplog.set_level(logging.DEBUG)  # every logger, every level
    return caplog


@pytest.fixture
def central(tmp_path):
    return central_sessions(tmp_path, users=("alice",))[0]


# --- the log redaction --------------------------------------------------------------

def test_a_key_shaped_string_is_blanked_in_any_logger(caplog):
    install_log_redaction()
    for name in ("lore", "lore.keys", "httpx", "uvicorn.error", "", "anything.else"):
        logging.getLogger(name).warning("sending %s now", FAKE_KEY)
        logging.getLogger(name).error(f"inline {FAKE_KEY}")
    assert FAKE_KEY not in caplog.text
    assert caplog.text.count(KEY_MASK) == 12


def test_a_key_in_an_exception_traceback_is_blanked(caplog):
    install_log_redaction()
    try:
        raise RuntimeError(f"provider said: bad key {FAKE_KEY}")
    except RuntimeError:
        logging.getLogger("lore").exception("it failed")
    assert "provider said" in caplog.text and FAKE_KEY not in caplog.text


def test_the_bound_key_is_blanked_whatever_its_shape(caplog):
    install_log_redaction()
    with key_scope.bound(KeyScope("alice", ODD_KEY)):
        logging.getLogger("lore").warning("odd %s", ODD_KEY)
    assert ODD_KEY not in caplog.text
    assert redact_keys(f"x {FAKE_KEY} y") == f"x {KEY_MASK} y"


def test_ordinary_records_are_left_alone(caplog):
    install_log_redaction()
    logging.getLogger("lore").info("%s requests on %s", 3, "gemini-3.1-flash-lite")
    assert "3 requests on gemini-3.1-flash-lite" in caplog.text
    assert caplog.records[-1].args == (3, "gemini-3.1-flash-lite")


# --- error text and reprs -------------------------------------------------------------

def _client(handler):
    return GeminiClient(FAKE_KEY, models=["m"], transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("handler,kind", [
    (lambda r: httpx.Response(401, text="nope"), GeminiAuthError),
    (lambda r: httpx.Response(403, text="nope"), GeminiAuthError),
    (lambda r: httpx.Response(500, text="server error"), GeminiError),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("no route", request=r)),
     GeminiTransportError),
])
def test_provider_errors_never_carry_the_key(handler, kind):
    import asyncio

    async def go():
        client = _client(handler)
        try:
            await client.generate("q")
        finally:
            await client.aclose()

    with pytest.raises(kind) as err:
        asyncio.run(go())
    chain, exc = [], err.value
    while exc is not None:
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__
    for e in chain:
        assert FAKE_KEY not in str(e) and FAKE_KEY not in repr(e)


def test_reprs_never_show_a_key(central):
    svc = K.KeyService(central, master_keys(), FakeKeyChecker(), clock=lambda: NOW)
    svc.save("alice", FAKE_KEY)
    with central() as db:
        row = db.get(ApiKey, ("alice", "gemini"))
        assert FAKE_KEY not in repr(row)
    for obj in (svc, KeyScope("alice", FAKE_KEY), master_keys(), svc.status("alice"),
                K.SaveOutcome(True, "ok")):
        assert FAKE_KEY not in repr(obj) and FAKE_KEY not in str(obj)


def test_the_database_holds_no_plaintext(central, tmp_path):
    K.KeyService(central, master_keys(), FakeKeyChecker(), clock=lambda: NOW).save(
        "alice", FAKE_KEY)
    raw = (tmp_path / "central.db").read_bytes()
    assert FAKE_KEY.encode() not in raw
    wal = tmp_path / "central.db-wal"
    if wal.exists():
        assert FAKE_KEY.encode() not in wal.read_bytes()


# --- end to end: the web app ---------------------------------------------------------

def _events(body: str) -> list[dict]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append({"event": lines["event"], **json.loads(lines["data"])})
    return out


@pytest.mark.parametrize("failure", ["401", "500", "connect", "raise"])
def test_a_failing_question_shows_the_key_nowhere(tmp_path, central, monkeypatch,
                                                   caplog, failure):
    """The provider fails in each way; the key is in no log, no event, no body."""
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), central)
    alice = sign_in(app, central, "alice")
    assert alice.put("/api/me/key", json={"key": FAKE_KEY}).status_code == 200

    async def search(config, query, on_event=None, **_):
        on_event({"type": "parts", "sub_questions": [query]})

        def reply(request):
            assert request.headers["x-goog-api-key"] == FAKE_KEY  # it was used
            if failure == "connect":
                raise httpx.ConnectError("no route to host", request=request)
            return httpx.Response(int(failure) if failure != "raise" else 500,
                                  text='{"error": "upstream"}')

        client = GeminiClient(gemini_api_key(), models=["m"],
                              transport=httpx.MockTransport(reply))
        try:
            await client.generate("q")
        except GeminiError as exc:
            logging.getLogger("lore.mcp").exception("Gemini failed")
            if failure == "raise":  # an unexpected crash: the app logs the traceback
                raise RuntimeError(f"boom while using {gemini_api_key()}") from exc
            return {"error": {"code": "upstream_unavailable", "message": str(exc)}}
        finally:
            await client.aclose()
        return {"answer": "?"}

    monkeypatch.setattr("lore.web.app.tools.lore_search", search)
    with alice.stream("POST", "/api/ask", json={"question": "who?"}) as r:
        body = b"".join(r.iter_bytes()).decode()
    events = _events(body)
    assert events[-1]["event"] == "error"
    assert FAKE_KEY not in body
    assert FAKE_KEY not in caplog.text
    for path in ("/api/me/key", "/api/me/usage", "/api/books", "/api/quota"):
        assert FAKE_KEY not in alice.get(path).text, path


def test_a_checker_that_raises_with_the_key_in_its_message_leaks_nothing(
        tmp_path, central, caplog):
    checker = FakeKeyChecker(result=RuntimeError(f"cannot check {FAKE_KEY}"))
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), central, key_checker=checker)
    alice = sign_in(app, central, "alice")
    r = alice.put("/api/me/key", json={"key": FAKE_KEY})
    assert r.status_code == 200 and r.json()["check"] == "unavailable"
    assert FAKE_KEY not in r.text and FAKE_KEY not in caplog.text


def test_saving_a_key_logs_nothing_about_it(tmp_path, central, caplog):
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), central)
    alice = sign_in(app, central, "alice")
    for status in ("ok", "invalid", "unavailable"):
        app_checker = FakeKeyChecker(status)
        app2 = make_app(LoreConfig(data_dir=tmp_path / "root"), central,
                        key_checker=app_checker)
        client = sign_in(app2, central, "alice")
        r = client.put("/api/me/key", json={"key": FAKE_KEY})
        assert FAKE_KEY not in r.text
    assert alice.delete("/api/me/key").status_code == 200
    assert FAKE_KEY not in caplog.text
