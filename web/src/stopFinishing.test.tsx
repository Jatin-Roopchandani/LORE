import { act, fireEvent, render, screen } from "@testing-library/react";
import { full } from "./fixtures/deep";
import { UI } from "./messages";
import type { AskEvent } from "./types";

// With motion on, a deep answer can arrive in full while it is still being drawn. Stop has nothing left to stop then:
// the button says "Show answer" and draws it whole, and the server is never asked to cancel a finished run.

interface Stream { signal: AbortSignal; push(e: AskEvent): void; end(): void }
const streams: Stream[] = [];
const cancelAsk = vi.fn();

vi.mock("./chatsApi", async (importOriginal) => ({ ...(await importOriginal<typeof import("./chatsApi")>()),
  chatsApi: { list: () => Promise.resolve({ error: { code: "unreachable", message: "" } }), get: vi.fn(), create: vi.fn(), rename: vi.fn(),
    remove: () => Promise.resolve({ deleted: true }) } }));
vi.mock("./api", () => ({
  streamAsk: (_q: string, signal: AbortSignal) => controllable(signal),
  cancelAsk: (id: string) => cancelAsk(id),
}));
// Motion on: the scheduler staggers what it draws.
Object.defineProperty(window, "matchMedia", { value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });

function controllable(signal: AbortSignal) {
  const queue: AskEvent[] = [];
  let ended = false;
  let wake: (() => void) | null = null;
  streams.push({ signal, push: (e) => { queue.push(e); wake?.(); }, end: () => { ended = true; wake?.(); } });
  signal.addEventListener("abort", () => wake?.());
  return (async function* () {
    while (true) {
      if (signal.aborted) return;
      if (queue.length) { yield queue.shift()!; continue; }
      if (ended) return;
      await new Promise<void>((r) => { wake = r; });
    }
  })();
}

const SEEDS: AskEvent = { type: "seeds", t: 8, part: 0, entities: [1, 2, 3].map((i) => ({ id: `e${i}`, name: `E${i}`,
  kind: "Character", books: [], score: 0 })) } as AskEvent;

// Lets the stream and React run (promises only): with the clock frozen, the drawing's staggered waits never end.
const flush = () => act(async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); });

test("an answer that has all arrived but is still drawing offers Show answer, which draws it whole without a cancel", async () => {
  vi.useFakeTimers();
  const { useAsk } = await import("./useAsk");
  const { ChatPane } = await import("./components/ChatPane");
  let api: ReturnType<typeof useAsk> | null = null;
  function Harness() { const ask = useAsk(); api = ask; return <ChatPane ask={ask} showInternals={false} onHoverChunks={() => {}} />; }
  render(<Harness />);
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "What runs the harbour?" } });
  fireEvent.click(screen.getByRole("button", { name: UI.ask }));
  await flush();
  expect(streams).toHaveLength(1);
  // Still arriving: Stop.
  streams[0].push({ type: "request", t: 0, request_id: "rid-1" } as AskEvent);
  streams[0].push(full[0]);
  await flush();
  expect(api!.busy).toBe(true);
  expect(screen.getByRole("button", { name: UI.stopHint })).toBeInTheDocument();
  // Everything arrives, answer included, while the screen is still drawing (the clock is frozen mid-stagger).
  for (const e of full.slice(1, -1)) streams[0].push(e);
  streams[0].push(SEEDS);  // drawn one by one, so the drawing is still under way when the answer is in
  streams[0].push(full.at(-1)!);  // the answer
  streams[0].end();
  await flush();
  expect(api!.state.status).toBe("running");
  const show = screen.getByRole("button", { name: UI.showAnswerHint });
  expect(show).toHaveTextContent(UI.showAnswer);
  expect(screen.queryByRole("button", { name: UI.stopHint })).toBeNull();
  fireEvent.click(show);
  await flush();
  expect(cancelAsk).not.toHaveBeenCalled();          // a finished run: nothing to cancel
  expect(api!.state.status).toBe("done");            // drawn whole, not marked stopped
  expect(screen.queryByText(UI.stoppedNote)).toBeNull();
  expect(screen.getByRole("button", { name: UI.ask })).toBeInTheDocument();
  expect((screen.getByLabelText("Question") as HTMLInputElement).value).toBe(""); // nothing to ask again
  vi.useRealTimers();
});
