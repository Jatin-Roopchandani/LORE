import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { askReducer, initialState } from "./askReducer";
import { head, sec0, start } from "./fixtures/deep";
import { DEEP, UI } from "./messages";
import type { AskEvent } from "./types";
import { AskBox } from "./components/AskBox";

// Stop: the Ask button becomes Stop while an answer streams (Esc too); stopping aborts the stream, asks the server to
// cancel the run, keeps what had arrived (marked stopped) and gives the question back to the box.

interface Stream { signal: AbortSignal; push(e: AskEvent): void }
const streams: Stream[] = [];
const streamAsk = vi.fn();
const cancelAsk = vi.fn();

vi.mock("./chatsApi", async (importOriginal) => ({ ...(await importOriginal<typeof import("./chatsApi")>()),
  chatsApi: { list: () => Promise.resolve({ error: { code: "unreachable", message: "" } }), get: vi.fn(), create: vi.fn(), rename: vi.fn(),
    remove: () => Promise.resolve({ deleted: true }) } }));
vi.mock("./api", () => ({
  streamAsk: (q: string, signal: AbortSignal, world?: string, depth?: string, chat?: string) => streamAsk(q, signal, world, depth, chat),
  cancelAsk: (id: string) => cancelAsk(id),
}));
Object.defineProperty(window, "matchMedia", { value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });

function controllable(signal: AbortSignal) {
  const queue: AskEvent[] = [];
  let wake: (() => void) | null = null;
  streams.push({ signal, push: (e) => { queue.push(e); wake?.(); } });
  signal.addEventListener("abort", () => wake?.());
  return (async function* () {
    while (true) {
      if (signal.aborted) return;
      if (queue.length) { yield queue.shift()!; continue; }
      await new Promise<void>((r) => { wake = r; });
    }
  })();
}

beforeEach(() => {
  streams.length = 0;
  localStorage.clear();
  streamAsk.mockReset();
  cancelAsk.mockReset();
  streamAsk.mockImplementation((_q: string, signal: AbortSignal) => controllable(signal));
});

const QUESTION = "What runs the harbour?";

async function chat() {
  const { useAsk } = await import("./useAsk");
  const { ChatPane } = await import("./components/ChatPane");
  let api: ReturnType<typeof useAsk> | null = null;
  function Harness() {
    const ask = useAsk();
    api = ask;
    return <ChatPane ask={ask} showInternals={false} onHoverChunks={() => {}} />;
  }
  render(<Harness />);
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: QUESTION } });
  fireEvent.click(screen.getByRole("button", { name: UI.ask }));
  await waitFor(() => expect(streams).toHaveLength(1));
  // The request id, the plan, one section written and the next one being written.
  act(() => {
    for (const e of [{ type: "request", t: 0, request_id: "rid-1" } as AskEvent, ...head, ...sec0, start(1, "The guild")]) streams[0].push(e);
  });
  await waitFor(() => expect(screen.getByText(DEEP.writing)).toBeInTheDocument());
  return () => api!;
}

test("while an answer streams the button is Stop, and stopping keeps what arrived, marked stopped", async () => {
  const api = await chat();
  const input = screen.getByLabelText("Question") as HTMLInputElement;
  expect(input.value).toBe("");                                   // sent: the box was emptied
  expect(screen.queryByRole("button", { name: UI.ask })).toBeNull();
  const stop = screen.getByRole("button", { name: UI.stopHint });
  expect(stop).toHaveTextContent(UI.stop);
  expect(stop).toBeEnabled();

  fireEvent.click(stop);
  expect(streams[0].signal.aborted).toBe(true);                   // the stream is cut at once
  expect(cancelAsk).toHaveBeenCalledWith("rid-1");                // and the server told to stop the run
  expect(api().state.status).toBe("stopped");
  // What had streamed stays: the written section, and the one being written is marked as stopped.
  expect(screen.getByText(/The council rules the harbour/)).toBeInTheDocument();
  expect(screen.getByText(DEEP.skipped.stopped)).toBeInTheDocument();
  expect(screen.queryByText(DEEP.writing)).toBeNull();
  expect(screen.getByText(UI.stoppedNote)).toBeInTheDocument();
  expect(screen.getByText(UI.stopped)).toBeInTheDocument();
  // The box is usable again with the question in it, ready to ask again.
  expect(input.value).toBe(QUESTION);
  expect(document.activeElement).toBe(input);
  expect(screen.queryByRole("button", { name: UI.stopHint })).toBeNull();
  expect(screen.getByRole("button", { name: UI.ask })).toBeEnabled();
  // Not a turn of the chat: nothing in history.
  await new Promise((r) => setTimeout(r, 20));
  expect(api().history).toHaveLength(0);
});

test("Esc in the ask box stops the answer", async () => {
  const api = await chat();
  const input = screen.getByLabelText("Question") as HTMLInputElement;
  input.focus();
  fireEvent.keyDown(input, { key: "Escape" });
  expect(streams[0].signal.aborted).toBe(true);
  expect(cancelAsk).toHaveBeenCalledWith("rid-1");
  expect(api().state.status).toBe("stopped");
  expect(input.value).toBe(QUESTION);
});

test("asking again after a stop sends the question afresh, with no chat to follow", async () => {
  await chat();
  fireEvent.click(screen.getByRole("button", { name: UI.stopHint }));
  fireEvent.click(screen.getByRole("button", { name: UI.ask }));
  await waitFor(() => expect(streams).toHaveLength(2));
  expect(streamAsk).toHaveBeenLastCalledWith(QUESTION, expect.anything(), "all", "auto", undefined);
  // The stopped answer is not kept as an earlier turn of the thread.
  expect(screen.queryByRole("region", { name: "Earlier in this chat" })).toBeNull();
});

describe("the ask box alone", () => {
  test("not busy: Ask, and Esc does nothing", () => {
    const onStop = vi.fn();
    render(<AskBox onAsk={() => {}} busy={false} onStop={onStop} />);
    fireEvent.keyDown(screen.getByLabelText("Question"), { key: "Escape" });
    expect(onStop).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: UI.ask })).toBeInTheDocument();
  });

  test("busy: Stop is a keyboard-reachable button; text typed meanwhile is kept", () => {
    const onStop = vi.fn(() => "the old question");
    render(<AskBox onAsk={() => {}} busy onStop={onStop} />);
    const input = screen.getByLabelText("Question") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "a new one" } });
    const stop = screen.getByRole("button", { name: UI.stopHint });
    expect(stop).toHaveAttribute("type", "button");
    expect(stop).toHaveAttribute("aria-keyshortcuts", "Escape");
    stop.focus();
    fireEvent.keyDown(stop, { key: "Escape" });
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(input.value).toBe("a new one");
  });

  test("busy without a stop handler keeps the old disabled button", () => {
    render(<AskBox onAsk={() => {}} busy />);
    expect(screen.getByRole("button", { name: "Answering…" })).toBeDisabled();
  });
});

describe("the reducer", () => {
  test("stopped marks a running answer and leaves a finished one alone", () => {
    const running = [...head, ...sec0, start(1, "The guild")].reduce(askReducer, initialState("q"));
    const stopped = askReducer(running, { type: "stopped", t: 3 });
    expect(stopped.status).toBe("stopped");
    expect(stopped.sections.map((s) => [s.state, s.reason])).toEqual([["done", null], ["skipped", "stopped"]]);
    expect(stopped.steps.some((s) => s.state === "running")).toBe(false);
    expect(stopped.deepSteps.some((d) => d.state === "running")).toBe(false);
    const errored = askReducer(initialState("q"), { type: "error", t: 0, error: { code: "busy", message: "" } });
    expect(askReducer(errored, { type: "stopped", t: 1 })).toEqual({ ...errored, elapsed: 1 });
  });
});
