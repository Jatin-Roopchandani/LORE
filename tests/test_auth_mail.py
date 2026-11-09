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


def test_the_dev_mailbox_writes_messages_to_a_file_on_localhost_only(tmp_path):
    from lore.auth.mailer import DevMailboxMailer, MailError
    box = DevMailboxMailer(tmp_path / "dev-mailbox.txt", "http://localhost:8767")
    box.send(to="a@example.com", subject="Your LORE sign-in link", text="open http://localhost:8767/x")
    written = (tmp_path / "dev-mailbox.txt").read_text()
    assert "To: a@example.com" in written and "http://localhost:8767/x" in written
    for public in ("https://lore.jrvn.site", "http://192.168.1.4:8767", "https://example.com"):
        try:
            DevMailboxMailer(tmp_path / "x.txt", public)
        except MailError:
            continue
        raise AssertionError(f"the dev mailbox accepted {public}")


def test_a_reply_to_is_sent_only_when_set():
    import json
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "1"})

    ResendMailer("k", "LORE <login@example.org>", transport=httpx.MockTransport(handler),
                 reply_to="hello@example.org").send(to="a@example.org", subject="s", text="t")
    mailer(handler).send(to="a@example.org", subject="s", text="t")
    assert seen[0]["reply_to"] == "hello@example.org"
    assert "reply_to" not in seen[1]


@pytest.mark.parametrize("make", ["sign_in", "invite"])
def test_both_emails_carry_the_logo_a_button_and_a_plain_text_part(make):
    from lore.auth import mailer as m
    link = "https://lore.example.org/signin?invite=abc&x=<1>"
    subject, text, html = (m.sign_in_message if make == "sign_in" else m.invite_message)(link)
    assert '<img src="https://lore.example.org/icon-192.png"' in html  # served by LORE itself
    assert 'alt="LORE"' in html
    assert link in text                                  # the plain-text part has the link
    assert "<1>" not in html and "&lt;1&gt;" in html      # the link is escaped in the HTML
    assert html.count('href="https://lore.example.org/signin?invite=abc&amp;x=&lt;1&gt;"') == 2
    assert "<style" not in html and "<script" not in html  # inline styles only


def test_the_reply_to_setting_comes_from_the_environment():
    from lore.auth.settings import AuthSettings
    base = {"LORE_SESSION_SECRET": "s" * 40, "LORE_CSRF_SECRET": "c" * 40,
            "LORE_BASE_URL": "https://lore.example.org"}
    assert AuthSettings.from_env(base).mail_reply_to is None
    with_reply = AuthSettings.from_env({**base, "LORE_MAIL_REPLY_TO": " hi@example.org "})
    assert with_reply.mail_reply_to == "hi@example.org"
