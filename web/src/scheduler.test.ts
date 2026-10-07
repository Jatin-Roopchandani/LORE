import { createScheduler, splitForStagger } from "./scheduler";
import type { AskEvent } from "./types";

const seeds: AskEvent = { type: "seeds", t: 0, part: 0, entities: [1, 2, 3].map((i) => ({ id: String(i), name: String(i), kind: "Character", books: [], score: 0 })) };

test("splits seeds one per entity", () => {
  expect(splitForStagger(seeds, 1)).toHaveLength(3);
});

test("reduced motion dispatches everything at once, unsplit", () => {
  const out: AskEvent[] = [];
  const s = createScheduler((e) => out.push(e), { reduceMotion: true });
  s.push(seeds); s.push({ type: "parts", t: 0, sub_questions: [] });
  expect(out.map((e) => e.type)).toEqual(["seeds", "parts"]);
});

test("staggered pieces keep event order", async () => {
  vi.useFakeTimers();
  const out: string[] = [];
  const s = createScheduler((e) => out.push(e.type === "seeds" ? `seed${e.entities[0].id}` : e.type), { reduceMotion: false });
  s.push(seeds); s.push({ type: "parts", t: 0, sub_questions: [] });
  await vi.runAllTimersAsync();
  expect(out).toEqual(["seed1", "seed2", "seed3", "parts"]);
  vi.useRealTimers();
});

test("stop cancels pending pieces", async () => {
  vi.useFakeTimers();
  const out: AskEvent[] = [];
  const s = createScheduler((e) => out.push(e), { reduceMotion: false });
  s.push(seeds); s.stop();
  await vi.runAllTimersAsync();
  expect(out.length).toBeLessThan(3);
  vi.useRealTimers();
});

test("reduced motion: stop() blocks later pushes", () => {
  const out: AskEvent[] = [];
  const s = createScheduler((e) => out.push(e), { reduceMotion: true });
  s.push({ type: "parts", t: 0, sub_questions: [] }); s.stop(); s.push({ type: "parts", t: 0, sub_questions: [] });
  expect(out).toHaveLength(1);
});

test("with motion: stop() after one piece halts the rest and later pushes", async () => {
  vi.useFakeTimers();
  const out: AskEvent[] = [];
  const s = createScheduler((e) => out.push(e), { reduceMotion: false });
  s.push(seeds);
  await vi.advanceTimersByTimeAsync(0);
  expect(out).toHaveLength(1);
  s.stop();
  await vi.runAllTimersAsync();
  s.push({ type: "parts", t: 0, sub_questions: [] });
  await vi.runAllTimersAsync();
  expect(out).toHaveLength(1);
  vi.useRealTimers();
});

test("replay caps the wait between events at 600ms", async () => {
  vi.useFakeTimers();
  const times: number[] = [];
  const s = createScheduler(() => times.push(Date.now()), { reduceMotion: false, replay: true });
  s.push({ type: "parts", t: 0, sub_questions: [] }); s.push({ type: "parts", t: 5, sub_questions: [] });
  await vi.runAllTimersAsync();
  expect(times).toHaveLength(2);
  expect(times[1] - times[0]).toBe(600);
  vi.useRealTimers();
});
