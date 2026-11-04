"""The Resend mailer (mock transport) and the log redaction."""
import logging

import httpx
import pytest

from lore.auth.mailer import MailError, ResendMailer, UnconfiguredMailer
from lore.auth.redact import RedactQuerySecrets, install_log_redaction, redact


def mailer(handler):
    return ResendMailer("re_secretkey", "LORE <login@example.org>",
                        transport=httpx.MockTransport(handler))


def test_resend_gets_the_key_the_sender_and_the_message():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "1"})

    mailer(handler).send(to="a@example.org", subject="Hi", text="body", html="<p>b</p>")
    (req,) = seen
    assert str(req.url) == "https://api.resend.com/emails"
    assert req.headers["authorization"] == "Bearer re_secretkey"
    import json
    assert json.loads(req.content) == {"from": "LORE <login@example.org>",
                                       "to": ["a@example.org"], "subject": "Hi",
                                       "text": "body", "html": "<p>b</p>"}


def test_a_refusal_is_a_mail_error_without_the_message_or_key():
    m = mailer(lambda r: httpx.Response(422, json={"message": "bad a@example.org"}))
    with pytest.raises(MailError) as exc:
        m.send(to="a@example.org", subject="Hi", text="https://link/?token=zzz")
    text = str(exc.value)
    assert "422" in text and "a@example.org" not in text and "zzz" not in text
    assert "re_secretkey" not in text


def test_an_unreachable_service_is_a_mail_error():
    def boom(request):
        raise httpx.ConnectError("no route to host a@example.org")
    with pytest.raises(MailError) as exc:
        mailer(boom).send(to="a@example.org", subject="s", text="t")
    assert "a@example.org" not in str(exc.value)


def test_the_mailer_repr_hides_the_key():
    assert "re_secretkey" not in repr(mailer(lambda r: httpx.Response(200)))


def test_an_unconfigured_mailer_fails_loudly():
    with pytest.raises(MailError):
        UnconfiguredMailer().send(to="a@b.co", subject="s", text="t")


@pytest.mark.parametrize("before,after", [
    ("GET /auth/email/verify?token=abc.def HTTP/1.1",
     "GET /auth/email/verify?token=[redacted] HTTP/1.1"),
    ("/auth/google/callback?code=4%2F0A&state=xyz",
     "/auth/google/callback?code=[redacted]&state=[redacted]"),
    ("/signin?invite=AAAA&error=oauth_failed",
     "/signin?invite=[redacted]&error=oauth_failed"),
    ("/api/books?world=1", "/api/books?world=1"),
])
def test_secret_query_values_are_redacted(before, after):
    assert redact(before) == after


def test_the_filter_rewrites_uvicorn_access_records():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/auth/email/verify?token=SECRETTOKEN", "1.1", 303), None)
    assert RedactQuerySecrets().filter(record)
    assert "SECRETTOKEN" not in record.getMessage()


def test_install_is_idempotent_and_attaches_to_the_access_logger():
    install_log_redaction()
    install_log_redaction()
    filters = [f for f in logging.getLogger("uvicorn.access").filters
               if isinstance(f, RedactQuerySecrets)]
    assert len(filters) == 1
