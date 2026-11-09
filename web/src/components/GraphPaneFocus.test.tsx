import { fireEvent, render, screen } from "@testing-library/react";
import { askReducer, initialState } from "../askReducer";
import { FOCUS_KEY } from "../filters";
import type { GraphView } from "../graphView";
import { EXPLORE } from "../messages";
import type { AskEvent } from "../types";
import ogier from "../fixtures/answer-graph-ogier.json";
import twoPart from "../fixtures/two-part.json";
import { GraphPane } from "./GraphPane";

const views: GraphView[] = [];
vi.mock("./GraphCanvas", () => ({ GraphCanvas: ({ view }: { view: GraphView }) => { views.push(view); return <div />; } }));
vi.mock("../api", () => ({
  api: { entities: () => new Promise(() => {}) },
  isError: (x: unknown) => typeof x === "object" && x !== null && "error" in x,
}));

const stateOf = (events: AskEvent[]) => events.reduce(askReducer, initialState("q"));
const props = { hiddenBooks: new Set<string>(), hoverChunks: [], noBooks: false };
beforeEach(() => { views.length = 0; sessionStorage.clear(); localStorage.clear(); });

test("a big answer opens focused; Show all switches to the whole graph and back, remembered for the session", () => {
  const { unmount } = render(<GraphPane state={stateOf(ogier as AskEvent[])} {...props} />);
  const focused = views.at(-1)!;
  expect(focused.focus).toBeDefined();
  expect(screen.getByText(EXPLORE.focused)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: EXPLORE.showAll(focused.focus!.total) }));
  const full = views.at(-1)!;
  expect(full.focus).toBeUndefined();
  expect(full.nodes.length).toBe(focused.focus!.total);
  expect(sessionStorage.getItem(FOCUS_KEY)).toBe("full");
  unmount();
  // The choice holds for the next answer in this session.
  render(<GraphPane state={stateOf(ogier as AskEvent[])} {...props} />);
  expect(views.at(-1)!.focus).toBeUndefined();
  fireEvent.click(screen.getByRole("button", { name: EXPLORE.focusAgain(focused.nodes.length) }));
  expect(views.at(-1)!.nodes.length).toBe(focused.nodes.length);
  expect(sessionStorage.getItem(FOCUS_KEY)).toBe("focused");
});

test("a small answer has no focus control and is drawn whole", () => {
  render(<GraphPane state={stateOf(twoPart as AskEvent[])} {...props} />);
  expect(views.at(-1)!.focus).toBeUndefined();
  expect(screen.queryByText(EXPLORE.focused)).toBeNull();
  expect(screen.queryByRole("button", { name: /^Show all/ })).toBeNull();
});

test("the Uncategorized chip in Filters shows the hidden relationships, and hides them again", () => {
  sessionStorage.setItem(FOCUS_KEY, "full");
  render(<GraphPane state={stateOf(ogier as AskEvent[])} {...props} />);
  const has = () => views.at(-1)!.links.some((l) => /uncategorized/.test(l.label));
  expect(has()).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: EXPLORE.filters }));
  const chip = screen.getByRole("button", { name: new RegExp(`^${EXPLORE.uncategorized}`) });
  expect(chip).toHaveAttribute("aria-pressed", "false");
  fireEvent.click(chip);
  expect(chip).toHaveAttribute("aria-pressed", "true");
  expect(has()).toBe(true);
  fireEvent.click(chip);
  expect(has()).toBe(false);
});

test("a deep answer's section brings its part forward, and names it in the header", () => {
  const state = stateOf(ogier as AskEvent[]);
  const { rerender } = render(<GraphPane state={state} {...props} section={null} />);
  expect(views.at(-1)!.nodes.every((n) => n.state === "shown")).toBe(true);
  rerender(<GraphPane state={state} {...props} section={3} />);
  const v = views.at(-1)!;
  expect(v.nodes.some((n) => n.state === "dim")).toBe(true);
  for (const s of state.seeds[3]) if (v.nodes.some((n) => n.id === s.id)) expect(v.nodes.find((n) => n.id === s.id)!.state).toBe("shown");
  expect(screen.getByText(EXPLORE.sectionShown("Makers of the Ways"))).toBeInTheDocument();
});
