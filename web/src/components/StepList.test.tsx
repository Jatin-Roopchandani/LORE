import { fireEvent, render, screen, within } from "@testing-library/react";
import { askReducer, initialState } from "../askReducer";
import * as deep from "../fixtures/deep";
import * as lens from "../fixtures/lens";
import { STEPS_DEEP } from "../messages";
import { STEPS_KEY, SPLIT_KEY } from "../stepsPref";
import type { AskEvent } from "../types";
import { StepList } from "./StepList";

const step = (name: any, state: any, part: number | null = null, detail: string | null = null): AskEvent => ({ type: "step", t: 1, name, state, part, detail });
const lensRun: AskEvent[] = [
  { type: "parts", t: 0, sub_questions: ["Does she lead?", "Does she follow?", "Where is the tower?"], lens_of: [0, 0, null],
    plan_questions: ["Is she a good leader?", "Where is the tower?"] },
  step("seeds", "start", 0),
  { type: "seeds", t: 1, part: 0, entities: [{ id: "a", name: "Mara", kind: "Character", books: ["b"], score: 1 }] },
  { type: "edges", t: 1, part: 0, template: "neighbours", total: 7, edges: [] },
  step("search", "start", 0),
];
const stateOf = (events: AskEvent[]) => events.reduce(askReducer, initialState("q"));
const render_ = (events: AskEvent[]) => render(<StepList state={stateOf(events)} />);

beforeEach(() => localStorage.clear());

// ---- G2: the question's parts and lenses are a labelled, structured list -------------------------------

test("lens parts are listed under their interpretive part, other parts as lines of their own", () => {
  render_(lensRun);
  const list = screen.getByRole("list", { name: STEPS_DEEP.partsLabel });
  const heading = within(list).getByText("Is she a good leader?");
  const lenses = within(heading.closest("li")!).getAllByText(/Does she (lead|follow)\?/);
  expect(lenses.map((l) => l.textContent)).toEqual(["Does she lead?", "Does she follow?"]);
  // The other part is a line of its own, with no lenses beneath it.
  const tower = within(list).getByText("Where is the tower?").closest("li")!;
  expect(within(tower).queryByText(STEPS_DEEP.lensLabel)).toBeNull();
  expect(within(list).getByText(STEPS_DEEP.lensDetail(1, 7))).toBeInTheDocument();
});

test("the list has a small label, and each part's lenses sit under a lens label", () => {
  render_(lensRun);
  expect(screen.getByRole("button", { name: new RegExp(STEPS_DEEP.splitLabel) })).toBeInTheDocument();
  expect(STEPS_DEEP.splitLabel).toBe("Question split into");
  expect(STEPS_DEEP.lensLabel).toBe("looked at:");
  const heading = screen.getByText("Is she a good leader?").closest("li")!;
  expect(within(heading).getByText(STEPS_DEEP.lensLabel)).toBeInTheDocument();
});

test("a long sentence stays on one line, with its full text in a tooltip", () => {
  const long = "Does she lead the others through every hard winter that the long road asks of them?";
  render_([{ type: "parts", t: 0, sub_questions: [long, "Does she follow?"], lens_of: [0, 0], plan_questions: [`${long} And more.`] }]);
  const line = screen.getByText(long);
  expect(line).toHaveAttribute("title", long);
  expect(line).toHaveClass("lens-q");
  expect(screen.getByText(`${long} And more.`)).toHaveAttribute("title", `${long} And more.`);
});

test("without lenses, parts are plain lines; one part needs no list", () => {
  render_([{ type: "parts", t: 0, sub_questions: ["a", "b"] }]);
  const list = screen.getByRole("list", { name: STEPS_DEEP.partsLabel });
  expect(within(list).getAllByRole("listitem").map((l) => l.textContent)).toEqual(["a", "b"]);
  expect(screen.queryByText(STEPS_DEEP.lensLabel)).toBeNull();
});

test("a single part shows no split list", () => {
  render_([{ type: "parts", t: 0, sub_questions: ["only"] }]);
  expect(screen.queryByRole("list", { name: STEPS_DEEP.partsLabel })).toBeNull();
});

test("the split list sits right after the split row", () => {
  const { container } = render_(lensRun);
  const rows = [...container.querySelectorAll("ol.steps > li")];
  const at = rows.findIndex((li) => li.textContent?.includes("Splitting the question"));
  expect(rows[at + 1]).toHaveClass("split");
});

// ---- G2: sections that are lenses are not listed twice ----------------------------------------------

test("in a lens answer each lens line carries its section's status; there are no separate section rows", () => {
  const events = [...lens.head, lens.plan, lens.shortEvent([lens.shortA]), ...lens.sec[0], ...lens.sec[1], ...lens.sec[2], ...lens.sec[3].slice(0, 2)];
  const { container } = render_(events);
  expect(screen.queryByText(/^Section \d/)).toBeNull();
  const lensLines = [...container.querySelectorAll("li.lens")];
  expect(lensLines).toHaveLength(4);
  const marks = lensLines.map((l) => l.querySelector(".mark")?.textContent);
  expect(marks).toEqual(["✓", "✕", "–", "●"]);   // done, failed, found nothing, still being written
  expect(within(lensLines[1] as HTMLElement).getByText(STEPS_DEEP.failed)).toBeInTheDocument();
});

test("a topical answer keeps its section rows and does not list the sections again as parts", () => {
  const { container } = render_([...deep.head, { type: "parts", t: 1.1, sub_questions: ["The council", "The guild"] }, ...deep.sec0]);
  expect(screen.getByText("Section 1: The council")).toBeInTheDocument();
  expect(container.querySelector("li.split")).toBeNull();
});

// ---- G3: honest marks, and timing that stays on one line --------------------------------------------

test("a failed section row shows a failed mark and a skipped one a skipped mark, never a tick", () => {
  const skipped = { type: "section", t: 3, index: 1, state: "skipped", kind: "section", heading: "The guild", reason: "budget" } as AskEvent;
  const failed = deep.done(0, "The council", [], [], null, [], { style: "failed" });
  const { container } = render_([...deep.head, deep.sec0[0], deep.sec0[1], failed, step("section", "done", 0, "failed"), skipped]);
  const rows = [...container.querySelectorAll("li.section-step")];
  expect(rows.map((r) => r.querySelector(".dot")?.textContent)).toEqual(["✕", "–"]);
  expect(rows.map((r) => r.className)).toEqual(expect.arrayContaining([expect.stringContaining("failed"), expect.stringContaining("skipped")]));
  expect(within(rows[0] as HTMLElement).getByText(STEPS_DEEP.failed)).toBeInTheDocument();
});

test("a row's label can be cut with an ellipsis while its timing stays whole", () => {
  const { container } = render_([...deep.head, ...deep.sec0]);
  const row = container.querySelector("li.section-step")!;
  const label = row.querySelector(".lbl")!;
  expect(label).toHaveAttribute("title", "Section 1: The council");
  expect(row.querySelector(".t")?.textContent).toMatch(/2 claims/);
});

// ---- G1: collapsible ---------------------------------------------------------------------------------

const answered = [...lens.full];

test("while an answer is running the steps are expanded and there is no toggle", () => {
  const { container } = render_(lensRun);
  expect(container.querySelector("ol.steps")).not.toBeNull();
  expect(screen.queryByRole("button", { name: /How this was found/ })).toBeNull();
});

test("once the answer arrives the steps collapse into one summary line on a real button", () => {
  const { container } = render_(answered);
  expect(container.querySelector("ol.steps")).toBeNull();
  const toggle = screen.getByRole("button", { name: /How this was found/ });
  expect(toggle.tagName).toBe("BUTTON");
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  // parts, sections written and seconds: the answer arrived at t = 10
  expect(toggle).toHaveTextContent("How this was found · 4 parts · 2 sections · 10 s");
  expect(STEPS_DEEP.summary(6, 4, 41)).toBe("How this was found · 6 parts · 4 sections · 41 s");
  expect(STEPS_DEEP.summary(1, 0, 3)).toBe("How this was found · 1 part · 3 s");
});

test("the button opens the steps and closes them again, and the choice is remembered", () => {
  const { container, unmount } = render_(answered);
  const toggle = screen.getByRole("button", { name: /How this was found/ });
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(container.querySelector("ol.steps")).not.toBeNull();
  expect(localStorage.getItem(STEPS_KEY)).toBe("open");
  unmount();
  // A later answer, a new mount: still open.
  const again = render_(answered);
  expect(screen.getByRole("button", { name: /How this was found/ })).toHaveAttribute("aria-expanded", "true");
  expect(again.container.querySelector("ol.steps")).not.toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /How this was found/ }));
  expect(localStorage.getItem(STEPS_KEY)).toBe("closed");
  expect(again.container.querySelector("ol.steps")).toBeNull();
});

test("an open choice does not hide the steps of the next running answer, and storage that throws is tolerated", () => {
  localStorage.setItem(STEPS_KEY, "closed");
  const running = render_(lensRun);
  expect(running.container.querySelector("ol.steps")).not.toBeNull();
  const orig = Storage.prototype.getItem;
  Storage.prototype.getItem = () => { throw new Error("no"); };
  try {
    render_(answered);
    expect(screen.getAllByRole("button", { name: /How this was found/ }).length).toBeGreaterThan(0);
  } finally { Storage.prototype.getItem = orig; }
});

test("the split list is collapsible inside the steps, and the choice is remembered", () => {
  const { container } = render_(lensRun);
  const toggle = screen.getByRole("button", { name: new RegExp(STEPS_DEEP.splitLabel) });
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("list", { name: STEPS_DEEP.partsLabel })).toBeInTheDocument();
  expect(toggle).toHaveTextContent("Question split into · 3 parts");   // the retrieval parts, lenses counted
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("list", { name: STEPS_DEEP.partsLabel })).toBeNull();
  expect(localStorage.getItem(SPLIT_KEY)).toBe("closed");
  expect(container.querySelector("ol.steps")).not.toBeNull();       // the rest of the steps are still there
});
