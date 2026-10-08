import type { ApiError, AskEvent, Book, EntityCardData, EntityKind, McpInfo, Quota, SubgraphData } from "./types";

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

export async function* streamAsk(question: string, signal: AbortSignal): AsyncGenerator<AskEvent> {
  let response: Response;
  try {
    response = await fetch("/api/ask", {
      method: "POST", signal,
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ question }),
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

export const api = {
  library: () => getJson<{ books: Book[]; note: string | null }>("/api/library"),
  entity: (id: string) => getJson<EntityCardData>(`/api/entity?id=${encodeURIComponent(id)}`),
  entities: (ids: string[]) => getJson<{ entities: EntityKind[] }>(`/api/entities?ids=${encodeURIComponent(ids.join(","))}`),
  subgraph: (id: string) => getJson<SubgraphData>(`/api/subgraph?id=${encodeURIComponent(id)}&depth=1`),
  overview: () => getJson<SubgraphData>("/api/overview?limit=40"),
  quota: () => getJson<Quota>("/api/quota"),
  mcp: () => getJson<McpInfo>("/api/mcp"),
  settings: () => getJson<{ show_internals: boolean }>("/api/settings"),
};
