import type { AdminEvent, AdminReport, AdminUser, ApiError, TraceRecord, AskEvent, AuthProviders, KeySaved, KeyStatus, Me, ProcessEstimate, ProviderId, ProvidersState, UsageSummary, NewInvite, UsageToday, WaitlistEntry, Depth, Book, EdgeDetail, EntityCardData,
  EntityIntro, EntityKind, EntityProfile, LibraryStatus, McpInfo, McpCheck, DeviceSession, MoveResult, PassageText, PathResult, Quota, ReadingPositionBody, ReadingPositionData, Relations,
  SearchHit, SubgraphData, Timeline, World, WorldRef } from "./types";

export function parseSse(buffer: string): { events: AskEvent[]; rest: string } {
  const events: AskEvent[] = [];
  const normalized = buffer.replace(/\r\n?/g, "\n");
  const blocks = normalized.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let type = "message";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) type = line.slice(7);
      else if (line.startsWith("data: ")) data += line.slice(6);
    }
    if (data) events.push({ type, ...JSON.parse(data) } as AskEvent);
  }
  return { events, rest };
}

export { CSRF_COOKIE, csrfHeaders } from "./csrf";
import { csrfHeaders } from "./csrf";

const errorEvent = (code: string, message: string): AskEvent => ({ type: "error", t: 0, error: { code, message } });

export { onKeyProblem, reportKeyProblem, type KeyProblem } from "./keyProblem";
import { reportKeyProblem } from "./keyProblem";

// `world`: a world id as a string, or "all" (the same value the graph calls take).
// `depth`: auto, deep or focused; left out of the body when not given.
// `chatId`: the chat this question follows up; the server loads that chat's earlier turns itself (never sent from here).
export async function* streamAsk(question: string, signal: AbortSignal, world?: string, depth?: Depth, chatId?: string): AsyncGenerator<AskEvent> {
  let response: Response;
  try {
    response = await fetch("/api/ask", {
      method: "POST", signal,
      headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...csrfHeaders() },
      body: JSON.stringify({ question, ...(world ? { world } : {}), ...(depth ? { depth } : {}), ...(chatId ? { chat_id: chatId } : {}) }),
    });
  } catch {
    if (signal.aborted) return;
    yield errorEvent("unreachable", "");
    return;
  }
  if (!response.ok || !response.body) {
    if (signedOut(response)) { yield errorEvent("unauthorized", ""); return; }
    let message = "";
    let code = response.status === 403 ? "forbidden" : "bad_request";
    let requestId: string | undefined;
    try {
      const body = await response.json();
      message = body.detail ?? "";
      requestId = body.error?.request_id;
      if (response.status === 404 && body.error?.code === "not_found") code = "chat_not_found";  // the chat followed up is gone
      // A limit (429: rate_limited, or busy while another question runs) says so in its own words.
      else if (response.status === 429 && body.error?.code) { code = body.error.code; message = body.error.message ?? ""; }
    } catch { /* not JSON */ }
    const event = errorEvent(code, String(message));
    if (requestId && event.type === "error") event.error.request_id = requestId;
    yield event;
    return;
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let finished = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      const parsed = parseSse(buffer + value);
      buffer = parsed.rest;
      for (const e of parsed.events) {
        if (e.type === "answer" || e.type === "error") finished = true;
        if (e.type === "error") reportKeyProblem(e.error.code);
        yield e;
      }
    }
  } catch {
    if (signal.aborted) return;
  }
  if (!finished && !signal.aborted) yield errorEvent("interrupted", "");
}

// Where the browser goes when the server says the session is over (multi-user mode only). A seam for tests.
export const nav = { to: (path: string) => window.location.assign(path) };
const multiUser = () => document.querySelector('meta[name="lore-multi"]')?.getAttribute("content") === "1";
// A 401 means the session ended: go and sign in again. Local mode never sees one.
function signedOut(r: Response): boolean {
  if (r.status !== 401 || !multiUser()) return false;
  nav.to("/signin");
  return true;
}

// `strict` (the sign-in and admin calls): any non-2xx answer without an {error} body becomes {error: {code: "http_<status>"}},
// so a bare 403 {"detail": ...} from the server is an error here, never mistaken for data.
async function readJson<T>(r: Response, strict: boolean): Promise<T | { error: ApiError }> {
  if (signedOut(r)) return { error: { code: "unauthorized", message: "" } };
  let body: unknown;
  try { body = await r.json(); } catch { if (strict && !r.ok) return { error: { code: `http_${r.status}`, message: "" } }; throw new Error("not JSON"); }
  if (strict && !r.ok && !isError(body)) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    return { error: { code: `http_${r.status}`, message: typeof detail === "string" ? detail : "" } };
  }
  return body as T | { error: ApiError };
}

export async function getJson<T>(path: string, strict = false): Promise<T | { error: ApiError }> {
  try {
    return await readJson<T>(await fetch(path, { headers: { Accept: "application/json" } }), strict);
  } catch {
    return { error: { code: "unreachable", message: "" } };
  }
}

export const isError = (x: unknown): x is { error: ApiError } =>
  typeof x === "object" && x !== null && "error" in x;

// Starts or resumes a book's processing. 202 gives {started: true}; 409 gives {error: {code, message}}.
export async function postContinue(id: string): Promise<{ started: true } | { error: ApiError }> {
  try {
    const r = await fetch(`/api/books/${encodeURIComponent(id)}/continue`, { method: "POST", headers: { Accept: "application/json", ...csrfHeaders() } });
    if (r.status === 404) return { error: { code: "not_found", message: "" } };
    if (signedOut(r)) return { error: { code: "unauthorized", message: "" } };
    const body = (await r.json()) as { started: true } | { error: ApiError };
    if (isError(body)) reportKeyProblem(body.error.code);
    return body;
  } catch {
    return { error: { code: "unreachable", message: "" } };
  }
}

const withWorld = (path: string, world?: string) =>
  world ? `${path}${path.includes("?") ? "&" : "?"}world=${encodeURIComponent(world)}` : path;

async function sendJson<T>(method: string, path: string, body?: object, strict = false): Promise<T | { error: ApiError }> {
  try {
    const r = await fetch(path, { method, headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json" } : {}), ...(method === "GET" ? {} : csrfHeaders()) },
      body: body ? JSON.stringify(body) : undefined });
    const out = await readJson<T>(r, strict);
    // A move answers key_required too; the key routes themselves do not route (their screen is already open).
    if (isError(out) && !path.startsWith("/api/me/key")) reportKeyProblem(out.error.code);
    return out;
  } catch {
    return { error: { code: "unreachable", message: "" } };
  }
}

export const api = {
  books: () => getJson<LibraryStatus>("/api/books"),
  continueBook: postContinue,
  library: (world?: string) => getJson<{ books: Book[]; note: string | null }>(withWorld("/api/library", world)),
  entity: (id: string, world?: string) => getJson<EntityCardData>(withWorld(`/api/entity?id=${encodeURIComponent(id)}`, world)),
  entities: (ids: string[], world?: string) => getJson<{ entities: EntityKind[] }>(withWorld(`/api/entities?ids=${encodeURIComponent(ids.join(","))}`, world)),
  subgraph: (id: string, world?: string) => getJson<SubgraphData>(withWorld(`/api/subgraph?id=${encodeURIComponent(id)}&depth=1`, world)),
  overview: (world?: string) => getJson<SubgraphData>(withWorld("/api/overview?limit=40", world)),
  worlds: () => getJson<World[]>("/api/worlds"),
  // The graph explorer's reads: stored data only, no model calls.
  profile: (id: string, world?: string) => getJson<EntityProfile>(withWorld(`/api/entity/${encodeURIComponent(id)}/profile`, world)),
  // The one explorer read that may spend a request: the spoiler-safe intro under a reading position (cached).
  intro: (id: string, world?: string) => getJson<EntityIntro>(withWorld(`/api/entity/${encodeURIComponent(id)}/intro`, world)),
  timeline: (id: string, world?: string) => getJson<Timeline>(withWorld(`/api/entity/${encodeURIComponent(id)}/timeline`, world)),
  relations: (id: string, world?: string) => getJson<Relations>(withWorld(`/api/entity/${encodeURIComponent(id)}/relations`, world)),
  edge: (a: string, b: string, world?: string) =>
    getJson<EdgeDetail>(withWorld(`/api/edge?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`, world)),
  search: (q: string, world?: string, limit = 8) =>
    getJson<{ results: SearchHit[] }>(withWorld(`/api/search?q=${encodeURIComponent(q)}&limit=${limit}`, world)),
  path: (a: string, b: string, world?: string) =>
    getJson<PathResult>(withWorld(`/api/path?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}&max=4`, world)),
  passage: (chunkId: string, world?: string) => getJson<PassageText>(withWorld(`/api/passage/${encodeURIComponent(chunkId)}`, world)),
  // A world given by name with `create` is made first; 202 starts the move job, 200 {started:false} means already there.
  moveBook: (id: string, world: string | number, create = false) =>
    sendJson<MoveResult>("POST", `/api/books/${encodeURIComponent(id)}/move`, { world, create }),
  createWorld: (name: string) => sendJson<WorldRef>("POST", "/api/worlds", { name }),
  renameWorld: (id: number, name: string) => sendJson<WorldRef>("PATCH", `/api/worlds/${id}`, { name }),
  deleteWorld: (id: number) => sendJson<{ deleted: true }>("DELETE", `/api/worlds/${id}`),
  // The world's reading position (stored on the server); every read above and every question applies it by default.
  readingPosition: (world: string) => getJson<ReadingPositionData>(withWorld("/api/reading-position", world)),
  setReadingPosition: (world: string, body: ReadingPositionBody) =>
    sendJson<ReadingPositionData>("PUT", withWorld("/api/reading-position", world), body),
  quota: () => getJson<Quota>("/api/quota"),
  mcp: () => getJson<McpInfo>("/api/mcp"),
  // Local mode: builds the MCP server as `lore mcp` would and lists its tools (404 in multi mode).
  mcpCheck: () => getJson<McpCheck>("/api/mcp/check"),
  settings: () => getJson<{ show_internals: boolean }>("/api/settings"),
};

// Sign-in, the waitlist and the admin page (multi-user mode). The server answers a failed call with
// {error: {code, message}}; `isError` tells them apart.
export const authApi = {
  providers: () => getJson<AuthProviders>("/auth/providers", true),
  // Always 202 {sent: true} for a well-formed address, whether or not it has an account.
  requestLink: (email: string, invite?: string) =>
    sendJson<{ sent: true }>("POST", "/auth/email", { email, ...(invite ? { invite } : {}) }, true),
  logout: () => sendJson<{ ok: true }>("POST", "/auth/logout", {}, true),
  me: () => getJson<Me>("/api/me", true),
  // Always 202. `website` is the honeypot: the form leaves it empty.
  joinWaitlist: (email: string, reading: string, consent: boolean, website = "") =>
    sendJson<{ ok: true }>("POST", "/api/waitlist", { email, reading, consent, website }, true),
  // Deletes the account and everything in it; `confirm` is the user's own email, typed.
  deleteMe: (confirm: string) => sendJson<{ deleted: true }>("POST", "/api/me/delete", { confirm }, true),
  // Signed-in devices: this user's live sessions; revoke one (never this device: that is Sign out), or all the others.
  sessions: () => getJson<{ sessions: DeviceSession[] }>("/api/me/sessions", true),
  revokeSession: (id: string) => sendJson<{ revoked: number }>("DELETE", `/api/me/sessions/${encodeURIComponent(id)}`, undefined, true),
  signOutOthers: () => sendJson<{ revoked: number }>("POST", "/api/me/sessions/sign-out-others", {}, true),
  // Download my data: a zip of this user's chats and their books' metadata (never keys).
  exportData: exportData,
};

async function exportData(): Promise<{ blob: Blob; filename: string } | { error: ApiError }> {
  try {
    const r = await fetch("/api/me/export", { method: "POST", headers: { Accept: "application/zip", ...csrfHeaders() } });
    if (!r.ok) {
      if (signedOut(r)) return { error: { code: "unauthorized", message: "" } };
      return { error: { code: `http_${r.status}`, message: "" } };
    }
    const named = /filename="([^"]+)"/.exec(r.headers.get("content-disposition") ?? "");
    return { blob: await r.blob(), filename: named?.[1] ?? "lore-data.zip" };
  } catch {
    return { error: { code: "unreachable", message: "" } };
  }
}

// The signed-in user's own model-provider keys (multi-user mode): Gemini, Claude (anthropic) or OpenAI. `get` is the
// active provider's key. The server never sends a key back, only its last four characters.
export const keyApi = {
  get: () => getJson<KeyStatus>("/api/me/key", true),
  save: (key: string, provider?: ProviderId) =>
    sendJson<KeySaved>("PUT", "/api/me/key", provider ? { provider, key } : { key }, true),
  remove: (provider?: ProviderId) =>
    sendJson<{ deleted: boolean }>("DELETE", provider ? `/api/me/key?provider=${provider}` : "/api/me/key", undefined, true),
  usage: () => getJson<UsageToday>("/api/me/usage", true),
  providers: () => getJson<ProvidersState>("/api/me/providers", true),
  // Switch the active provider, and/or its model (and an optional cheaper model for the short steps).
  choose: (provider: ProviderId, model?: string | null, fastModel?: string | null) =>
    sendJson<ProvidersState>("PUT", "/api/me/provider",
      { provider, ...(model ? { model } : {}), ...(fastModel !== undefined ? { fast_model: fastModel ?? "" } : {}) }, true),
  // "I'm on a paid Gemini plan": on the user's Gemini key; never switches the active provider.
  setGeminiPaid: (paid: boolean) => sendJson<ProvidersState>("PUT", "/api/me/provider", { gemini_paid: paid }, true),
  setTokenCap: (cap: number | null) => sendJson<UsageSummary>("PUT", "/api/me/token-cap", { cap }, true),
  estimate: (bookId: string) => getJson<ProcessEstimate>(`/api/books/${encodeURIComponent(bookId)}/estimate`, true),
};

export const adminApi = {
  waitlist: () => getJson<{ entries: WaitlistEntry[] }>("/api/admin/waitlist", true),
  approve: (id: number) => sendJson<{ invited: true; entry: WaitlistEntry }>("POST", `/api/admin/waitlist/${id}/approve`, {}, true),
  decline: (id: number) => sendJson<{ declined: true; entry: WaitlistEntry }>("POST", `/api/admin/waitlist/${id}/decline`, {}, true),
  createInvite: (days: number) => sendJson<NewInvite>("POST", "/api/admin/invites", { days }, true),
  users: () => getJson<{ users: AdminUser[] }>("/api/admin/users", true),
  reports: () => getJson<{ reports: AdminReport[] }>("/api/admin/reports", true),
  resolveReport: (id: number) => sendJson<{ report: AdminReport }>("POST", `/api/admin/reports/${id}/resolve`, {}, true),
  trace: (requestId: string) => getJson<TraceRecord>(`/api/admin/traces/${encodeURIComponent(requestId)}`, true),
  disable: (id: string) => sendJson<{ user: { status: string } }>("POST", `/api/admin/users/${encodeURIComponent(id)}/disable`, {}, true),
  enable: (id: string) => sendJson<{ user: { status: string } }>("POST", `/api/admin/users/${encodeURIComponent(id)}/enable`, {}, true),
  revokeSessions: (id: string) => sendJson<{ revoked: number }>("POST", `/api/admin/users/${encodeURIComponent(id)}/revoke-sessions`, {}, true),
  deleteUser: (id: string) => sendJson<{ deleted: true }>("DELETE", `/api/admin/users/${encodeURIComponent(id)}`, undefined, true),
  events: () => getJson<{ events: AdminEvent[] }>("/api/admin/events", true),
};
