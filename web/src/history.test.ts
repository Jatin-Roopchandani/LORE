import { addToHistory, answerOf, HISTORY_KEY, loadHistory, saveHistory, type HistoryItem } from "./history";
import type { AskEvent, SearchResult } from "./types";

const item = (id: string, events: AskEvent[] = []): HistoryItem => ({ id, question: id, events, askedAt: "2026-10-07T00:00:00Z" });

class MemoryStorage { m = new Map<string, string>(); getItem(k: string) { return this.m.get(k) ?? null; }
  setItem(k: string, v: string) { this.m.set(k, v); } removeItem(k: string) { this.m.delete(k); } clear() { this.m.clear(); }
  key() { return null; } get length() { return this.m.size; } }

test("round-trips newest first and caps at 50", () => {
  const s = new MemoryStorage() as unknown as Storage;
  let items: HistoryItem[] = [];
  for (let i = 0; i < 55; i++) items = addToHistory(items, item(String(i)));
  expect(saveHistory(items, s)).toBe(50);
  const back = loadHistory(s);
  expect(back).toHaveLength(50);
  expect(back[0].id).toBe("54");
});

test("survives a throwing storage", () => {
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("full"); } } as unknown as Storage;
  expect(loadHistory(broken)).toEqual([]);
  expect(saveHistory([item("a")], broken)).toBe(false);
});

test("ignores corrupt data", () => {
  const s = new MemoryStorage() as unknown as Storage;
  s.setItem(HISTORY_KEY, "{not json");
  expect(loadHistory(s)).toEqual([]);
  expect(loadHistory(null)).toEqual([]);
});

const result = { answer: "x".repeat(200), mode: "graph", template: null, note: null, model: null, selection_model: null,
  sub_questions: ["q"], claims: [], passages: [], edges: [], seeds: [] } as SearchResult;
const answered = (id: string) => item(id, [{ type: "answer", t: 1, result }]);

class QuotaStorage extends MemoryStorage {
  constructor(private limit: number) { super(); }
  setItem(k: string, v: string) {
    if (v.length > this.limit) throw new DOMException("full", "QuotaExceededError");
    super.setItem(k, v);
  }
}

test("a full store drops the oldest items until the newest fit", () => {
  const one = JSON.stringify([answered("0")]).length;
  const s = new QuotaStorage(one * 3 + 10) as unknown as Storage;
  let items: HistoryItem[] = [];
  for (let i = 0; i < 10; i++) items = addToHistory(items, answered(String(i)));
  expect(saveHistory(items, s)).toBe(3);
  expect(loadHistory(s).map((x) => x.id)).toEqual(["9", "8", "7"]);
  // the next answer still saves
  items = addToHistory(items, answered("10"));
  expect(saveHistory(items, s)).toBe(3);
  expect(loadHistory(s)[0].id).toBe("10");
});

test("a store too small for even one item ends up with an empty list", () => {
  const s = new QuotaStorage(5) as unknown as Storage;
  expect(saveHistory([answered("a")], s)).toBe(0);
  expect(loadHistory(s)).toEqual([]);
  expect(saveHistory([answered("a")], new QuotaStorage(1) as unknown as Storage)).toBe(false);
});

test("an item round-trips without a duplicate answer; old items with one still read", () => {
  const s = new MemoryStorage() as unknown as Storage;
  saveHistory([{ ...answered("a"), answer: result }], s);
  const raw = s.getItem(HISTORY_KEY)!;
  expect(raw.split("x".repeat(200)).length - 1).toBe(1);
  const [back] = loadHistory(s);
  expect(back).not.toHaveProperty("answer");
  expect(answerOf(back)).toEqual(result);
  expect(answerOf({ ...item("old"), answer: result })).toEqual(result);
  expect(answerOf(item("none"))).toBeNull();
});
