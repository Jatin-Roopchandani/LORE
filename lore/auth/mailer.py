"""Sending mail. The app depends on the `Mailer` protocol; production uses
Resend, tests a fake. Nothing here logs an address, a link or a body."""
from __future__ import annotations

from typing import Protocol

import httpx

RESEND_URL = "https://api.resend.com/emails"


class MailError(RuntimeError):
    """The message could not be sent. The text never includes the content."""


class Mailer(Protocol):
    def send(self, *, to: str, subject: str, text: str,
             html: str | None = None) -> None: ...


class ResendMailer:
    def __init__(self, api_key: str, sender: str, *,
                 transport: httpx.BaseTransport | None = None,
                 timeout: float = 10.0, reply_to: str | None = None) -> None:
        self._key, self._from, self._reply_to = api_key, sender, reply_to
        self._transport, self._timeout = transport, timeout

    def __repr__(self) -> str:  # never show the key
        return f"ResendMailer(from={self._from!r})"

    def send(self, *, to: str, subject: str, text: str,
             html: str | None = None) -> None:
        payload = {"from": self._from, "to": [to], "subject": subject, "text": text}
        if html:
            payload["html"] = html
        if self._reply_to:
            payload["reply_to"] = self._reply_to
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as http:
                response = http.post(RESEND_URL, json=payload,
                                     headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as exc:
            raise MailError(f"mail service unreachable ({type(exc).__name__})") from None
        if response.status_code >= 300:
            raise MailError(f"mail service answered {response.status_code}")


class UnconfiguredMailer:
    """Used when no Resend key is set: sending fails loudly, not silently."""

    def send(self, *, to: str, subject: str, text: str,
             html: str | None = None) -> None:
        raise MailError("no mail service is configured (LORE_RESEND_API_KEY)")



class DevMailboxMailer:
    """Local development only (`LORE_MAIL_CONSOLE=1` with a localhost
    `LORE_BASE_URL`): each message is appended to a mailbox file instead of
    being sent, so sign-in links can be tried without a mail service. It
    refuses any non-local site, so it can never stand in for real mail."""

    def __init__(self, path, base_url: str) -> None:
        from pathlib import Path
        from urllib.parse import urlsplit
        host = (urlsplit(base_url).hostname or "").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise MailError("the dev mailbox is for a localhost LORE_BASE_URL only")
        self.path = Path(path)

    def __repr__(self) -> str:
        return f"DevMailboxMailer({str(self.path)!r})"

    def send(self, *, to: str, subject: str, text: str,
             html: str | None = None) -> None:
        from datetime import datetime, timezone
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self.path.open("a", encoding="utf-8") as box:
            box.write(f"=== {stamp}\nTo: {to}\nSubject: {subject}\n\n{text}\n\n")


def _origin(link: str) -> str:
    """https://host of a link LORE made (the logo is served from the same site)."""
    from urllib.parse import urlsplit
    parts = urlsplit(link)
    return f"{parts.scheme}://{parts.netloc}"


def branded_html(link: str, *, heading: str, lead: str, button: str, foot: str) -> str:
    """A small, email-safe page: the LORE logo, a heading, one line, one
    button and a plain link below it (for clients that block buttons). Inline
    styles and a table layout only, which every mail client renders; the logo
    is an image on LORE's own site, so it shows once images are allowed."""
    from html import escape
    origin, url = escape(_origin(link), quote=True), escape(link, quote=True)
    text = "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#17212B"
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:#ECEFF2">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#ECEFF2;padding:32px 12px"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:480px;background:#ffffff;border-radius:12px;padding:32px">'
        f'<tr><td align="left" style="padding-bottom:20px">'
        f'<img src="{origin}/icon-192.png" width="48" height="48" alt="LORE" '
        'style="display:inline-block;border-radius:10px;vertical-align:middle">'
        f'<span style="{text};font-size:20px;font-weight:700;letter-spacing:.04em;'
        'vertical-align:middle;padding-left:12px">LORE</span></td></tr>'
        f'<tr><td style="{text};font-size:22px;font-weight:700;padding-bottom:10px">'
        f'{escape(heading)}</td></tr>'
        f'<tr><td style="{text};font-size:15px;line-height:1.55;color:#3D4A56;'
        f'padding-bottom:24px">{escape(lead)}</td></tr>'
        f'<tr><td style="padding-bottom:24px"><a href="{url}" '
        'style="display:inline-block;background:#1E5AA0;color:#ffffff;text-decoration:none;'
        f'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:15px;'
        f'font-weight:600;padding:12px 22px;border-radius:8px">{escape(button)}</a></td></tr>'
        f'<tr><td style="{text};font-size:12px;line-height:1.5;color:#5A6774">'
        f'Or open this link: <a href="{url}" style="color:#1E5AA0;word-break:break-all">{url}</a>'
        f'<br><br>{escape(foot)}</td></tr>'
        '</table></td></tr></table></body></html>')


def sign_in_message(link: str) -> tuple[str, str, str]:
    """(subject, text, html) for a sign-in link."""
    text = ("Open this link to sign in to LORE. It works once and expires in"
            f" 15 minutes.\n\n{link}\n\nIf you did not ask for it, ignore this email.")
    html = branded_html(
        link, heading="Sign in to LORE",
        lead="Use the button below to sign in. The link works once and expires in 15 minutes.",
        button="Sign in to LORE", foot="If you did not ask for this, you can ignore this email.")
    return "Your LORE sign-in link", text, html


def invite_message(link: str) -> tuple[str, str, str]:
    text = ("You're invited to the LORE beta. Open this link to sign in and"
            f" create your account:\n\n{link}\n\nThe invitation works once.")
    html = branded_html(
        link, heading="You're invited to LORE",
        lead=("Your place in the LORE beta is ready. Ask about the books you own and LORE"
              " answers from the text, with every sentence cited."),
        button="Join LORE", foot="The invitation works once. Sign in with the email it was sent to.")
    return "You're invited to LORE", text, html
