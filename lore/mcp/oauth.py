"""OAuth 2.1 for MCP clients (claude.ai and Claude Desktop custom connectors, Claude Code).

The MCP authorization spec, as the installed `mcp` SDK implements it: the SDK
serves the endpoints and checks the protocol (mcp.server.auth.routes and its
handlers); this module is the `OAuthAuthorizationServerProvider` behind them,
storing everything in the central database.

- Metadata: /.well-known/oauth-authorization-server (RFC 8414; PKCE S256 only)
  and /.well-known/oauth-protected-resource[/mcp] (RFC 9728), issuer = the
  public origin (LORE_BASE_URL).
- Dynamic client registration (POST /register, RFC 7591), open as the spec
  expects, with strict redirect URIs: absolute https, or http only on
  localhost / 127.0.0.1 / [::1]; no fragment, no user info, at most 10. Later
  requests must name one of them exactly. Registration is rate limited per
  address (lore.mcp.hosted).
- GET /authorize checks the client, the redirect URI, PKCE (S256; the SDK
  refuses anything else) and the RFC 8707 resource (it must be this server's
  /mcp), then sends the browser to LORE's own consent page with the request
  signed into the address (`req`, 10 minutes). The consent page needs the
  normal LORE sign-in; approving there mints the code for the signed-in user.
- POST /token: authorization_code (PKCE checked by the SDK; a code is single
  use, 5 minutes, bound to its client and redirect URI; a second use revokes
  everything the first one issued) and refresh_token (rotated on every use; a
  refresh token presented again after rotation revokes the whole grant).
- POST /revoke (RFC 7009) revokes the token's whole grant.

Tokens: access tokens last an hour, refresh tokens 30 days. Codes, access and
refresh tokens are 256 random bits stored as SHA-256 only. Every token belongs
to a grant: one user's consent to one client, listed in Settings and
revocable there. A confidential client's secret is sealed with the master key
(the token endpoint must compare it, so it cannot be a hash).

Time: the SDK compares expiries with the wall clock, so this provider's clock
is the wall clock too (`now`, a seam for tests).
"""
from __future__ import annotations

import ipaddress
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import anyio
import sqlalchemy as sa
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from itsdangerous import BadSignature, URLSafeTimedSerializer
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken,
    RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken as IssuedTokens
from pydantic import AnyHttpUrl, ValidationError

from lore.auth.tokens import hash_token
from lore.central.models import OAuthClient, OAuthCode, OAuthGrant, OAuthToken, User
from lore.mcp.hosted import MCP_PATH, PRINCIPAL_CLAIM, SCOPE, PatVerifier

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=5)
CONSENT_MAX_AGE = 10 * 60          # seconds: the signed request in the consent page's address
MAX_REDIRECT_URIS = 10
MAX_URI_CHARS = 2000
MAX_CLIENT_NAME = 200
MAX_GRANTS_PER_USER = 20           # past this the least recently used is revoked
MAX_TOKEN_CHARS = 128
ACCESS_PREFIX, REFRESH_PREFIX = "lore_at_", "lore_rt_"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
AUTH_METHODS = frozenset({"none", "client_secret_post", "client_secret_basic"})
LAST_USED_EVERY = timedelta(minutes=1)
PATHS = ("/authorize", "/token", "/register", "/revoke")
_CONSENT_SALT = "lore.mcp-consent"


class ConsentError(Exception):
    """The consent request is missing, tampered, expired or no longer valid."""


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _same_resource(a: str, b: str) -> bool:
    try:
        return str(AnyHttpUrl(a)).rstrip("/") == str(AnyHttpUrl(b)).rstrip("/")
    except ValidationError:
        return False


def redirect_uri_problem(uri: str) -> str | None:
    """Why `uri` may not be registered, or None. https anywhere; http only on
    the loopback hosts; nothing else (no custom schemes)."""
    if not isinstance(uri, str) or len(uri) > MAX_URI_CHARS:
        return "a redirect URI is at most 2000 characters"
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "a redirect URI must be an absolute URL"
    if parts.fragment or "#" in uri:
        return "a redirect URI may not have a fragment"
    if parts.username is not None or parts.password is not None:
        return "a redirect URI may not carry a user name or password"
    host = (parts.hostname or "").lower()
    if not host:
        return "a redirect URI must be an absolute URL"
    if parts.scheme == "https":
        return None
    if parts.scheme == "http":
        loopback = host in LOOPBACK_HOSTS
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        return None if loopback else "a redirect URI must be https (http only on localhost)"
    return "a redirect URI must be https (http only on localhost)"


class LoreOAuthProvider:
    """The authorization server behind the SDK's OAuth routes. `sessions()`
    gives the central sessionmaker; `auth_now` is the auth clock (sessions,
    personal tokens); `now` the wall clock the SDK also uses."""

    def __init__(self, sessions: Callable, master, settings, *, auth_now: Callable,
                 now: Callable[[], datetime] | None = None) -> None:
        self._sessions, self._master = sessions, master
        self.origin = settings.origin
        self.resource = self.origin + MCP_PATH
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._consent = URLSafeTimedSerializer(settings.session_secret, salt=_CONSENT_SALT)
        self._pat = PatVerifier(sessions, auth_now)

    # --- wiring -------------------------------------------------------------------

    @staticmethod
    def auth_options() -> dict:
        return {"client_registration_options": ClientRegistrationOptions(
                    enabled=True, default_scopes=[SCOPE]),
                "revocation_options": RevocationOptions(enabled=True)}

    @staticmethod
    def paths() -> tuple[str, ...]:
        return PATHS

    def _db(self):
        return self._sessions()()

    async def _run(self, fn, *args):
        return await anyio.to_thread.run_sync(fn, *args)

    # --- client secrets, sealed -----------------------------------------------------

    @staticmethod
    def _aad(client_id: str) -> bytes:
        return f"lore/oauth_clients/{client_id}".encode()

    def _seal(self, client_id: str, secret: str) -> tuple[bytes, bytes, str]:
        nonce = secrets.token_bytes(12)
        sealed = AESGCM(self._master.current).encrypt(nonce, secret.encode(),
                                                      self._aad(client_id))
        return sealed, nonce, self._master.version

    def _unseal(self, row: OAuthClient) -> str | None:
        if row.secret_ciphertext is None:
            return None
        key = self._master.for_version(row.key_version or "")
        if key is None:
            return None
        try:
            return AESGCM(key).decrypt(row.secret_nonce, row.secret_ciphertext,
                                       self._aad(row.client_id)).decode()
        except (InvalidTag, ValueError):
            return None

    # --- clients --------------------------------------------------------------------

    def _get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not isinstance(client_id, str) or not client_id or len(client_id) > 64:
            return None
        with self._db() as db:
            row = db.get(OAuthClient, client_id)
            if row is None:
                return None
            secret = self._unseal(row)
            if row.secret_ciphertext is not None and secret is None:
                return None  # a secret this server cannot open: the client must register again
            return OAuthClientInformationFull.model_validate(
                {**row.info, "client_id": row.client_id, "client_secret": secret})

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self._run(self._get_client, client_id)

    def _register(self, info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (info.redirect_uris or [])]
        if not uris or len(uris) > MAX_REDIRECT_URIS:
            raise RegistrationError("invalid_redirect_uri",
                                    f"register 1-{MAX_REDIRECT_URIS} redirect URIs")
        for uri in uris:
            problem = redirect_uri_problem(uri)
            if problem:
                raise RegistrationError("invalid_redirect_uri", problem)
        if (info.token_endpoint_auth_method or "client_secret_post") not in AUTH_METHODS:
            raise RegistrationError("invalid_client_metadata",
                                    "token_endpoint_auth_method must be none,"
                                    " client_secret_post or client_secret_basic")
        if not set(info.grant_types) <= {"authorization_code", "refresh_token"}:
            raise RegistrationError("invalid_client_metadata",
                                    "grant_types may be authorization_code and refresh_token")
        meta = info.model_dump(mode="json", exclude={"client_id", "client_secret"},
                               exclude_none=True)
        meta["redirect_uris"] = uris
        if meta.get("client_name"):
            meta["client_name"] = str(meta["client_name"])[:MAX_CLIENT_NAME]
        sealed = (self._seal(info.client_id, info.client_secret)
                  if info.client_secret else (None, None, None))
        with self._db() as db:
            db.add(OAuthClient(client_id=info.client_id, info=meta,
                               secret_ciphertext=sealed[0], secret_nonce=sealed[1],
                               key_version=sealed[2], created_at=self._now()))
            db.commit()

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._run(self._register, client_info)

    # --- authorize and consent ------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        if not params.redirect_uri_provided_explicitly:
            raise AuthorizeError("invalid_request", "redirect_uri is required")
        if params.resource is not None and not _same_resource(params.resource, self.resource):
            raise AuthorizeError("invalid_target", f"this server's resource is {self.resource}")
        req = self._consent.dumps({
            "c": client.client_id, "r": str(params.redirect_uri), "ch": params.code_challenge,
            "s": params.state, "sc": [SCOPE], "rs": params.resource,
        })
        return f"{self.origin}/oauth/consent?req={req}"

    def _pending(self, req: object) -> tuple[dict, OAuthClientInformationFull]:
        if not isinstance(req, str) or not req or len(req) > 8192:
            raise ConsentError("missing request")
        try:
            data = self._consent.loads(req, max_age=CONSENT_MAX_AGE)
        except BadSignature:
            raise ConsentError("expired or tampered request") from None
        client = self._get_client(data.get("c"))
        if client is None or data.get("r") not in [str(u) for u in client.redirect_uris or []]:
            raise ConsentError("unknown client")
        return data, client

    def describe(self, req: object) -> dict:
        """What the consent page shows (the client's own name is shown as text)."""
        data, client = self._pending(req)
        return {"client_name": (client.client_name or "")[:MAX_CLIENT_NAME] or None,
                "redirect_host": urlsplit(data["r"]).hostname or ""}

    def decide(self, req: object, user_id: str, approve: bool) -> str:
        """Where to send the browser: the client's redirect URI with a fresh code
        for `user_id`, or with error=access_denied."""
        data, client = self._pending(req)
        if not approve:
            return construct_redirect_uri(data["r"], error="access_denied",
                                          error_description="the reader declined",
                                          state=data.get("s"))
        code = secrets.token_urlsafe(32)
        now = self._now()
        with self._db() as db:
            user = db.get(User, user_id)
            if user is None or user.status != "active":
                raise ConsentError("not an active user")
            db.add(OAuthCode(code_hash=hash_token(code), client_id=client.client_id,
                             user_id=user_id, redirect_uri=data["r"],
                             redirect_uri_explicit=True, code_challenge=data["ch"],
                             scopes=list(data.get("sc") or [SCOPE]),
                             resource=data.get("rs"), created_at=now,
                             expires_at=now + CODE_TTL))
            db.execute(sa.delete(OAuthCode).where(OAuthCode.expires_at < now - timedelta(days=1)))
            db.commit()
        return construct_redirect_uri(data["r"], code=code, state=data.get("s"))

    # --- codes ----------------------------------------------------------------------

    def _revoke_grant(self, db, grant_id: str | None, now: datetime) -> None:
        if not grant_id:
            return
        db.execute(sa.update(OAuthGrant).where(OAuthGrant.id == grant_id,
                                               OAuthGrant.revoked_at.is_(None))
                   .values(revoked_at=now).execution_options(synchronize_session=False))
        db.execute(sa.update(OAuthToken).where(OAuthToken.grant_id == grant_id,
                                               OAuthToken.revoked_at.is_(None))
                   .values(revoked_at=now).execution_options(synchronize_session=False))

    def _load_code(self, client_id: str, code: str) -> AuthorizationCode | None:
        if not isinstance(code, str) or len(code) > MAX_TOKEN_CHARS:
            return None
        now = self._now()
        with self._db() as db:
            row = db.get(OAuthCode, hash_token(code))
            if row is None or row.client_id != client_id:
                return None
            if row.used_at is not None:  # replayed: whatever it issued is revoked
                self._revoke_grant(db, row.grant_id, now)
                db.commit()
                return None
            if _utc(row.expires_at) <= now:
                return None
            user = db.get(User, row.user_id)
            if user is None or user.status != "active":
                return None
            return AuthorizationCode(
                code=code, scopes=list(row.scopes), expires_at=_utc(row.expires_at).timestamp(),
                client_id=row.client_id, code_challenge=row.code_challenge,
                redirect_uri=row.redirect_uri,
                redirect_uri_provided_explicitly=row.redirect_uri_explicit,
                resource=row.resource, subject=row.user_id)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        return await self._run(self._load_code, client.client_id, authorization_code)

    def _issue(self, db, grant: OAuthGrant, now: datetime) -> IssuedTokens:
        access = ACCESS_PREFIX + secrets.token_urlsafe(32)
        refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
        db.add(OAuthToken(token_hash=hash_token(access), grant_id=grant.id, kind="access",
                          created_at=now, expires_at=now + ACCESS_TTL))
        db.add(OAuthToken(token_hash=hash_token(refresh), grant_id=grant.id, kind="refresh",
                          created_at=now, expires_at=now + REFRESH_TTL))
        return IssuedTokens(access_token=access, expires_in=int(ACCESS_TTL.total_seconds()),
                          scope=" ".join(grant.scopes), refresh_token=refresh)

    def _exchange_code(self, client: OAuthClientInformationFull,
                       code: AuthorizationCode) -> IssuedTokens:
        now = self._now()
        with self._db() as db:
            h = hash_token(code.code)
            claimed = db.execute(sa.update(OAuthCode).where(
                OAuthCode.code_hash == h, OAuthCode.client_id == client.client_id,
                OAuthCode.used_at.is_(None), OAuthCode.expires_at > now)
                .values(used_at=now).execution_options(synchronize_session=False))
            if (claimed.rowcount or 0) != 1:
                db.rollback()
                raise TokenError("invalid_grant", "authorization code does not exist")
            row = db.get(OAuthCode, h)
            live = db.scalars(sa.select(OAuthGrant).where(
                OAuthGrant.user_id == row.user_id, OAuthGrant.revoked_at.is_(None))
                .order_by(sa.func.coalesce(OAuthGrant.last_used_at, OAuthGrant.created_at),
                          OAuthGrant.id)).all()
            for old in live[:max(0, len(live) - MAX_GRANTS_PER_USER + 1)]:
                self._revoke_grant(db, old.id, now)
            grant = OAuthGrant(id=uuid.uuid4().hex, user_id=row.user_id,
                               client_id=client.client_id,
                               client_name=(client.client_name or "")[:MAX_CLIENT_NAME]
                               or "An MCP client",
                               scopes=list(row.scopes), created_at=now)
            db.add(grant)
            db.flush()
            row.grant_id = grant.id
            tokens = self._issue(db, grant, now)
            db.commit()
            return tokens

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> IssuedTokens:
        return await self._run(self._exchange_code, client, authorization_code)

    # --- refresh tokens -------------------------------------------------------------

    def _load_refresh(self, client_id: str, token: str) -> RefreshToken | None:
        if not isinstance(token, str) or not token.startswith(REFRESH_PREFIX) \
                or len(token) > MAX_TOKEN_CHARS:
            return None
        now = self._now()
        with self._db() as db:
            row = db.get(OAuthToken, hash_token(token))
            if row is None or row.kind != "refresh":
                return None
            grant = db.get(OAuthGrant, row.grant_id)
            if grant is None or grant.client_id != client_id:
                return None
            if row.revoked_at is not None:
                # A rotated-away refresh token came back: someone holds a copy.
                self._revoke_grant(db, grant.id, now)
                db.commit()
                return None
            if grant.revoked_at is not None or _utc(row.expires_at) <= now:
                return None
            user = db.get(User, grant.user_id)
            if user is None or user.status != "active":
                return None
            return RefreshToken(token=token, client_id=grant.client_id,
                                scopes=list(grant.scopes),
                                expires_at=int(_utc(row.expires_at).timestamp()),
                                subject=grant.user_id)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        return await self._run(self._load_refresh, client.client_id, refresh_token)

    def _exchange_refresh(self, client: OAuthClientInformationFull,
                          refresh: RefreshToken) -> IssuedTokens:
        now = self._now()
        with self._db() as db:
            h = hash_token(refresh.token)
            row = db.get(OAuthToken, h)
            grant = db.get(OAuthGrant, row.grant_id) if row is not None else None
            if grant is None or grant.client_id != client.client_id or grant.revoked_at:
                raise TokenError("invalid_grant", "refresh token does not exist")
            rotated = db.execute(sa.update(OAuthToken).where(
                OAuthToken.token_hash == h, OAuthToken.revoked_at.is_(None))
                .values(revoked_at=now).execution_options(synchronize_session=False))
            if (rotated.rowcount or 0) != 1:
                db.rollback()
                raise TokenError("invalid_grant", "refresh token does not exist")
            # The access tokens of the grant go with the refresh token they came with.
            db.execute(sa.update(OAuthToken).where(
                OAuthToken.grant_id == grant.id, OAuthToken.kind == "access",
                OAuthToken.revoked_at.is_(None)).values(revoked_at=now)
                .execution_options(synchronize_session=False))
            tokens = self._issue(db, grant, now)
            db.execute(sa.delete(OAuthToken).where(OAuthToken.grant_id == grant.id,
                                                   OAuthToken.expires_at < now))
            db.commit()
            return tokens

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken,
                                     scopes: list[str]) -> IssuedTokens:
        return await self._run(self._exchange_refresh, client, refresh_token)

    # --- access tokens --------------------------------------------------------------

    def _load_access(self, token: str) -> AccessToken | None:
        pat = self._pat.verify_sync(token)
        if pat is not None:
            return pat
        if not isinstance(token, str) or not token.startswith(ACCESS_PREFIX) \
                or len(token) > MAX_TOKEN_CHARS:
            return None
        now = self._now()
        with self._db() as db:
            row = db.get(OAuthToken, hash_token(token))
            if row is None or row.kind != "access" or row.revoked_at is not None \
                    or _utc(row.expires_at) <= now:
                return None
            grant = db.get(OAuthGrant, row.grant_id)
            if grant is None or grant.revoked_at is not None:
                return None
            user = db.get(User, grant.user_id)
            if user is None or user.status != "active":
                return None
            if grant.last_used_at is None or now - _utc(grant.last_used_at) >= LAST_USED_EVERY:
                grant.last_used_at = now
                db.commit()
            return AccessToken(token=token, client_id=grant.client_id,
                               scopes=list(grant.scopes),
                               expires_at=int(_utc(row.expires_at).timestamp()),
                               resource=self.resource, subject=grant.user_id,
                               claims={PRINCIPAL_CLAIM: f"oauth:{grant.id}"})

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await self._run(self._load_access, token)

    def _revoke(self, token: AccessToken | RefreshToken) -> None:
        if not isinstance(token.token, str) or token.client_id.startswith("pat:"):
            return  # personal access tokens are revoked in Settings, not here
        with self._db() as db:
            row = db.get(OAuthToken, hash_token(token.token))
            if row is not None:
                self._revoke_grant(db, row.grant_id, self._now())
                db.commit()

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self._run(self._revoke, token)

    # --- what Settings lists ----------------------------------------------------------

    def connections(self, user_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.scalars(sa.select(OAuthGrant).where(
                OAuthGrant.user_id == user_id, OAuthGrant.revoked_at.is_(None))
                .order_by(OAuthGrant.created_at, OAuthGrant.id)).all()
            return [{"id": g.id, "client_name": g.client_name,
                     "created_at": _utc(g.created_at).isoformat(),
                     "last_used_at": _utc(g.last_used_at).isoformat()
                     if g.last_used_at else None} for g in rows]

    def disconnect(self, user_id: str, grant_id: str) -> bool:
        with self._db() as db:
            grant = db.get(OAuthGrant, grant_id[:64])
            if grant is None or grant.user_id != user_id or grant.revoked_at is not None:
                return False
            self._revoke_grant(db, grant.id, self._now())
            db.commit()
            return True


def rotate_client_secrets(sessions, master) -> int:
    """Re-seal every OAuth client secret under the current master key (part of
    `lore admin rotate-keys`); returns how many were re-sealed. A secret sealed
    under a key this server no longer has is left: that client registers again."""
    done = 0
    with sessions() as db:
        rows = db.scalars(sa.select(OAuthClient).where(
            OAuthClient.secret_ciphertext.is_not(None),
            OAuthClient.key_version != master.version)).all()
        for row in rows:
            key = master.for_version(row.key_version or "")
            if key is None:
                continue
            aad = LoreOAuthProvider._aad(row.client_id)
            try:
                plain = AESGCM(key).decrypt(row.secret_nonce, row.secret_ciphertext, aad)
            except (InvalidTag, ValueError):
                continue
            nonce = secrets.token_bytes(12)
            row.secret_ciphertext = AESGCM(master.current).encrypt(nonce, plain, aad)
            row.secret_nonce, row.key_version = nonce, master.version
            done += 1
        db.commit()
    return done
