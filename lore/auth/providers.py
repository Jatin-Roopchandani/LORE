"""OAuth 2 sign-in with Google and GitHub, on Authlib's client.

Both providers get the authorization-code flow with PKCE (S256) and a `state`
the route checks on the way back. A provider turns a code into a `Profile`;
`email_verified` is only ever True when the provider itself says so, and the
caller signs nobody in without it.

Tests substitute a fake `Provider`; the real ones also take an httpx
transport, which is how their request handling is tested without a network.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from authlib.common.errors import AuthlibBaseError
from authlib.integrations.httpx_client import OAuth2Client

from lore.auth.settings import OAuthClient

# Authlib's httpx client runs on httpx2 when it is installed (it is, through
# the lockfile) and on httpx otherwise; its transports and errors must come
# from the same module, so resolve it the way Authlib does.
try:
    import httpx2 as _httpx
except ImportError:  # pragma: no cover
    import httpx as _httpx


class ProviderError(RuntimeError):
    """The provider refused, was unreachable or returned something unusable."""


@dataclass(frozen=True)
class Profile:
    subject: str  # the provider's stable id for the person, never the email
    email: str | None
    email_verified: bool
    name: str | None


class Provider(Protocol):
    name: str

    def authorize_url(self, *, redirect_uri: str, state: str,
                      code_verifier: str) -> str: ...

    def fetch_profile(self, *, code: str, code_verifier: str,
                      redirect_uri: str) -> Profile: ...


class _Base:
    name = ""
    authorize_endpoint = ""
    token_endpoint = ""
    scope = ""

    def __init__(self, client: OAuthClient, *,
                 transport: _httpx.BaseTransport | None = None) -> None:
        self._client_config, self._transport = client, transport

    def __repr__(self) -> str:
        return f"{type(self).__name__}(client_id={self._client_config.client_id!r})"

    def _client(self, redirect_uri: str) -> OAuth2Client:
        return OAuth2Client(
            client_id=self._client_config.client_id,
            client_secret=self._client_config.client_secret,
            token_endpoint_auth_method="client_secret_post",
            scope=self.scope, redirect_uri=redirect_uri,
            code_challenge_method="S256",  # PKCE is not optional
            transport=self._transport, timeout=10.0)

    def authorize_url(self, *, redirect_uri: str, state: str,
                      code_verifier: str) -> str:
        with self._client(redirect_uri) as client:
            url, _ = client.create_authorization_url(
                self.authorize_endpoint, state=state, code_verifier=code_verifier,
                **self._authorize_extra())
        return url

    def _authorize_extra(self) -> dict:
        return {}

    def fetch_profile(self, *, code: str, code_verifier: str,
                      redirect_uri: str) -> Profile:
        try:
            with self._client(redirect_uri) as client:
                client.fetch_token(
                    self.token_endpoint, code=code, code_verifier=code_verifier,
                    grant_type="authorization_code",
                    headers={"Accept": "application/json"})
                return self._profile(client)
        except (AuthlibBaseError, _httpx.HTTPError, ValueError, KeyError,
                TypeError) as exc:
            # The type only: a message can quote the response, which can hold tokens.
            raise ProviderError(f"{self.name} sign-in failed ({type(exc).__name__})"
                                ) from None

    def _profile(self, client: OAuth2Client) -> Profile:
        raise NotImplementedError


def _get_json(client: OAuth2Client, url: str, accept: str = "application/json"):
    response = client.get(url, headers={"Accept": accept})
    response.raise_for_status()
    return response.json()


class GoogleProvider(_Base):
    name = "google"
    authorize_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    userinfo_endpoint = "https://openidconnect.googleapis.com/v1/userinfo"
    scope = "openid email profile"

    def _authorize_extra(self) -> dict:
        return {"prompt": "select_account"}

    def _profile(self, client: OAuth2Client) -> Profile:
        info = _get_json(client, self.userinfo_endpoint)
        verified = info.get("email_verified")
        return Profile(
            subject=str(info["sub"]), email=info.get("email"),
            email_verified=verified is True or verified == "true",
            name=info.get("name"))


class GitHubProvider(_Base):
    name = "github"
    authorize_endpoint = "https://github.com/login/oauth/authorize"
    token_endpoint = "https://github.com/login/oauth/access_token"
    user_endpoint = "https://api.github.com/user"
    emails_endpoint = "https://api.github.com/user/emails"
    scope = "read:user user:email"

    def _profile(self, client: OAuth2Client) -> Profile:
        accept = "application/vnd.github+json"
        user = _get_json(client, self.user_endpoint, accept)
        emails = _get_json(client, self.emails_endpoint, accept)
        # The address on the profile is not trusted (it can be unverified):
        # only the primary address that GitHub marks verified counts.
        primary = next((e for e in emails if isinstance(e, dict)
                        and e.get("primary") is True and e.get("verified") is True
                        and e.get("email")), None)
        return Profile(
            subject=str(user["id"]),
            email=primary["email"] if primary else None,
            email_verified=primary is not None,
            name=user.get("name") or user.get("login"))


def default_providers(google: OAuthClient | None, github: OAuthClient | None,
                      ) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    if google:
        providers["google"] = GoogleProvider(google)
    if github:
        providers["github"] = GitHubProvider(github)
    return providers
