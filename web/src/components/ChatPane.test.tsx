import { fireEvent, render, screen } from "@testing-library/react";
import single from "../fixtures/single-part.json";
import { budget } from "../fixtures/synthetic";
import { askReducer, initialState } from "../askReducer";
import { ChatPane } from "./ChatPane";
import { POSITION, PROCESSING, UI } from "../messages";
import type { AskEvent } from "../types";

const askWith = (events: AskEvent[], extra = {}) => ({
  state: events.reduce(askReducer, initialState("q")), history: [], current: null, run: 1,
  ask: vi.fn(), replay: vi.fn(), retry: vi.fn(), clear: vi.fn(), depth: "auto" as const, setDepth: vi.fn(), busy: false, ...extra });

test("renders steps, claims and the how panel for a recorded answer", () => {
  localStorage.clear();
  render(<ChatPane ask={askWith(single as AskEvent[])} showInternals onHoverChunks={() => {}} />);
  // Once the answer has landed the steps are folded into one line; opening it shows them.
  expect(screen.queryByText("Splitting the question")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /How this was found/ }));
  expect(screen.getByText("Splitting the question")).toBeInTheDocument();
  expect(screen.getByText("How this was answered")).toBeInTheDocument();
  expect(screen.getAllByRole("listitem").length).toBeGreaterThan(3);
});

test("an error shows its message and a retry for retryable codes", () => {
  render(<ChatPane ask={askWith([{ type: "error", t: 0, error: { code: "upstream_unavailable", message: "" } }])} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByText(/busy right now/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
});

test("the budget message has no retry", () => {
  render(<ChatPane ask={askWith(budget)} showInternals onHoverChunks={() => {}} />);
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
});

test("models are hidden without internals", () => {
  render(<ChatPane ask={askWith(single as AskEvent[])} showInternals={false} onHoverChunks={() => {}} />);
  expect(screen.queryByText(/gemini/i)).toBeNull();
});

test("re-asking the same question resets the open claim passage", () => {
  const events = single as AskEvent[];
  const { rerender } = render(<ChatPane ask={askWith(events, { run: 1 })} showInternals onHoverChunks={() => {}} />);
  const cite = screen.getAllByRole("button", { name: /ch\.\d+/ })[0];
  fireEvent.click(cite);
  expect(cite).toHaveAttribute("aria-expanded", "true");
  rerender(<ChatPane ask={askWith(events, { run: 2 })} showInternals onHoverChunks={() => {}} />);
  expect(screen.getAllByRole("button", { name: /ch\.\d+/ })[0]).toHaveAttribute("aria-expanded", "false");
});

const overview = { truncated: false, depth: 0, edges: [], nodes: [
  { id: "h", name: "Holmes", kind: "Character", books: ["b1"] }, { id: "w", name: "Watson", kind: "Character", books: ["b1"] }] };
const idle = (extra = {}) => ({ state: initialState(), history: [], current: null, run: 0,
  ask: vi.fn(), replay: vi.fn(), retry: vi.fn(), clear: vi.fn(), depth: "auto" as const, setDepth: vi.fn(), busy: false, ...extra });

test("with no question the thread explains itself and offers example questions", () => {
  const ask = idle();
  render(<ChatPane ask={ask} overview={overview} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByText(UI.emptyIntro)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Tell me about Holmes" }));
  expect(ask.ask).toHaveBeenCalledWith("Tell me about Holmes");
  expect(screen.getByRole("button", { name: "How is Holmes connected to Watson?" })).toBeInTheDocument();
});

test("without an overview only the explainer shows", () => {
  render(<ChatPane ask={idle()} overview={null} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByText(UI.emptyIntro)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Tell me about/ })).toBeNull();
});

test("a question replaces the empty state", () => {
  render(<ChatPane ask={askWith(single as AskEvent[])} overview={overview} showInternals onHoverChunks={() => {}} />);
  expect(screen.queryByText(UI.emptyIntro)).toBeNull();
  expect(screen.queryByRole("button", { name: /Tell me about/ })).toBeNull();
});

test("a prose answer shows the prose and a collapsed evidence list", () => {
  const events = (single as AskEvent[]).map((e) => e.type === "answer" ? { ...e, result: { ...e.result, answer_style: "prose",
    prose: [{ text: "A written sentence.", claims: [1], passages: [e.result.claims[0].passage], new_paragraph: false }] } } : e);
  render(<ChatPane ask={askWith(events as AskEvent[])} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByText("A written sentence.")).toBeInTheDocument();
  const details = screen.getByText(/^Evidence \(/).closest("details")!;
  expect(details).not.toHaveAttribute("open");
});

test("an old result without prose renders the claim view", () => {
  render(<ChatPane ask={askWith(single as AskEvent[])} showInternals onHoverChunks={() => {}} />);
  expect(screen.queryByText(/^Evidence \(/)).toBeNull();
  expect(document.querySelectorAll("ul.claims li").length).toBeGreaterThan(0);
});

test("a prose fallback shows the note", () => {
  const events = (single as AskEvent[]).map((e) => e.type === "answer" ? { ...e, result: { ...e.result, answer_style: "claims",
    note: "the written answer did not pass verification; showing the checked facts" } } : e);
  render(<ChatPane ask={askWith(events as AskEvent[])} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByText(/didn't pass verification/)).toBeInTheDocument();
});

test("the reading position note shows under the short answer, only when the answer carries one", () => {
  const withNote = (single as AskEvent[]).map((e) => e.type === "answer"
    ? { ...e, result: { ...e.result, position_note: "Answered from what you've read: up to chapter 9 of Hollow Gate" } } : e) as AskEvent[];
  const { rerender } = render(<ChatPane ask={askWith(withNote)} showInternals onHoverChunks={() => {}} />);
  const note = screen.getByRole("note", { name: POSITION.noteLabel });
  expect(note).toHaveTextContent("Answered from what you've read: up to chapter 9 of Hollow Gate");
  rerender(<ChatPane ask={askWith(single as AskEvent[])} showInternals onHoverChunks={() => {}} />);
  expect(screen.queryByRole("note", { name: POSITION.noteLabel })).toBeNull();
});

test("a finished answer with a request id offers \"Report this answer\"; one without, or still running, does not", () => {
  const withId = [{ type: "request", t: 0, request_id: "req-1" } as AskEvent, ...(single as AskEvent[])];
  const { unmount } = render(<ChatPane ask={askWith(withId)} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByRole("button", { name: "Report this answer" })).toBeInTheDocument();
  unmount();
  render(<ChatPane ask={askWith(single as AskEvent[])} showInternals onHoverChunks={() => {}} />);
  expect(screen.queryByRole("button", { name: "Report this answer" })).toBeNull();
});

test("an empty answer the reading position held back says the reader hasn't reached it; a plain one keeps the refusal", () => {
  const empty = (ahead: boolean) => (single as AskEvent[]).map((e) => e.type === "answer"
    ? { ...e, result: { ...e.result, mode: "empty", claims: [], prose: [], answer_style: "claims", ahead,
        position_note: "Answered from what you've read: up to chapter 3 of Hollow Gate" } } : e) as AskEvent[];
  const { rerender } = render(<ChatPane ask={askWith(empty(true))} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByText(POSITION.ahead)).toBeInTheDocument();
  expect(screen.queryByText(UI.refusal)).toBeNull();
  rerender(<ChatPane ask={askWith(empty(false))} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByText(UI.refusal)).toBeInTheDocument();
  expect(screen.queryByText(POSITION.ahead)).toBeNull();
});

test("an empty answer while a book is still processing names it, its progress and when it continues", () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-18T05:00:00Z"));
  const processing = [{ book_id: "big", title: "Les Misérables", stage: "extract", state: "waiting_quota", done: 969,
    total: 1051, resumable_at: "2026-11-18T08:00:00+00:00" }];
  const empty = (answer: string) => (single as AskEvent[]).map((e) => e.type === "answer"
    ? { ...e, result: { ...e.result, answer, mode: "empty", claims: [], prose: [], answer_style: "claims", processing } } : e) as AskEvent[];
  const time = new Date("2026-11-18T08:00:00Z").toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const { rerender } = render(<ChatPane ask={askWith(empty("Les Misérables is still being processed …"))} showInternals onHoverChunks={() => {}} />);
  const note = screen.getByRole("note", { name: PROCESSING.noteLabel });
  expect(note).toHaveTextContent(
    `Les Misérables is still being processed (969 of 1,051 parts read), so it can’t be searched yet. `
    + `LORE will continue automatically after ${time} (your time) — in 3h 0m. Ask again when it’s ready.`);
  expect(screen.queryByText(UI.refusal)).toBeNull();
  expect(note).not.toHaveTextContent(PROCESSING.readyEmpty);
  // A world that also has ready books: they were searched and had nothing.
  rerender(<ChatPane ask={askWith(empty(`${PROCESSING.readyEmptyServer} Les Misérables …`))} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByRole("note", { name: PROCESSING.noteLabel })).toHaveTextContent(PROCESSING.readyEmpty);
  vi.useRealTimers();
});

test("an answer from the ready books says the unprocessed one was left out", () => {
  const processing = [{ book_id: "big", title: "Les Misérables", stage: "extract", state: "running", done: 12,
    total: 1051, resumable_at: null }];
  const events = (single as AskEvent[]).map((e) => e.type === "answer" ? { ...e, result: { ...e.result, processing } } : e) as AskEvent[];
  render(<ChatPane ask={askWith(events)} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByRole("list", { name: PROCESSING.noteLabel })).toHaveTextContent(`Les Misérables ${PROCESSING.leftOut}`);
});

test("the ask box warns of a book still being processed", () => {
  const book = { id: "big", title: "Les Misérables", author: null, status: "chunked", world: "Les Misérables", series: null,
    series_index: null, processing: true,
    job: { stage: "extract", state: "waiting_quota" as const, done: 969, total: 1051, stopped_reason: null,
      resumable_at: null, can_continue: false, continue_reason: "", provider: null, plan: null } };
  render(<ChatPane ask={askWith([])} pending={[book as never]} showInternals onHoverChunks={() => {}} />);
  expect(screen.getByRole("note", { name: PROCESSING.noteLabel })).toHaveTextContent(
    "Les Misérables is still being processed, so questions about it will work once it’s ready (969/1,051).");
  expect(screen.getByRole("button", { name: UI.ask })).toBeInTheDocument();
});
