import type { HistoryItem } from "./history";
import { filterHistory, groupHistory, groupOf, isDeep, timeLabel } from "./historyView";

const now = new Date(2026, 10, 3, 15, 30); // Tue 3 Nov 2026, 15:30
const at = (days: number, h = 10, m = 42) => new Date(2026, 10, 3 - days, h, m).toISOString();
const item = (id: string, question: string, askedAt: string, extra: Partial<HistoryItem> = {}): HistoryItem => ({ id, question, askedAt, events: [], ...extra });

test("an item is Today, Yesterday, This week (2 to 6 days back) or Earlier by calendar day", () => {
  expect(groupOf(at(0, 0, 5), now)).toBe("today");
  expect(groupOf(at(1, 23, 59), now)).toBe("yesterday");
  expect(groupOf(at(2), now)).toBe("week");
  expect(groupOf(at(6), now)).toBe("week");
  expect(groupOf(at(7), now)).toBe("earlier");
  expect(groupOf("not a date", now)).toBe("earlier");
});

test("groups come in order, newest first inside each, and empty groups are left out", () => {
  const items = [item("a", "old", at(30)), item("b", "morning", at(0, 9, 0)), item("c", "evening", at(0, 14, 0)), item("d", "last night", at(1)), item("e", "midweek", at(3))];
  expect(groupHistory(items, now).map((g) => [g.key, g.items.map((i) => i.id)])).toEqual([
    ["today", ["c", "b"]], ["yesterday", ["d"]], ["week", ["e"]], ["earlier", ["a"]]]);
  expect(groupHistory([item("x", "q", at(0))], now).map((g) => g.key)).toEqual(["today"]);
});

test("the time reads as a clock today, Yesterday, then a date (with the year when it isn't this one)", () => {
  expect(timeLabel(at(0, 9, 5), now)).toBe("09:05");
  expect(timeLabel(at(0, 10, 42), now)).toBe("10:42");
  expect(timeLabel(at(1), now)).toBe("Yesterday");
  expect(timeLabel(at(2), now)).toBe("1 Nov");
  expect(timeLabel(new Date(2025, 11, 25, 8, 0).toISOString(), now)).toBe("25 Dec 2025");
  expect(timeLabel("garbage", now)).toBe("");
});

test("search filters by question text, ignoring case and blanks", () => {
  const items = [item("a", "Who is Zed Quill?", at(0)), item("b", "Where is the marsh?", at(0))];
  expect(filterHistory(items, "ZED").map((i) => i.id)).toEqual(["a"]);
  expect(filterHistory(items, "  ").map((i) => i.id)).toEqual(["a", "b"]);
  expect(filterHistory(items, "nothing")).toEqual([]);
});

test("a deep answer is one that was sectioned or planned; an old item with no events is not", () => {
  const result = { answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null, sub_questions: [], claims: [], passages: [], edges: [], seeds: [] };
  expect(isDeep(item("a", "q", at(0), { events: [{ type: "answer", t: 0, result: { ...result, answer_style: "sectioned" } }] }))).toBe(true);
  expect(isDeep(item("b", "q", at(0), { events: [{ type: "plan", t: 0, scope: "x", candidates: 1, sections: [] }] }))).toBe(true);
  expect(isDeep(item("c", "q", at(0), { events: [{ type: "answer", t: 0, result: { ...result, answer_style: "prose" } }] }))).toBe(false);
  expect(isDeep({ id: "d", question: "q", askedAt: at(0), events: [] })).toBe(false);
});
