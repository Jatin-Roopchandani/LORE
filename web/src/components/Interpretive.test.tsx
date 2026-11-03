import { fireEvent, render, screen, within } from "@testing-library/react";
import { askReducer, initialState } from "../askReducer";
import * as lens from "../fixtures/lens";
import { DEEP } from "../messages";
import type { AskEvent, SearchResult } from "../types";
import { ChatPane } from "./ChatPane";

const stateOf = (events: AskEvent[]) => events.reduce(askReducer, initialState("Who is boldest in each book?"));
const askWith = (events: AskEvent[]) => ({ state: stateOf(events), history: [], current: null, run: 1,
  ask: vi.fn(), replay: vi.fn(), retry: vi.fn(), clear: vi.fn(), depth: "auto" as const, setDepth: vi.fn(), busy: false });
const view = (events: AskEvent[]) => render(<ChatPane ask={askWith(events)} showInternals onHoverChunks={() => {}} />);

// ---- A. the short answer comes first ----------------------------------------------------------

test("the short answer is a highlighted block at the top, above the contents and the sections", () => {
  const { container } = view(lens.full);
  const block = container.querySelector(".short-answer")!;
  expect(block).not.toBeNull();
  expect(within(block as HTMLElement).getByRole("heading", { name: DEEP.shortAnswer })).toBeInTheDocument();
  // One sentence per interpretive part: a reading, and code's line for the part with no position.
  expect(block.textContent).toContain("the evidence suggests Rin is boldest.");
  expect(block.textContent).toContain("The evidence found isn't strong enough for a clear verdict on this.");
  // Each part is marked by its short label, never by its question.
  expect(block.textContent).not.toContain(lens.PART_A);
  expect(block.textContent).not.toContain(lens.PART_B);
  const answer = container.querySelector(".a")!;
  const order = [...answer.children].map((n) => n.className.split(" ")[0]);
  expect(order.indexOf("short-answer")).toBeGreaterThanOrEqual(0);
  expect(order.indexOf("short-answer")).toBeLessThan(order.indexOf("sectioned"));
  // Inside the sectioned answer nothing precedes the contents: the short answer is not repeated there.
  expect(container.querySelectorAll(".short-answer")).toHaveLength(1);
});

test("the short answer shows as soon as it is sent, before any section is written", () => {
  view([...lens.head, lens.plan, lens.shortEvent([lens.shortA, lens.shortB])]);
  expect(screen.getByRole("heading", { name: DEEP.shortAnswer })).toBeInTheDocument();
  expect(screen.getByText(/In the second book, on balance Tam is boldest/)).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "What did Rin risk" })).toBeNull();
});

test("its citations open the passage, and its readings are not labelled again inside the block", () => {
  const { container } = view([...lens.head, lens.plan, lens.shortEvent([lens.shortA, lens.shortB])]);
  expect(container.querySelector(".short-answer .kind-label")).toBeNull();
  fireEvent.click(within(container.querySelector(".short-answer") as HTMLElement).getAllByRole("button", { name: "[1]" })[0]);
  expect(screen.getByText("crossed the pass in winter")).toBeInTheDocument();
});

test("an ordinary answer has no short answer block", () => {
  const { container } = view(lens.head);
  expect(container.querySelector(".short-answer")).toBeNull();
});

const proseResult = (extra: Partial<SearchResult>): AskEvent => ({ type: "answer", t: 1, result: {
  answer: "", mode: "graph", template: "n", note: null, model: "m", selection_model: "m", sub_questions: ["q"],
  claims: lens.claims, passages: [{ n: 1, chunk_id: "c1", book_id: "b1", chapter_seq: 1, text: "Rin crossed the pass in winter." }],
  edges: [], seeds: [], answer_style: "prose",
  prose: [{ text: "Rin crossed the pass in winter.", claims: [1], passages: [1], new_paragraph: false }], ...extra } });

test("a prose answer that is not sectioned opens with its short answer too", () => {
  const { container } = view([proseResult({ short_answer: [lens.shortA] })]);
  const answer = container.querySelector(".a")!;
  const classes = [...answer.children].map((n) => n.className.split(" ")[0]);
  expect(classes.indexOf("short-answer")).toBeLessThan(classes.indexOf("prose-answer"));
  expect(screen.getByRole("heading", { name: DEEP.shortAnswer })).toBeInTheDocument();
});

test("the facts-only fallback also opens with the short answer when there is one", () => {
  const { container } = view([proseResult({ answer_style: "claims", prose: [], short_answer: [lens.noPosition] })]);
  expect(container.querySelector(".short-answer")?.textContent).toContain("isn't strong enough for a clear verdict");
});

// ---- B. sections follow the question's own structure ------------------------------------------

test("with several parts, each part is an h2 and its sections h3 beneath it, in a nested contents list", () => {
  const { container } = view(lens.full);
  const nav = screen.getByRole("navigation", { name: DEEP.contents });
  const parts = [...nav.querySelectorAll(":scope > ol > li.part")];
  expect(parts.map((li) => li.querySelector(":scope > .entry > a")?.textContent)).toEqual([lens.PART_A, lens.PART_B]);
  const h2 = [...container.querySelectorAll(".sectioned h2.part-heading")].map((n) => n.textContent);
  expect(h2).toEqual([lens.PART_A, lens.PART_B, "Counter-case and limits"]);
  expect(screen.getByRole("heading", { name: "What did Rin risk", level: 3 })).toHaveClass("section-heading");
  expect(container.querySelector(".group-heading")).toBeNull();
});

test("the contents numbers parts 1, 2, ... and their sections 1.1, 2.1, with none repeated or skipped", () => {
  view(lens.full);   // a failed and an empty section are hidden: no gap where they were
  const nav = screen.getByRole("navigation", { name: DEEP.contents });
  const nums = [...nav.querySelectorAll("li > .entry > .num")].map((n) => n.textContent);
  expect(nums).toEqual(["1.", "1.1", "2.", "2.1", "3."]);
  expect(new Set(nums).size).toBe(nums.length);
  expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(
    [lens.PART_A, "What did Rin risk", lens.PART_B, "Who stood by Tam", "Counter-case and limits"]);
});

test("every contents entry is a link to an anchor that exists", () => {
  const { container } = view(lens.full);
  const nav = screen.getByRole("navigation", { name: DEEP.contents });
  for (const a of within(nav).getAllByRole("link")) {
    const id = a.getAttribute("href")!.slice(1);
    expect(container.querySelector(`#${id}`), id).not.toBeNull();
  }
  expect(nav.querySelectorAll("li > .pending")).toHaveLength(0);
});

const oneGroup = (events: AskEvent[]) => events.map((e) => e.type === "section" || e.type === "plan"
  ? JSON.parse(JSON.stringify(e).replaceAll(lens.PART_A, "").replaceAll(lens.PART_B, "")) as AskEvent : e);

test("a question with one part has no part heading and the contents lists only its sections, 1..N", () => {
  const { container } = view(oneGroup(lens.full));
  expect(container.querySelector(".part-heading")).toBeNull();
  expect(container.querySelector(".sectioned h2")).toBeNull();
  const nav = screen.getByRole("navigation", { name: DEEP.contents });
  expect(nav.querySelectorAll("li.part")).toHaveLength(0);
  expect([...nav.querySelectorAll("li > .entry > .num")].map((n) => n.textContent)).toEqual(["1.", "2.", "3."]);
  expect(screen.getByRole("heading", { name: "What did Rin risk", level: 3 })).toBeInTheDocument();
  for (const a of within(nav).getAllByRole("link")) expect(container.querySelector(a.getAttribute("href")!)).not.toBeNull();
});

test("a section heading carries its full lens question as a tooltip, and as a subtitle when the heading is a label", () => {
  const { container } = view(lens.full);
  const head = screen.getByRole("heading", { name: "What did Rin risk", level: 3 });
  expect(head).toHaveAttribute("title", lens.TITLE_0);
  // The heading is the start of the question, so no subtitle repeats it.
  expect(container.querySelector(".section-question")).toBeNull();
  const labelled = lens.full.map((e) => e.type === "section" || e.type === "plan"
    ? JSON.parse(JSON.stringify(e).replaceAll('"What did Rin risk"', '"Risks he ran"')) as AskEvent : e);
  const again = render(<ChatPane ask={askWith(labelled)} showInternals onHoverChunks={() => {}} />);
  expect(again.container.querySelector(".section-question")?.textContent).toBe(lens.TITLE_0);
  expect(within(again.container as HTMLElement).getByRole("heading", { name: "Risks he ran", level: 3 })).toHaveAttribute("title", lens.TITLE_0);
});

// ---- D. failed and empty sections are hidden, and counted ----------------------------------------

test("a failed or empty section is in neither the contents nor the body", () => {
  const { container } = view(lens.full);
  expect(screen.queryByText("What did Tam refuse")).toBeNull();
  expect(screen.queryByText("Who stood by Rin")).toBeNull();
  expect(screen.queryByText(/couldn.t be written\.$/)).toBeNull();     // no per-section failure line
  const nav = screen.getByRole("navigation", { name: DEEP.contents });
  expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual([lens.PART_A, "What did Rin risk", lens.PART_B, "Who stood by Tam", "Counter-case and limits"]);
  expect(container.querySelectorAll("section.answer-section")).toHaveLength(3);
});

test("one muted line at the end says how many sections could not be written", () => {
  const { container } = view(lens.full);
  const line = container.querySelector(".unwritten")!;
  expect(line.textContent).toBe(DEEP.unwritten(2));
  expect(DEEP.unwritten(2)).toBe("2 sections couldn't be written");
  expect(DEEP.unwritten(1)).toBe("1 section couldn't be written");
  const sectioned = container.querySelector(".sectioned")!;
  expect(sectioned.lastElementChild).toBe(line);
});

test("while the answer streams the line counts the sections that have failed so far, and no line when none did", () => {
  const { container, rerender } = view([...lens.head, lens.plan, lens.shortEvent([lens.shortA]), ...lens.sec[0]]);
  expect(container.querySelector(".unwritten")).toBeNull();
  rerender(<ChatPane ask={askWith([...lens.head, lens.plan, lens.shortEvent([lens.shortA]), ...lens.sec[0], ...lens.sec[1]])} showInternals onHoverChunks={() => {}} />);
  expect(container.querySelector(".unwritten")?.textContent).toBe(DEEP.unwritten(1));
});

test("a short answer part opens with its label as a small muted inline mark, not as text of the sentence", () => {
  const { container } = view([...lens.head, lens.plan, lens.shortEvent([lens.shortA, lens.noPosition])]);
  const labels = [...container.querySelectorAll(".short-answer .part-label")];
  expect(labels.map((n) => n.textContent)).toEqual(["First book", "Second book"]);
  const paras = container.querySelectorAll(".short-answer p.prose");
  expect(paras[0].firstElementChild).toBe(labels[0]);
  expect(paras[0].querySelector(".sentence")!.textContent).not.toContain("First book");
  expect(container.querySelector(".short-answer .kind-label")).toBeNull();
});

test("a short answer with no label shows no label element", () => {
  const { container } = view([proseResult({ short_answer: [{ ...lens.shortA, label: "" }] })]);
  expect(container.querySelector(".short-answer .part-label")).toBeNull();
});
