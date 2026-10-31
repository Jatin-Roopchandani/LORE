import { fireEvent, render, screen, within } from "@testing-library/react";
import { askReducer, initialState } from "../askReducer";
import * as deep from "../fixtures/deep";
import { DEEP, STEPS_DEEP, UI } from "../messages";
import type { AskEvent } from "../types";
import { ChatPane } from "./ChatPane";

const stateOf = (events: AskEvent[]) => events.reduce(askReducer, initialState("What runs the harbour?"));
const askWith = (events: AskEvent[], extra = {}) => ({ state: stateOf(events), history: [], current: null, run: 1,
  ask: vi.fn(), replay: vi.fn(), retry: vi.fn(), clear: vi.fn(), depth: "auto" as const, setDepth: vi.fn(), busy: false, ...extra });
const view = (events: AskEvent[], internals = true) => render(<ChatPane ask={askWith(events)} showInternals={internals} onHoverChunks={() => {}} />);

test("sections appear as they stream in, and an unfinished one shows Writing", () => {
  const ask = (events: AskEvent[]) => <ChatPane ask={askWith(events)} showInternals onHoverChunks={() => {}} />;
  const { rerender } = render(ask(deep.head));
  expect(screen.queryByRole("heading", { level: 3 })).toBeNull();               // planned, none started
  rerender(ask([...deep.head, deep.sec0[0], deep.sec0[1]]));
  expect(screen.getByRole("heading", { name: "The council" })).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(DEEP.writing);
  expect(screen.queryByRole("heading", { name: "The guild" })).toBeNull();
  rerender(ask([...deep.head, ...deep.sec0]));
  expect(screen.queryByText(DEEP.writing)).toBeNull();
  expect(screen.getByText(/taxes the ferries/)).toBeInTheDocument();
  rerender(ask([...deep.head, ...deep.sec0, ...deep.sec1.slice(0, 2)]));
  expect(screen.getByRole("heading", { name: "The guild" })).toBeInTheDocument();
  expect(screen.getAllByText(DEEP.writing)).toHaveLength(1);
});

test("a finished section's citation chips open its passage before the answer lands", () => {
  view([...deep.head, ...deep.sec0]);
  fireEvent.click(screen.getByRole("button", { name: "[1]" }));
  expect(screen.getByText("council rules the harbour").tagName).toBe("MARK");
});

test("the contents list links to real anchors for every section that has started", () => {
  const { container } = view([...deep.head, ...deep.sec0, deep.sec1[0], deep.sec1[1]]);
  const nav = screen.getByRole("navigation", { name: DEEP.contents });
  const link = within(nav).getByRole("link", { name: "The council" });
  expect(link).toHaveAttribute("href", "#answer-section-0");
  expect(container.querySelector("#answer-section-0")).not.toBeNull();
  expect(within(nav).getByRole("link", { name: "The guild" })).toHaveAttribute("href", "#answer-section-1");
  fireEvent.click(link);
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "The council" }));
});

test("a section not yet started is listed but not linked", () => {
  view(deep.head);
  const nav = screen.getByRole("navigation", { name: DEEP.contents });
  expect(within(nav).getByText("The guild")).toBeInTheDocument();
  expect(within(nav).queryAllByRole("link")).toHaveLength(0);
});

test("the overview arrives last but renders above the contents and the sections", () => {
  const { container } = view(deep.full);
  const order = [...container.querySelectorAll(".sectioned > *")].map((n) => n.className.split(" ")[0]);
  expect(order.slice(0, 2)).toEqual(["overview", "contents"]);
  expect(order.slice(2)).toEqual(["answer-section", "answer-section", "answer-section"]);
  expect(screen.getByRole("heading", { name: DEEP.overview })).toBeInTheDocument();
  expect(screen.getByText("A council and a guild run the harbour.")).toBeInTheDocument();
});

test("no overview heading until the overview arrives", () => {
  view([...deep.head, ...deep.sec0]);
  expect(screen.queryByRole("heading", { name: DEEP.overview })).toBeNull();
});

test("the close shows a reading, the other side and a limit, each labelled", () => {
  const { container } = view(deep.full);
  const close = container.querySelector("#answer-section-2")!;
  expect(within(close as HTMLElement).getByRole("heading", { name: "Reading, counter-case and limits" })).toBeInTheDocument();
  expect([...close.querySelectorAll(".kind-label")].map((n) => n.textContent)).toEqual([UI.kindLabel.reading, UI.kindLabel.counter, UI.kindLabel.limit]);
  expect(close.querySelector(".sentence.reading .cite")).not.toBeNull();
  expect(close.querySelector("p.prose.limit")?.textContent).toContain("who founded the council");
});

test("a skipped section shows why, and a failed one says so", () => {
  const skipped = { type: "section", t: 3, index: 1, state: "skipped", kind: "section", heading: "The guild", reason: "budget" } as AskEvent;
  const failed = { ...deep.done(2, "Trade", [], [], null, [], { style: "failed", note: "the section could not be written" }) } as AskEvent;
  view([...deep.head, ...deep.sec0, skipped, failed]);
  const guild = document.querySelector("#answer-section-1") as HTMLElement;
  expect(within(guild).getByRole("heading", { name: "The guild" })).toBeInTheDocument();
  expect(guild).toHaveTextContent(DEEP.skipped.budget);
  expect(document.querySelector("#answer-section-2")).toHaveTextContent(DEEP.failed);
});

test("a reserve skip has its own note", () => {
  view([...deep.head, { type: "section", t: 3, index: 0, state: "skipped", kind: "section", heading: "The council", reason: "reserve" } as AskEvent]);
  expect(screen.getByText(DEEP.skipped.reserve)).toBeInTheDocument();
});

test("a section that fell back to its claims shows them with the note, still with chips", () => {
  const fell = deep.done(0, "The council", [], deep.claims.slice(0, 2), [1, 2], [], { style: "claims", text: "x" });
  view([...deep.head, deep.sec0[1], fell]);
  const sec = document.querySelector("#answer-section-0") as HTMLElement;
  expect(sec).toHaveTextContent(DEEP.claimsOnly);
  expect(within(sec).getByText("The council rules the harbour.")).toBeInTheDocument();
  expect(within(sec).getByText("The council taxes the ferries.")).toBeInTheDocument();
});

test("an empty section is left out of the page and the contents", () => {
  const empty = deep.done(1, "The guild", [], [], null, [], { style: "empty" });
  view([...deep.head, ...deep.sec0, deep.sec1[1], empty]);
  expect(screen.queryByRole("heading", { name: "The guild" })).toBeNull();
  expect(within(screen.getByRole("navigation", { name: DEEP.contents })).queryByText("The guild")).toBeNull();
});

test("the how panel shows the request count, and models only with internals", () => {
  const { unmount } = view(deep.full, true);
  expect(screen.getByText(DEEP.requests)).toBeInTheDocument();
  expect(screen.getByText(DEEP.requestCount(11))).toBeInTheDocument();
  unmount();
  view(deep.full, false);
  expect(screen.queryByText("Models")).toBeNull();
});

test("an answer with no section events still renders its sections from the result", () => {
  view([deep.tail[deep.tail.length - 1]]);
  expect(screen.getByRole("heading", { name: "The guild" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "The guild" })).toBeInTheDocument();
});

test("a deep ask that fell back to the ordinary path renders the ordinary answer", () => {
  const final = deep.tail[deep.tail.length - 1];
  if (final.type !== "answer") throw new Error("fixture");
  const result = { ...final.result, answer_style: "prose" as const, sections: [], overview: [], requests: 0, prose: deep.s2 };
  view([deep.head[0], deep.head[3], { type: "answer", t: 2, result }]);
  expect(screen.queryByRole("navigation", { name: DEEP.contents })).toBeNull();
  expect(document.querySelector("p.prose")).toHaveTextContent("The guild trains the pilots.");
  expect(screen.getByText(/^Evidence \(/)).toBeInTheDocument();
});

test("a plan whose sections all came back empty, then an ordinary answer, renders the ordinary prose", () => {
  const final = deep.tail[deep.tail.length - 1];
  if (final.type !== "answer") throw new Error("fixture");
  const result = { ...final.result, answer_style: "prose" as const, sections: [], overview: [], requests: 0, prose: deep.s2 };
  const empties = [0, 1].map((i) => deep.done(i, `S${i}`, [], [], null, [], { style: "empty" }));
  view([...deep.head, ...empties, { type: "answer", t: 2, result }]);
  expect(screen.queryByRole("navigation", { name: DEEP.contents })).toBeNull();
  expect(document.querySelector("p.prose")).toHaveTextContent("The guild trains the pilots.");
  expect(screen.getByText(/^Evidence \(/)).toBeInTheDocument();
});

test("a plan alone, then an ordinary answer, renders the ordinary prose", () => {
  const final = deep.tail[deep.tail.length - 1];
  if (final.type !== "answer") throw new Error("fixture");
  const result = { ...final.result, answer_style: "prose" as const, sections: [], overview: [], requests: 0, prose: deep.s2 };
  view([...deep.head, { type: "answer", t: 2, result }]);
  expect(document.querySelector("p.prose")).toHaveTextContent("The guild trains the pilots.");
});

test("while streaming, a plan whose sections all came back empty shows no contents list to strand the reader", () => {
  const empties = [0, 1].map((i) => deep.done(i, `S${i}`, [], [], null, [], { style: "empty" }));
  const { unmount } = view([...deep.head, ...empties]);
  expect(document.querySelector(".sectioned")).toBeNull();
  unmount();
  view([...deep.head]);
  expect(screen.getAllByRole("navigation", { name: DEEP.contents }).length).toBe(1); // a plan alone still shows its contents
});

test("the steps list has a planner row and one row per section", () => {
  view([...deep.head, ...deep.sec0, deep.sec1[0]]);
  const steps = document.querySelector("ol.steps") as HTMLElement;
  expect(within(steps).getByText(STEPS_DEEP.plan)).toBeInTheDocument();
  expect(within(steps).getByText("Section 1: The council")).toBeInTheDocument();
  const second = within(steps).getByText("Section 2: The guild").closest("li")!;
  expect(second).toHaveClass("now");
  expect(within(steps).getByText("Section 1: The council").closest("li")).toHaveClass("done");
  expect(within(steps).queryByText("Searching the graph")).toBeNull();   // rows that never ran are left out
});
