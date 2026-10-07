import { render, screen } from "@testing-library/react";
import single from "../fixtures/single-part.json";
import { budget } from "../fixtures/synthetic";
import { askReducer, initialState } from "../askReducer";
import { ChatPane } from "./ChatPane";
import type { AskEvent } from "../types";

const askWith = (events: AskEvent[], extra = {}) => ({
  state: events.reduce(askReducer, initialState("q")), history: [], current: null,
  ask: vi.fn(), replay: vi.fn(), retry: vi.fn(), busy: false, ...extra });

test("renders steps, claims and the how panel for a recorded answer", () => {
  render(<ChatPane ask={askWith(single as AskEvent[])} showInternals onHoverChunk={() => {}} />);
  expect(screen.getByText("Splitting the question")).toBeInTheDocument();
  expect(screen.getByText("How this was answered")).toBeInTheDocument();
  expect(screen.getAllByRole("listitem").length).toBeGreaterThan(3);
});

test("an error shows its message and a retry for retryable codes", () => {
  render(<ChatPane ask={askWith([{ type: "error", t: 0, error: { code: "upstream_unavailable", message: "" } }])} showInternals onHoverChunk={() => {}} />);
  expect(screen.getByText(/busy right now/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
});

test("the budget message has no retry", () => {
  render(<ChatPane ask={askWith(budget)} showInternals onHoverChunk={() => {}} />);
  expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
});

test("models are hidden without internals", () => {
  render(<ChatPane ask={askWith(single as AskEvent[])} showInternals={false} onHoverChunk={() => {}} />);
  expect(screen.queryByText(/gemini/i)).toBeNull();
});
