import single from "./fixtures/single-part.json";
import two from "./fixtures/two-part.json";
import { budget, empty, interrupted } from "./fixtures/synthetic";
import { askReducer, initialState } from "./askReducer";
import type { AskEvent } from "./types";

const run = (events: AskEvent[]) => events.reduce(askReducer, initialState("q"));

test("a recorded single-part answer ends done with every step settled", () => {
  const s = run(single as AskEvent[]);
  expect(s.status).toBe("done");
  expect(s.result?.claims.length).toBeGreaterThan(0);
  expect(s.steps.every((x) => x.state === "done" || x.state === "skipped")).toBe(true);
  expect(s.seeds[0].length).toBeGreaterThan(0);
});

test("a two-part answer keeps seeds and edges per part", () => {
  const s = run(two as AskEvent[]);
  expect(s.subQuestions).toHaveLength(2);
  expect(s.seeds).toHaveLength(2);
  expect(s.edges.length).toBeGreaterThanOrEqual(1);
  expect(s.evidence?.chunkIds.length).toBeGreaterThan(0);
});

test("seeds is not done until the last part reports", () => {
  let s = initialState("q");
  s = askReducer(s, { type: "parts", t: 0, sub_questions: ["a", "b"] });
  s = askReducer(s, { type: "step", t: 0, name: "seeds", state: "start", part: 0, detail: null });
  s = askReducer(s, { type: "step", t: 0, name: "seeds", state: "done", part: 0, detail: "3 found" });
  expect(s.steps.find((x) => x.name === "seeds")!.state).toBe("running");
  s = askReducer(s, { type: "step", t: 0, name: "seeds", state: "done", part: 1, detail: "2 found" });
  expect(s.steps.find((x) => x.name === "seeds")!.state).toBe("done");
});

test("split edges events append", () => {
  const edge = (c: string) => ({ subject: "a", predicate: "P", predicate_raw: "p", object: "b", evidence: "", confidence: 1,
    book_id: "b1", chapter_seq: 1, chunk_id: c, subject_id: "a", object_id: "b" });
  let s = initialState("q");
  s = askReducer(s, { type: "edges", t: 0, part: 0, template: "neighbours", total: 3, edges: [edge("1"), edge("2")] });
  s = askReducer(s, { type: "edges", t: 0, part: 0, template: "neighbours", total: 3, edges: [edge("3")] });
  expect(s.edges[0].edges.map((e) => e.chunk_id)).toEqual(["1", "2", "3"]);
});

test("an interrupted stream keeps what arrived", () => {
  const s = run(interrupted);
  expect(s.status).toBe("interrupted");
  expect(s.subQuestions).toEqual(["q"]);
  expect(s.steps.find((x) => x.name === "split")!.state).toBe("done");
});

test("errors and empty answers", () => {
  expect(run(budget).error?.code).toBe("budget_exhausted");
  const e = run(empty);
  expect(e.status).toBe("done");
  expect(e.steps.every((x) => x.state === "skipped")).toBe(true);
});

test("elapsed never goes backwards", () => {
  expect(run(interrupted).elapsed).toBe(0.5);
});

test("compose and check run at the same time", () => {
  const ev = (name: any, state: any, t: number) => ({ type: "step", t, name, state, part: null, detail: null }) as AskEvent;
  let s = initialState("q");
  for (const e of [ev("write", "start", 1), ev("write", "done", 2), ev("compose", "start", 3), ev("check", "start", 4)]) s = askReducer(s, e);
  const st = (n: string) => s.steps.find((x) => x.name === n)!.state;
  expect(st("compose")).toBe("running");
  expect(st("check")).toBe("running");
  expect(s.steps.map((x) => x.name).indexOf("compose")).toBe(s.steps.map((x) => x.name).indexOf("write") + 1);
  s = askReducer(s, ev("check", "done", 5));
  s = askReducer(s, { ...ev("compose", "done", 6), detail: "3 sentences" } as AskEvent);
  expect(st("compose")).toBe("done");
});

import * as deep from "./fixtures/deep";

test("a deep answer builds the plan, sections, overview and live citations as events arrive", () => {
  let s = initialState("q");
  for (const e of [...deep.head, ...deep.sec0, deep.sec1[0], deep.sec1[1]]) s = askReducer(s, e);
  expect(s.plan?.sections.map((x) => x.heading)).toEqual(["The council", "The guild"]);
  expect(s.sections.map((x) => [x.index, x.state])).toEqual([[0, "done"], [1, "writing"]]);
  expect(s.liveClaims.map((c) => c.text)).toEqual(deep.claims.slice(0, 2).map((c) => c.text));
  expect(s.livePassages.map((p) => p.n)).toEqual([1]);
  expect(s.overview).toBeNull();
  expect(s.deepSteps.map((d) => [d.kind, d.part, d.state])).toEqual([["plan", null, "done"], ["section", 0, "done"], ["section", 1, "running"]]);
  expect(s.steps.find((x) => x.name === "split")!.state).toBe("done");
  for (const e of [deep.sec1[2], deep.sec1[3], ...deep.tail]) s = askReducer(s, e);
  expect(s.overview?.length).toBe(1);
  expect(s.liveClaims).toHaveLength(3);
  expect(s.sections.map((x) => x.kind)).toEqual(["section", "section", "close"]);
  expect(s.status).toBe("done");
});

test("a skipped section is kept with its reason; an interrupted stream fails the one being written", () => {
  let s = initialState("q");
  s = askReducer(s, deep.start(0, "A"));
  s = askReducer(s, { type: "section", t: 1, index: 1, state: "skipped", kind: "section", heading: "B", reason: "reserve" });
  expect(s.sections[1]).toMatchObject({ state: "skipped", reason: "reserve" });
  s = askReducer(s, { type: "error", t: 2, error: { code: "interrupted", message: "" } });
  expect(s.sections[0].state).toBe("failed");
});

test("the parts event keeps lens_of and the plan's questions", () => {
  const s = askReducer(initialState("q"), { type: "parts", t: 0, sub_questions: ["l1", "l2", "f"], lens_of: [0, 0, null], plan_questions: ["P"] });
  expect(s.lensOf).toEqual([0, 0, null]);
  expect(s.planQuestions).toEqual(["P"]);
  expect(askReducer(initialState("q"), { type: "parts", t: 0, sub_questions: ["a"] }).lensOf).toEqual([]);
});
