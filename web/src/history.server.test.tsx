import { act, renderHook, waitFor } from "@testing-library/react";
import { HISTORY_KEY, IMPORTED_KEY, LEGACY_KEY, type HistoryItem } from "./history";
import type { AskEvent, ChatMessage, ChatSummary, SearchResult } from "./types";

const result = { answer: "Holmes met Watson.", mode: "graph", claims: [], passages: [], edges: [], seeds: [], sub_questions: ["who?"],
  answer_style: "claims" } as unknown as SearchResult;
const message = (id: number, question: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  id, role: "assistant", question, depth: "auto", world: "all", request_id: "req-1", created_at: "2026-11-04T10:00:00+00:00", ...extra });
const chat = (id: string, question: string): ChatSummary => ({ id, title: question, world: "all", client_id: null,
  created_at: "2026-11-04T10:00:00+00:00", updated_at: "2026-11-04T10:00:00+00:00",
  latest: message(1, question, { summary: { answer_style: "claims", mode: "graph", deep: false, truncated: false } }) });

let serverChats: ChatSummary[] = [];
const list = vi.fn(() => Promise.resolve({ chats: serverChats, next_offset: null }));
const get = vi.fn((id: string) => Promise.resolve({ chat: chat(id, "q"), messages: [
  message(7, "Who is Holmes?", { answer: { result, steps: [{ type: "parts", t: 0.2, sub_questions: ["who?"] } as AskEvent] } })] }));
const create = vi.fn((_b: object) => Promise.resolve({ chat: chat("new", "x"), created: true }));
const remove = vi.fn((_id: string) => Promise.resolve({ deleted: true }));
vi.mock("./chatsApi", async (importOriginal) => ({ ...(await importOriginal<typeof import("./chatsApi")>()),
  chatsApi: { list: () => list(), get: (id: string) => get(id), create: (b: object) => create(b), rename: vi.fn(), remove: (id: string) => remove(id) } }));

const streamAsk = vi.fn();
vi.mock("./api", () => ({ streamAsk: (...a: unknown[]) => streamAsk(...a) }));
Object.defineProperty(window, "matchMedia", { value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });

const local = (id: string): HistoryItem => ({ id, question: `local ${id}`, askedAt: "2026-10-01T00:00:00Z", world: "all",
  events: [{ type: "answer", t: 1, result } as AskEvent] });

beforeEach(() => {
  localStorage.clear(); serverChats = [];
  [list, get, create, remove, streamAsk].forEach((f) => f.mockClear());
});

test("the cache paints first, then the server's list replaces it", async () => {
  const cached = { ...local("c1"), synced: true };
  localStorage.setItem(HISTORY_KEY, JSON.stringify([cached]));
  serverChats = [chat("s1", "From the server")];
  let release!: () => void;
  list.mockImplementationOnce(() => new Promise((r) => { release = () => r({ chats: serverChats, next_offset: null }); }));
  const { useAsk } = await import("./useAsk");
  const { result: hook } = renderHook(() => useAsk());
  expect(hook.current.history.map((h) => h.id)).toEqual(["c1"]);  // before the server answers
  await act(async () => release());
  await waitFor(() => expect(hook.current.history.map((h) => h.id)).toEqual(["s1"]));
  expect(hook.current.history[0]).toMatchObject({ question: "From the server", synced: true, events: [] });
});

test("replaying a listed chat fetches it once, with no question asked", async () => {
  serverChats = [chat("s1", "Who is Holmes?")];
  const { useAsk } = await import("./useAsk");
  const { result: hook } = renderHook(() => useAsk());
  await waitFor(() => expect(hook.current.history).toHaveLength(1));
  act(() => hook.current.replay("s1"));
  await waitFor(() => expect(hook.current.state.status).toBe("done"));
  expect(get).toHaveBeenCalledWith("s1");
  expect(streamAsk).not.toHaveBeenCalled();
  expect(hook.current.state.result).toEqual(result);
  expect(hook.current.state.requestId).toBe("req-1");
  act(() => hook.current.replay("s1"));
  await waitFor(() => expect(hook.current.state.status).toBe("done"));
  expect(get).toHaveBeenCalledTimes(1);  // the events are cached now
});

test("an answer the server saved becomes that chat, and deleting it deletes the chat", async () => {
  streamAsk.mockImplementation(async function* () {
    yield { type: "request", t: 0, request_id: "r9" } as AskEvent;
    yield { type: "answer", t: 1, result, chat_id: "chat-9", message_id: 3 } as AskEvent;
  });
  const { useAsk } = await import("./useAsk");
  const { result: hook } = renderHook(() => useAsk());
  await waitFor(() => expect(list).toHaveBeenCalled());
  act(() => hook.current.ask("Who?"));
  await waitFor(() => expect(hook.current.history.map((h) => h.id)).toEqual(["chat-9"]));
  expect(hook.current.current).toBe("chat-9");
  expect(hook.current.history[0].synced).toBe(true);
  act(() => hook.current.remove("chat-9"));
  expect(remove).toHaveBeenCalledWith("chat-9");
  expect(hook.current.history).toEqual([]);
});

test("browser-only chats are offered for import once, and importing posts each by its own id", async () => {
  localStorage.setItem(HISTORY_KEY, JSON.stringify([local("1"), local("2")]));
  const { useAsk } = await import("./useAsk");
  const { result: hook, unmount } = renderHook(() => useAsk());
  await waitFor(() => expect(hook.current.importable).toBe(2));
  expect(JSON.parse(localStorage.getItem(LEGACY_KEY)!)).toHaveLength(2);  // kept aside: the cache now mirrors the server
  await act(async () => { await hook.current.importLocal(); });
  expect(create).toHaveBeenCalledTimes(2);
  expect(create.mock.calls.map((c) => (c[0] as { client_id: string }).client_id)).toEqual(["1", "2"]);
  expect((create.mock.calls[0][0] as { messages: { answer: { result: SearchResult } }[] }).messages[0].answer.result).toEqual(result);
  expect(localStorage.getItem(IMPORTED_KEY)).toBe("1");
  expect(hook.current.importable).toBe(0);
  unmount();
  localStorage.setItem(HISTORY_KEY, JSON.stringify([local("3")]));
  const again = renderHook(() => useAsk());
  await waitFor(() => expect(list).toHaveBeenCalledTimes(3));
  expect(again.result.current.importable).toBe(0);  // offered once only
});

test("declining the import hides it for good; a server that has chats never offers it", async () => {
  localStorage.setItem(HISTORY_KEY, JSON.stringify([local("1")]));
  const { useAsk } = await import("./useAsk");
  const first = renderHook(() => useAsk());
  await waitFor(() => expect(first.result.current.importable).toBe(1));
  act(() => first.result.current.declineImport());
  expect(first.result.current.importable).toBe(0);
  expect(create).not.toHaveBeenCalled();
  first.unmount();
  localStorage.clear();
  localStorage.setItem(HISTORY_KEY, JSON.stringify([local("1")]));
  serverChats = [chat("s1", "q")];
  const second = renderHook(() => useAsk());
  await waitFor(() => expect(second.result.current.history.map((h) => h.id)).toEqual(["s1"]));
  expect(second.result.current.importable).toBe(0);
});
