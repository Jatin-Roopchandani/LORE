"""The hosted MCP endpoint: `/mcp` on a multi-user LORE, one server for every user.

`lore mcp` (lore.mcp.server) stays local-only and unchanged: it serves the one
library it is given and has no notion of a user. This module is its multi-user
counterpart, mounted inside the web app (lore.web.app) when LORE_MULTI_USER is
on. Streamable HTTP, stateless, JSON responses: nothing is kept between
requests, so any web process can answer any call.

Who is asking. Every request must carry `Authorization: Bearer <token>`; the
SDK's bearer middleware (mcp.server.auth) asks our verifier, and a request
with no valid token is 401 before any tool runs. A token is either a personal
access token (`lore_...`, lore.central.mcp_tokens) or, with OAuth on, an
access token LORE issued to a connected client (lore.mcp.oauth). Either way
the verifier puts the user id in the token's `subject` and our own principal
id (`pat:<id>` or `oauth:<grant id>`) in its claims; tools read it back with
the SDK's `get_access_token()`. No tool takes a user, a path or a data
directory as an argument: the library is always `config_for(base, user)`, the
same choke point every web route uses (lore.tenancy).

What a tool may do:
- the six read tools of `lore mcp`, minus `cypher_query` (see below);
- `lore_search` runs with the user's OWN provider key bound
  (lore.web.keys.with_user_key, the same wrapper as /api/ask), takes their
  question slots and per-minute allowance (lore.web.throttle), and never
  reaches the server's key: with no user key bound, multi mode refuses
  (lore.inference.key_scope);
- every read applies the world's reading position unless the reader passes
  `ignore_reading_position`, exactly as in the web app;
- inputs are bounded (a question 2,000 characters, a name or id 200), the
  passage search returns at most 20 passages, and any result over 256 KB is
  refused with `too_large` rather than sent;
- errors and answers are the public ones (lore.web.friendly): no model name,
  provider text, path or trace.

`cypher_query` is NOT offered here. Its guard (lore.mcp.guards) is careful,
but its own notes say one shape of bypass is closed only because today's Kuzu
parser refuses it, and an engine crash takes down the whole process -- here,
the web server every user shares. It also reads the raw graph with no reading
position. The structured tools cover what a reader needs; raw Cypher stays a
local-mode tool.

Rate limits: every call counts against its token's and its user's per-minute
limits (lore.web.throttle, shared by every web process through the central
database). A refused call is the `rate_limited` error with `retry_after`.

Logging: one line per call -- tool, principal id, user id, outcome code,
time, result size. Never the arguments (a question, a name) and never any of
the result (book text).
"""
from __future__ import annotations

import contextlib
import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import anyio
from fastapi.responses import JSONResponse
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from lore.central import mcp_tokens
from lore.config import LoreConfig
from lore.mcp import tools
from lore.tenancy import InvalidUserId, config_for, validate_user_id

log = logging.getLogger("lore.mcp.hosted")

HOSTED_TOOLS = ("lore_search", "vector_search", "entity", "subgraph", "library",
                "ingest_status")
MCP_PATH = "/mcp"
SCOPE = "lore"
MAX_QUERY_CHARS = 2000
MAX_NAME_CHARS = 200
MAX_REF_CHARS = 200         # a world's id or name, a book id
VECTOR_LIMIT_MAX = 20       # passages per vector_search (local mode allows 50)
MAX_RESULT_BYTES = 256 * 1024
PRINCIPAL_CLAIM = "lore_principal"

INSTRUCTIONS = (
    "LORE reads the signed-in reader's own library of e-books as a knowledge"
    " graph. Use lore_search for questions (it answers with quoted citations and"
    " spends the reader's own AI provider allowance); library, entity, subgraph"
    " and vector_search are free lookups. The reader's reading position applies"
    " unless they ask to see everything (ignore_reading_position)."
)


@dataclass(frozen=True)
class Principal:
    """Who is calling: the user, and the credential (`pat:<id>` or
    `oauth:<grant id>`) that rate limits and log lines are keyed by."""
    user_id: str
    token_id: str


def principal_of(token: AccessToken | None) -> Principal | None:
    """The caller of this request, from the verified token, or None."""
    if token is None or not token.subject:
        return None
    pid = (token.claims or {}).get(PRINCIPAL_CLAIM)
    if not isinstance(pid, str) or not pid:
        return None
    try:
        return Principal(validate_user_id(token.subject), pid)
    except InvalidUserId:
        return None


def pat_access_token(row) -> AccessToken:
    """The SDK's view of a live personal access token. `token` is the row's id,
    not the secret: nothing downstream needs the secret again."""
    pid = f"pat:{row.id}"
    return AccessToken(token=pid, client_id=pid, scopes=[SCOPE], subject=row.user_id,
                       claims={PRINCIPAL_CLAIM: pid})


class PatVerifier(TokenVerifier):
    """Bearer tokens that are personal access tokens (lore.central.mcp_tokens)."""

    def __init__(self, sessions: Callable, now: Callable) -> None:
        self._sessions, self._now = sessions, now

    def verify_sync(self, token: str) -> AccessToken | None:
        if not mcp_tokens.looks_like(token):
            return None
        with self._sessions()() as db:
            row = mcp_tokens.resolve(db, token, self._now())
            db.commit()
            return None if row is None else pat_access_token(row)

    async def verify_token(self, token: str) -> AccessToken | None:
        return await anyio.to_thread.run_sync(self.verify_sync, token)


def _bypass(spoilers: bool) -> dict:
    return {"spoilers": True} if spoilers else {}


def _bad(message: str) -> dict:
    return {"error": {"code": "bad_request", "message": message}}


def _check_inputs(args: dict) -> dict | None:
    """The `bad_request` error for an argument out of bounds, else None."""
    query = args.get("query")
    if query is not None and (not query.strip() or len(query) > MAX_QUERY_CHARS):
        return _bad(f"a query is 1-{MAX_QUERY_CHARS} characters")
    for key in ("name", "entity"):
        value = args.get(key)
        if value is not None and (not value.strip() or len(value) > MAX_NAME_CHARS):
            return _bad(f"a name is 1-{MAX_NAME_CHARS} characters")
    for key in ("world", "book_id"):
        value = args.get(key)
        if value is not None and len(value) > MAX_REF_CHARS:
            return _bad(f"{key} is at most {MAX_REF_CHARS} characters")
    return None


class HostedMcp:
    """The hosted server and its ASGI app. `__call__` serves the routes it
    owns (`handles`); the web app sends those paths here and everything else
    to FastAPI. `running()` must wrap the app's lifetime (the web app's
    lifespan): the SDK's session manager needs its task group."""

    def __init__(self, base: LoreConfig, *, sessions: Callable, key_service, throttle,
                 origin: str, hosts: set[str], origins: set[str], now: Callable,
                 provider=None, verifier: TokenVerifier | None = None) -> None:
        self.base, self.key_service, self.throttle = base, key_service, throttle
        self.origin = origin.rstrip("/")
        self.provider = provider
        auth = AuthSettings(
            issuer_url=self.origin, resource_server_url=self.origin + MCP_PATH,
            # Tokens are bound to this server by our own authorization server
            # (lore.mcp.oauth checks the RFC 8707 resource when it issues one);
            # personal access tokens have no audience but this one.
            validate_token_resource=False,
            **(provider.auth_options() if provider is not None else {}))
        if provider is not None:
            self.server = MCPServer("lore", instructions=INSTRUCTIONS,
                                    auth_server_provider=provider, auth=auth)
        else:
            self.server = MCPServer("lore", instructions=INSTRUCTIONS, auth=auth,
                                    token_verifier=verifier or PatVerifier(sessions, now))
        self._security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=sorted(hosts),
            allowed_origins=sorted(origins))
        self._register()
        self.app = self._build()
        self._live = False
        self._paths = {MCP_PATH, MCP_PATH + "/"}
        self._prefixes = ("/.well-known/oauth-protected-resource",)
        if provider is not None:
            self._paths |= set(provider.paths())
            self._prefixes += ("/.well-known/oauth-authorization-server",)

    # --- serving ------------------------------------------------------------------

    def _build(self):
        return self.server.streamable_http_app(
            streamable_http_path=MCP_PATH, stateless_http=True, json_response=True,
            transport_security=self._security)

    def handles(self, path: str) -> bool:
        return path in self._paths or path.startswith(self._prefixes)

    @contextlib.asynccontextmanager
    async def running(self):
        """The SDK's session manager runs once per instance, so each start
        builds a fresh app (a test client may start the app more than once)."""
        self.app = self._build()
        async with self.server.session_manager.run():
            self._live = True
            try:
                yield
            finally:
                self._live = False

    async def __call__(self, scope, receive, send):
        if not self._live and scope.get("path", "").rstrip("/") == MCP_PATH:
            body = b'{"error": {"code": "unavailable", "message": "starting up"}}'
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"retry-after", b"5")]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)

    # --- one call -----------------------------------------------------------------

    async def _call(self, tool: str, impl: Callable[..., Awaitable[dict]], **args) -> dict:
        started = time.monotonic()
        principal = principal_of(get_access_token())
        if principal is None:  # the bearer middleware already refused; belt and braces
            return self._finish(tool, None, started,
                                {"error": {"code": "unauthorized", "message": ""}})
        out = _check_inputs(args)
        if out is None:
            wait = await anyio.to_thread.run_sync(
                self.throttle.mcp_wait, principal.token_id, principal.user_id)
            if wait is not None:
                seconds = max(1, int(wait + 0.999))
                out = {"error": {"code": "rate_limited", "retry_after": seconds,
                                 "message": f"Too many LORE calls a minute. Try again in"
                                            f" {seconds} seconds."}}
        if out is None:
            try:
                cfg = await anyio.to_thread.run_sync(config_for, self.base, principal.user_id)
                out = await impl(cfg, principal, **args)
            except Exception:  # noqa: BLE001 -- logged; the client gets the public 'internal'
                log.exception("mcp tool %s failed for %s", tool, principal.token_id)
                out = {"error": {"code": "internal", "message": ""}}
        return self._finish(tool, principal, started, out)

    def _finish(self, tool: str, principal: Principal | None, started: float,
                out: dict) -> dict:
        from lore.web import friendly
        if not isinstance(out, dict):
            out = {"error": {"code": "internal", "message": ""}}
        if "error" in out:
            out = {"error": friendly.public_error(out["error"])}
        elif tool == "lore_search":
            out = friendly.public_result(out)
        size = len(json.dumps(out, default=str))
        if size > MAX_RESULT_BYTES:
            out = {"error": friendly.public_error({
                "code": "too_large",
                "message": f"The result is over {MAX_RESULT_BYTES // 1024} KB. Narrow the"
                           " request: a smaller depth or limit, or one world."})}
            size = len(json.dumps(out))
        outcome = out["error"]["code"] if "error" in out else "ok"
        log.info("mcp %s by %s (user %s): %s in %d ms, %d bytes", tool,
                 principal.token_id if principal else "-",
                 principal.user_id if principal else "-", outcome,
                 int((time.monotonic() - started) * 1000), size)
        return out

    # --- the tools ----------------------------------------------------------------

    async def _lore_search(self, cfg, principal: Principal, *, query, world, depth,
                           spoilers) -> dict:
        from lore.web.keys import with_user_key
        grant = await anyio.to_thread.run_sync(self.throttle.start_question,
                                               principal.user_id)
        if isinstance(grant, JSONResponse):  # busy or rate_limited, as /api/ask says it
            body = json.loads(grant.body)
            return {"error": body["error"]}
        try:
            search = with_user_key(principal.user_id, self.key_service, tools.lore_search)
            return await search(cfg, query, world=world, depth=depth, **_bypass(spoilers))
        finally:
            await anyio.to_thread.run_sync(grant.release)

    async def _vector_search(self, cfg, principal, *, query, book_id, limit, world,
                             spoilers) -> dict:
        limit = max(1, min(int(limit), VECTOR_LIMIT_MAX))
        return await tools.vector_search(cfg, query, book_id, limit, world=world,
                                         **_bypass(spoilers))

    @staticmethod
    async def _sync(fn, cfg, *args, **kwargs) -> dict:
        return await anyio.to_thread.run_sync(functools.partial(fn, cfg, *args, **kwargs))

    async def _entity(self, cfg, principal, *, name, world, spoilers) -> dict:
        return await self._sync(tools.entity, cfg, name, world=world, **_bypass(spoilers))

    async def _subgraph(self, cfg, principal, *, entity, depth, world, spoilers) -> dict:
        return await self._sync(tools.subgraph, cfg, entity, depth, world=world,
                                **_bypass(spoilers))

    async def _library(self, cfg, principal, *, world, spoilers) -> dict:
        return await self._sync(tools.library, cfg, world=world, **_bypass(spoilers))

    async def _ingest_status(self, cfg, principal, *, book_id) -> dict:
        return await self._sync(tools.ingest_status, cfg, book_id)

    def _register(self) -> None:
        srv = self.server

        @srv.tool()
        async def lore_search(query: str, world: str | None = None,
                              depth: Literal["auto", "deep", "focused"] = "auto",
                              ignore_reading_position: bool = False) -> dict[str, Any]:
            """Answer a question about the reader's books from LORE's knowledge
            graph, with quoted citations.

            This is the only tool that spends the reader's AI provider
            allowance (their own key, set in LORE's settings); the others are
            free lookups. Without a key it returns key_required. A question is
            at most 2,000 characters.

            `world` (a world's id or name) answers from that world's books
            only; without it every world is searched, each on its own.
            `depth` is "auto" (LORE judges), "deep" or "focused".

            The world's reading position limits the answer to what the reader
            has read and adds a `position_note`. Pass
            `ignore_reading_position=true` only when the reader has asked to
            see everything.
            """
            return await self._call("lore_search", self._lore_search, query=query,
                                    world=world, depth=depth,
                                    spoilers=ignore_reading_position)

        @srv.tool()
        async def vector_search(query: str, book_id: str | None = None, limit: int = 10,
                                world: str | None = None,
                                ignore_reading_position: bool = False) -> dict[str, Any]:
            """Find passages nearest a query by meaning, optionally within one
            book or one `world` (an id or name). Free: spends no AI provider
            allowance. `limit` is clamped to 1-20.

            Passages past the world's reading position are left out; pass
            `ignore_reading_position=true` only when the reader asked to see
            everything.
            """
            return await self._call("vector_search", self._vector_search, query=query,
                                    book_id=book_id, limit=limit, world=world,
                                    spoilers=ignore_reading_position)

        @srv.tool()
        async def entity(name: str, world: str | None = None,
                         ignore_reading_position: bool = False) -> dict[str, Any]:
            """The canonical card for one character, place or thing: aliases,
            books and relationships (at most 50 each way; `truncated` says when
            there are more -- use subgraph for those).

            An unknown name returns not_found; an ambiguous one lists the
            candidates. With `world` (an id or name) only that world is used.
            Under the world's reading position only what the reader has read
            is shown; `ignore_reading_position=true` shows everything.
            """
            return await self._call("entity", self._entity, name=name, world=world,
                                    spoilers=ignore_reading_position)

        @srv.tool()
        async def subgraph(entity: str, depth: int = 1, world: str | None = None,
                           ignore_reading_position: bool = False) -> dict[str, Any]:
            """A bounded subgraph around one entity, as nodes and links: each
            link is a subject->object pair with its predicates and one quoted
            citation, strongest first, at most 100. `depth` is clamped to 1-3;
            the result says when it was truncated.

            Under the world's reading position only relationships the reader
            has read are drawn; `ignore_reading_position=true` shows everything.
            """
            return await self._call("subgraph", self._subgraph, entity=entity, depth=depth,
                                    world=world, spoilers=ignore_reading_position)

        @srv.tool()
        async def library(world: str | None = None,
                          ignore_reading_position: bool = False) -> dict[str, Any]:
            """Every book in the reader's library, with its world, status and
            entity counts; `world` (an id or name) lists one world's books.
            Books the world's reading position has not reached are left out
            unless `ignore_reading_position=true`."""
            return await self._call("library", self._library, world=world,
                                    spoilers=ignore_reading_position)

        @srv.tool()
        async def ingest_status(book_id: str) -> dict[str, Any]:
            """Processing state for one of the reader's books: its jobs and any
            failures."""
            return await self._call("ingest_status", self._ingest_status, book_id=book_id)


class McpMount:
    """Pure ASGI: the hosted server's paths go to it (behind a request id and
    the body limit, like every web route), everything else to `app`. Lifespan
    and websocket scopes go to `app`, whose lifespan runs `hosted.running()`."""

    def __init__(self, hosted: HostedMcp, app) -> None:
        from lore.web.limits import BodyLimit
        from lore.web.request_id import RequestId
        self.hosted, self.app = hosted, app
        self.mcp = RequestId(BodyLimit(hosted))

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.hosted.handles(scope.get("path", "")):
            return await self.mcp(scope, receive, send)
        await self.app(scope, receive, send)
