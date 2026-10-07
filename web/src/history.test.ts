import { addToHistory, HISTORY_KEY, loadHistory, saveHistory, type HistoryItem } from "./history";

const item = (id: string): HistoryItem => ({ id, question: id, events: [], answer: null, askedAt: "2026-10-07T00:00:00Z" });

class MemoryStorage { m = new Map<string, string>(); getItem(k: string) { return this.m.get(k) ?? null; }
  setItem(k: string, v: string) { this.m.set(k, v); } removeItem(k: string) { this.m.delete(k); } clear() { this.m.clear(); }
  key() { return null; } get length() { return this.m.size; } }

test("round-trips newest first and caps at 50", () => {
  const s = new MemoryStorage() as unknown as Storage;
  let items: HistoryItem[] = [];
  for (let i = 0; i < 55; i++) items = addToHistory(items, item(String(i)));
  expect(saveHistory(items, s)).toBe(true);
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
