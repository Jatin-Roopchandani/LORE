// Follow-ups: a chat is a thread. The hook sends follow-ups with the chat's id (never any history text), New chat starts
// afresh, and a chat opened from history comes back whole.
import { act, renderHook, waitFor } from "@testing-library/react";
import type { AskEvent, ChatMessage, ChatSummary, SearchResult } from "./types";

const result = (answer: string) => ({ answer, mode: "graph", template: null, note: null, model: null, selection_model: null,
  claims: [], passages: [], edges: [], seeds: [], sub_questions: ["q"], answer_style: "claims" }) as unknown as SearchResult;
const message = (id: number, question: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  id, role: "assistant", question, depth: "auto", world: "all", request_id: `req-${id}`, created_at: "2026-11-04T10:00:00+00:00",
  answer: { result: result(`answer ${id}`), steps: [] }, ...extra });
const chat = (id: string, title: string): ChatSummary => ({ id, title, world: "all", client_id: null,
  created_at: "2026-11-04T10:00:00+00:00", updated_at: "2026-11-04T10:00:00+00:00",
  latest: message(3, "And after that?", { summary: { answer_style: "claims", mode: "graph", deep: false, truncated: false } }) });

let serverChats: ChatSummary[] = [];
const get = vi.fn((id: string) => Promise.resolve({ chat: chat(id, "Who is Ren?"), messages: [
  message(1, "Who is Ren?"),
  message(2, "What did he do next?", { standalone: "What did Ren do after the siege?" }),
  message(3, "And after that?", { answer: { result: result("answer 3"), steps: [{ type: "understood", t: 0.1, standalone: "What did Ren do after leaving?" } as AskEvent] } }),
] }));
vi.mock("./chatsApi", async (importOriginal) => ({ ...(await importOriginal<typeof import("./chatsApi")>()),
  chatsApi: { list: () => Promise.resolve({ chats: serverChats, next_offset: null }), get: (id: string) => get(id), create: vi.fn(),
    rename: vi.fn(), remove: () => Promise.resolve({ deleted: true }) } }));
const streamAsk = vi.fn();
vi.mock("./api", () => ({ streamAsk: (...a: unknown[]) => streamAsk(...a) }));
Object.defineProperty(window, "matchMedia", { value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });

const answering = (chatId: string, standalone?: string) => async function* () {
  yield { type: "request", t: 0, request_id: "r" } as AskEvent;
  if (standalone) yield { type: "understood", t: 0.1, standalone } as AskEvent;
  yield { type: "answer", t: 1, result: result("ok"), chat_id: chatId, message_id: 1 } as AskEvent;
};

beforeEach(() => { localStorage.clear(); serverChats = []; streamAsk.mockReset(); get.mockClear(); });

test("a follow-up is sent with the chat's id and nothing else; the first answer moves up into the thread", async () => {
  const { useAsk } = await import("./useAsk");
  const { result: hook } = renderHook(() => useAsk());
  streamAsk.mockImplementation(answering("c1"));
  act(() => hook.current.ask("Who is Ren?"));
  await waitFor(() => expect(hook.current.current).toBe("c1"));
  expect(streamAsk.mock.calls[0]).toHaveLength(4);  // a first question carries no chat id
  streamAsk.mockImplementation(answering("c1", "What did Ren do next?"));
  act(() => hook.current.ask("What did he do next?"));
  await waitFor(() => expect(hook.current.state.status).toBe("done"));
  const call = streamAsk.mock.calls[1];
  expect(call[0]).toBe("What did he do next?");
  expect(call[4]).toBe("c1");
  expect(call).toHaveLength(5);
  expect(hook.current.thread.map((e) => e.question)).toEqual(["Who is Ren?"]);
  expect(hook.current.state.standalone).toBe("What did Ren do next?");
  // One chat in the drawer, under its first question.
  expect(hook.current.history.map((h) => [h.id, h.question])).toEqual([["c1", "Who is Ren?"]]);
});

test("New chat empties the thread and the next question starts a new chat", async () => {
  const { useAsk } = await import("./useAsk");
  const { result: hook } = renderHook(() => useAsk());
  streamAsk.mockImplementation(answering("c1"));
  act(() => hook.current.ask("first"));
  await waitFor(() => expect(hook.current.current).toBe("c1"));
  act(() => hook.current.ask("second"));
  await waitFor(() => expect(hook.current.thread).toHaveLength(1));
  act(() => hook.current.clear());
  expect(hook.current.thread).toEqual([]);
  expect(hook.current.state.question).toBe("");
  streamAsk.mockImplementation(answering("c2"));
  act(() => hook.current.ask("fresh"));
  await waitFor(() => expect(hook.current.current).toBe("c2"));
  expect(streamAsk.mock.calls.at(-1)).toHaveLength(4);
  expect(hook.current.thread).toEqual([]);
});

test("Retry asks again in the same chat without stacking the failed answer", async () => {
  const { useAsk } = await import("./useAsk");
  const { result: hook } = renderHook(() => useAsk());
  streamAsk.mockImplementation(answering("c1"));
  act(() => hook.current.ask("first"));
  await waitFor(() => expect(hook.current.current).toBe("c1"));
  streamAsk.mockImplementation(async function* () { yield { type: "error", t: 0, error: { code: "upstream_unavailable", message: "" } } as AskEvent; });
  act(() => hook.current.ask("second"));
  await waitFor(() => expect(hook.current.state.status).toBe("error"));
  streamAsk.mockImplementation(answering("c1"));
  act(() => hook.current.retry());
  await waitFor(() => expect(hook.current.state.status).toBe("done"));
  expect(streamAsk.mock.calls.at(-1)?.[4]).toBe("c1");
  expect(hook.current.thread.map((e) => e.question)).toEqual(["first"]);
});

test("opening a chat from history draws the whole thread, with each rewrite, once", async () => {
  serverChats = [chat("c9", "Who is Ren?")];
  const { useAsk } = await import("./useAsk");
  const { result: hook } = renderHook(() => useAsk());
  await waitFor(() => expect(hook.current.history).toHaveLength(1));
  expect(hook.current.history[0].question).toBe("Who is Ren?");  // a chat's title is its first question
  act(() => hook.current.replay("c9"));
  await waitFor(() => expect(hook.current.state.status).toBe("done"));
  expect(hook.current.thread.map((e) => [e.question, e.standalone])).toEqual([
    ["Who is Ren?", null], ["What did he do next?", "What did Ren do after the siege?"]]);
  expect(hook.current.state.question).toBe("And after that?");
  expect(hook.current.state.standalone).toBe("What did Ren do after leaving?");
  expect(streamAsk).not.toHaveBeenCalled();
  act(() => hook.current.replay("c9"));
  await waitFor(() => expect(hook.current.thread).toHaveLength(2));
  expect(get).toHaveBeenCalledTimes(1);
  // Asking now follows that chat up.
  streamAsk.mockImplementation(answering("c9"));
  act(() => hook.current.ask("Where is he now?"));
  await waitFor(() => expect(hook.current.state.status).toBe("done"));
  expect(streamAsk.mock.calls[0][4]).toBe("c9");
  expect(hook.current.thread.map((e) => e.question)).toEqual(["Who is Ren?", "What did he do next?", "And after that?"]);
});
