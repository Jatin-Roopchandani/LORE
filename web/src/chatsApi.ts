// The history API (release step 4): the signed-in user's chats on the server; in local mode, the local user's.
// Its own module, not part of api.ts, so the history keeps working wherever api.ts is mocked.
import { csrfHeaders } from "./csrf";
import type { ApiError, ChatDetail, ChatList, ChatSummary, NewChatBody } from "./types";

export type Result<T> = T | { error: ApiError };
export const failed = <T,>(r: Result<T>): r is { error: ApiError } => typeof r === "object" && r !== null && "error" in r;

async function call<T>(method: string, path: string, body?: object): Promise<Result<T>> {
  try {
    const r = await fetch(path, {
      method,
      headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json" } : {}), ...(method === "GET" ? {} : csrfHeaders()) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) return { error: (data && data.error) || { code: r.status === 404 ? "not_found" : "bad_request", message: "" } };
    return data as T;
  } catch {
    return { error: { code: "unreachable", message: "" } };
  }
}

export const chatsApi = {
  list: (limit = 50, offset = 0) => call<ChatList>("GET", `/api/chats?limit=${limit}&offset=${offset}`),
  get: (id: string) => call<ChatDetail>("GET", `/api/chats/${encodeURIComponent(id)}`),
  create: (body: NewChatBody) => call<{ chat: ChatSummary; created: boolean }>("POST", "/api/chats", body),
  rename: (id: string, title: string) => call<{ chat: ChatSummary }>("PATCH", `/api/chats/${encodeURIComponent(id)}`, { title }),
  remove: (id: string) => call<{ deleted: true }>("DELETE", `/api/chats/${encodeURIComponent(id)}`),
  // "Report this answer": the question's request id and an optional note (only the user's own questions).
  report: (request_id: string, note: string) => call<{ report: { id: number; status: string } }>("POST", "/api/reports",
    { request_id, ...(note.trim() ? { note: note.trim() } : {}) }),
};
