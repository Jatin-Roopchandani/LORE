import { render, screen, within } from "@testing-library/react";
import { askReducer, initialState } from "../askReducer";
import { STEPS_DEEP } from "../messages";
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
const render_ = (events: AskEvent[]) => render(<StepList state={events.reduce(askReducer, initialState("q"))} />);

test("lens parts are grouped under their interpretive part, other parts are not", () => {
  render_(lensRun);
  const group = screen.getByRole("list", { name: STEPS_DEEP.partsLabel });
  const heading = within(group).getByText("Is she a good leader?");
  const lenses = within(heading.closest("li")!).getAllByText(/Does she (lead|follow)\?/);
  expect(lenses.map((l) => l.textContent)).toEqual(["Does she lead?", "Does she follow?"]);
  expect(within(group).queryByText("Where is the tower?")).toBeNull();
  expect(within(group).getByText(STEPS_DEEP.lensDetail(1, 7))).toBeInTheDocument();
});

test("without lens_of there is no group", () => {
  render_([{ type: "parts", t: 0, sub_questions: ["a", "b"] }]);
  expect(screen.queryByRole("list", { name: STEPS_DEEP.partsLabel })).toBeNull();
});

test("the group sits after the search row", () => {
  const { container } = render_(lensRun);
  const rows = [...container.querySelectorAll("ol.steps > li")].map((li) => li.className.split(" ")[0] || li.textContent);
  const search = [...container.querySelectorAll("ol.steps > li")].findIndex((li) => li.textContent?.includes("Searching the graph"));
  expect(container.querySelectorAll("ol.steps > li")[search + 1]).toHaveClass("lens-groups");
  expect(rows.length).toBeGreaterThan(search + 1);
});
