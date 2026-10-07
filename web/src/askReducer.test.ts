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
