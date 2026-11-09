import { POSITION } from "./messages";
import { chapterOptions, limits, pickedSeq, summaryLine, topBarLabel } from "./progress";
import type { PositionBook, ProgressState, ReadingPositionData } from "./types";

const b = (id: string, state: ProgressState = "finished", progress_chapter = 0): PositionBook => ({
  id, title: id.toUpperCase(), series: null, series_index: null, chapters: 3, order: "unknown", state, progress_chapter,
  sections: [{ seq: 1, number: 1, heading: "Front" }, { seq: 3, number: 2, heading: null }, { seq: 4, number: 3, heading: "End" }] });
const w = (...books: PositionBook[]): ReadingPositionData =>
  ({ world: { id: 1, name: "W" }, enabled: false, book_id: null, chapter: 0, read_books: [], books });

test("the line under the list", () => {
  expect(summaryLine([b("a"), b("b")])).toBe(POSITION.usesAll);
  expect(summaryLine([])).toBe(POSITION.usesAll);
  expect(summaryLine([b("a", "reading", 3), b("b")])).toBe("Answers, graph and profiles stop at chapter 2 of A.");
  expect(summaryLine([b("a", "reading", 3), b("b", "reading", 4)])).toBe("Answers, graph and profiles stop at chapter 2 of A and chapter 3 of B.");
  expect(summaryLine([b("a", "reading", 0)])).toBe("Answers, graph and profiles stop at the start of A.");
  expect(summaryLine([b("a"), b("b", "not_started"), b("c", "not_started")])).toBe("Answers, graph and profiles leave out B and C.");
  expect(summaryLine([b("a", "not_started")])).toBe(POSITION.usesNone);
});

test("the top-bar label over one world or several", () => {
  expect(topBarLabel([w(b("a"), b("b"))])).toBe("All read");
  expect(topBarLabel([w(b("a"), b("b", "not_started")), w(b("c"))])).toBe("2 of 3 read");
  expect(topBarLabel([w(b("a"), b("b", "reading", 4))])).toBe("Ch. 3 · B");
  expect(topBarLabel([w(b("a", "reading", 1)), w(b("c", "reading", 3))])).toBe("Ch. 1 · A +1");
});

test("chapter options and the one shown", () => {
  expect(chapterOptions(b("a", "reading", 3)).map((o) => o.label)).toEqual(["1 · Front", "2", "3 · End"]);
  expect(pickedSeq(b("a", "reading", 3))).toBe(3);
  // a section between story chapters shows the chapter it falls in
  expect(pickedSeq({ ...b("a", "reading", 2) })).toBe(1);
  // a stored position before chapter 1 is offered as such
  expect(chapterOptions(b("a", "reading", 0))[0]).toEqual({ seq: 0, label: "Before chapter 1" });
});

test("limits: only when something is not finished", () => {
  expect(limits([w(b("a"))])).toBe(false);
  expect(limits([w(b("a")), w(b("b", "not_started"))])).toBe(true);
});
