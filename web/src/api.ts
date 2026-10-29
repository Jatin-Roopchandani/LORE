import type { ApiError, AskEvent, Book, EntityCardData, EntityKind, LibraryStatus, McpInfo, MoveResult, Quota, SubgraphData, World, WorldRef } from "./types";

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

const errorEvent = (code: string, message: string): AskEvent => ({ type: "error", t: 0, error: { code, message } });

// `world`: a world id as a string, or "all" (the same value the graph calls take).
export async function* streamAsk(question: string, signal: AbortSignal, world?: string): AsyncGenerator<AskEvent> {
  let response: Response;
  try {
    response = await fetch("/api/ask", {
      method: "POST", signal,
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(world ? { question, world } : { question }),
    });
  } catch {
    if (signal.aborted) return;
    yield errorEvent("unreachable", "");
    return;
  }
  if (!response.ok || !response.body) {
    let message = "";
    try { message = (await response.json()).detail ?? ""; } catch { /* not JSON */ }
    yield errorEvent(response.status === 403 ? "forbidden" : "bad_request", String(message));
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
        yield e;
      }
    }
  } catch {
    if (signal.aborted) return;
  }
  if (!finished && !signal.aborted) yield errorEvent("interrupted", "");
}

export async function getJson<T>(path: string): Promise<T | { error: ApiError }> {
  try {
    const r = await fetch(path, { headers: { Accept: "application/json" } });
    return (await r.json()) as T | { error: ApiError };
  } catch {
    return { error: { code: "unreachable", message: "" } };
  }
}

export const isError = (x: unknown): x is { error: ApiError } =>
  typeof x === "object" && x !== null && "error" in x;

// Starts or resumes a book's processing. 202 gives {started: true}; 409 gives {error: {code, message}}.
export async function postContinue(id: string): Promise<{ started: true } | { error: ApiError }> {
  try {
    const r = await fetch(`/api/books/${encodeURIComponent(id)}/continue`, { method: "POST", headers: { Accept: "application/json" } });
    if (r.status === 404) return { error: { code: "not_found", message: "" } };
    return (await r.json()) as { started: true } | { error: ApiError };
  } catch {
    return { error: { code: "unreachable", message: "" } };
  }
}

const withWorld = (path: string, world?: string) =>
  world ? `${path}${path.includes("?") ? "&" : "?"}world=${encodeURIComponent(world)}` : path;

async function sendJson<T>(method: string, path: string, body?: object): Promise<T | { error: ApiError }> {
  try {
    const r = await fetch(path, { method, headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined });
    return (await r.json()) as T | { error: ApiError };
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
  // A world given by name with `create` is made first; 202 starts the move job, 200 {started:false} means already there.
  moveBook: (id: string, world: string | number, create = false) =>
    sendJson<MoveResult>("POST", `/api/books/${encodeURIComponent(id)}/move`, { world, create }),
  createWorld: (name: string) => sendJson<WorldRef>("POST", "/api/worlds", { name }),
  renameWorld: (id: number, name: string) => sendJson<WorldRef>("PATCH", `/api/worlds/${id}`, { name }),
  deleteWorld: (id: number) => sendJson<{ deleted: true }>("DELETE", `/api/worlds/${id}`),
  quota: () => getJson<Quota>("/api/quota"),
  mcp: () => getJson<McpInfo>("/api/mcp"),
  settings: () => getJson<{ show_internals: boolean }>("/api/settings"),
};
