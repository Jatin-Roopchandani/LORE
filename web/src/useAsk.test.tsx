import { act, renderHook, waitFor } from "@testing-library/react";
import single from "./fixtures/single-part.json";
import type { AskEvent } from "./types";

interface Stream { signal: AbortSignal; push(e: AskEvent): void; end(): void }
const streams: Stream[] = [];
const streamAsk = vi.fn();

vi.mock("./api", () => ({ streamAsk: (q: string, signal: AbortSignal) => streamAsk(q, signal) }));
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
  expect(result.current.state.result).toEqual(result.current.history[0].answer);
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
