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
                 timeout: float = 10.0) -> None:
        self._key, self._from = api_key, sender
        self._transport, self._timeout = transport, timeout

    def __repr__(self) -> str:  # never show the key
        return f"ResendMailer(from={self._from!r})"

    def send(self, *, to: str, subject: str, text: str,
             html: str | None = None) -> None:
        payload = {"from": self._from, "to": [to], "subject": subject, "text": text}
        if html:
            payload["html"] = html
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


def sign_in_message(link: str) -> tuple[str, str, str]:
    """(subject, text, html) for a sign-in link."""
    text = ("Open this link to sign in to LORE. It works once and expires in"
            f" 15 minutes.\n\n{link}\n\nIf you did not ask for it, ignore this email.")
    html = ("<p>Open this link to sign in to LORE. It works once and expires in"
            f' 15 minutes.</p><p><a href="{link}">Sign in to LORE</a></p>'
            "<p>If you did not ask for it, ignore this email.</p>")
    return "Your LORE sign-in link", text, html


def invite_message(link: str) -> tuple[str, str, str]:
    text = ("You're invited to the LORE beta. Open this link to sign in and"
            f" create your account:\n\n{link}\n\nThe invitation works once.")
    html = ("<p>You're invited to the LORE beta. Open this link to sign in and"
            f' create your account.</p><p><a href="{link}">Join LORE</a></p>'
            "<p>The invitation works once.</p>")
    return "You're invited to LORE", text, html
