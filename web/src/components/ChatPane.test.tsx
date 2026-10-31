import { fireEvent, render, screen } from "@testing-library/react";
import single from "../fixtures/single-part.json";
import { budget } from "../fixtures/synthetic";
import { askReducer, initialState } from "../askReducer";
import { ChatPane } from "./ChatPane";
import { UI } from "../messages";
import type { AskEvent } from "../types";

const askWith = (events: AskEvent[], extra = {}) => ({
  state: events.reduce(askReducer, initialState("q")), history: [], current: null, run: 1,
  ask: vi.fn(), replay: vi.fn(), retry: vi.fn(), clear: vi.fn(), depth: "auto" as const, setDepth: vi.fn(), busy: false, ...extra });

test("renders steps, claims and the how panel for a recorded answer", () => {
  render(<ChatPane ask={askWith(single as AskEvent[])} showInternals onHoverChunks={() => {}} />);
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
