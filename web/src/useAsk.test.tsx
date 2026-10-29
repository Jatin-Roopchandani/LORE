import { act, renderHook, waitFor } from "@testing-library/react";
import { answerOf, HISTORY_KEY } from "./history";
import single from "./fixtures/single-part.json";
import type { AskEvent } from "./types";

interface Stream { signal: AbortSignal; push(e: AskEvent): void; end(): void }
const streams: Stream[] = [];
const streamAsk = vi.fn();

vi.mock("./api", () => ({ streamAsk: (q: string, signal: AbortSignal, world?: string) => streamAsk(q, signal, world) }));
Object.defineProperty(window, "matchMedia", { value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });

function controllable(signal: AbortSignal) {
  const queue: AskEvent[] = [];
  let wake: (() => void) | null = null;
  let ended = false;
  streams.push({ signal, push: (e) => { queue.push(e); wake?.(); }, end: () => { ended = true; wake?.(); } });
  return (async function* () {
    while (true) {
      if (queue.length) { yield queue.shift()!; continue; }
      if (ended || signal.aborted) return;
      await new Promise<void>((r) => { wake = r; });
    }
  })();
}

beforeEach(() => {
  streams.length = 0;
  localStorage.clear();
  streamAsk.mockReset();
  streamAsk.mockImplementation(async function* () { for (const e of single as AskEvent[]) yield e; });
});

test("ask streams to done and lands in history; replay redraws without a request", async () => {
  const { useAsk } = await import("./useAsk");
  const { result } = renderHook(() => useAsk());
  act(() => result.current.ask("How is Jefferson Hope connected to John Ferrier?"));
  await waitFor(() => expect(result.current.state.status).toBe("done"));
  expect(result.current.history).toHaveLength(1);
  expect(streamAsk).toHaveBeenCalledTimes(1);
  const id = result.current.history[0].id;
  act(() => result.current.replay(id));
  await waitFor(() => expect(result.current.state.status).toBe("done"));
  expect(result.current.current).toBe(id);
  expect(streamAsk).toHaveBeenCalledTimes(1);
  expect(result.current.state.result).toEqual(answerOf(result.current.history[0]));
  expect(result.current.history[0]).not.toHaveProperty("answer");
  expect(JSON.parse(localStorage.getItem(HISTORY_KEY)!)[0].events.length).toBeGreaterThan(0);
});

test("a second ask aborts the first stream and its later events are dropped", async () => {
  const { useAsk } = await import("./useAsk");
  streamAsk.mockImplementation((_q: string, signal: AbortSignal) => controllable(signal));
  const { result } = renderHook(() => useAsk());
  act(() => result.current.ask("first"));
  act(() => result.current.ask("second"));
  expect(streams[0].signal.aborted).toBe(true);
  expect(streams[1].signal.aborted).toBe(false);
  const answer = (single as AskEvent[]).find((e) => e.type === "answer")!;
  act(() => streams[0].push(answer));
  await new Promise((r) => setTimeout(r, 20));
  expect(result.current.state.status).not.toBe("done");
  expect(result.current.state.question).toBe("second");
  expect(result.current.history).toHaveLength(0);
});

test("a stream ending in an error adds nothing to history", async () => {
  const { useAsk } = await import("./useAsk");
  streamAsk.mockImplementation(async function* () {
    yield { type: "error", t: 0, error: { code: "upstream_unavailable", message: "x" } } as AskEvent;
  });
  const { result } = renderHook(() => useAsk());
  act(() => result.current.ask("q"));
  await waitFor(() => expect(result.current.state.status).toBe("error"));
  await new Promise((r) => setTimeout(r, 20));
  expect(result.current.history).toHaveLength(0);
});

test("unmount aborts the active stream", async () => {
  const { useAsk } = await import("./useAsk");
  streamAsk.mockImplementation((_q: string, signal: AbortSignal) => controllable(signal));
  const { result, unmount } = renderHook(() => useAsk());
  act(() => result.current.ask("q"));
  expect(streams[0].signal.aborted).toBe(false);
  unmount();
  expect(streams[0].signal.aborted).toBe(true);
});

test("ask sends the selected world and records it in the history item", async () => {
  const { useAsk } = await import("./useAsk");
  const { result, rerender } = renderHook(({ w }) => useAsk(w), { initialProps: { w: "2" } });
  act(() => result.current.ask("q1"));
  await waitFor(() => expect(result.current.state.status).toBe("done"));
  expect(streamAsk.mock.calls[0][2]).toBe("2");
  expect(result.current.history[0].world).toBe("2");
  rerender({ w: "all" });
  act(() => result.current.ask("q2"));
  await waitFor(() => expect(result.current.history).toHaveLength(2));
  expect(streamAsk.mock.calls[1][2]).toBe("all");
  expect(result.current.history[0].world).toBe("all");
});

test("replaying an item from another world switches to it; the same world does not", async () => {
  const { useAsk } = await import("./useAsk");
  const switched = vi.fn();
  const item = (id: string, world: string) => ({ id, question: id, events: single, askedAt: "2026-10-01T00:00:00Z", world });
  localStorage.setItem("lore-history-v1", JSON.stringify([item("here", "2"), item("there", "1")]));
  const { result } = renderHook(() => useAsk("2", switched));
  act(() => result.current.replay("here"));
  expect(switched).not.toHaveBeenCalled();
  act(() => result.current.replay("there"));
  expect(switched).toHaveBeenCalledWith("1");
  await waitFor(() => expect(result.current.state.status).toBe("done"));
  expect(streamAsk).not.toHaveBeenCalled();
});

test("an item saved before worlds replays without switching", async () => {
  const { useAsk } = await import("./useAsk");
  const switched = vi.fn();
  const item = { id: "old", question: "q", events: single, askedAt: "2026-10-01T00:00:00Z" };
  localStorage.setItem("lore-history-v1", JSON.stringify([item]));
  const { result } = renderHook(() => useAsk("3", switched));
  act(() => result.current.replay("old"));
  await waitFor(() => expect(result.current.state.status).toBe("done"));
  expect(switched).not.toHaveBeenCalled();
});

test("clear returns to the empty state and aborts a running stream", async () => {
  const { useAsk } = await import("./useAsk");
  streamAsk.mockImplementation((_q: string, signal: AbortSignal) => controllable(signal));
  const { result } = renderHook(() => useAsk());
  act(() => result.current.ask("q"));
  const run = result.current.run;
  act(() => result.current.clear());
  expect(streams[0].signal.aborted).toBe(true);
  expect(result.current.state.status).toBe("idle");
  expect(result.current.state.question).toBe("");
  expect(result.current.run).toBe(run + 1);
});
