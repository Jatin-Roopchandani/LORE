import { bigBookNote, finishPhrase, freeTierLine, roughParts } from "./freeTier";
import type { FreeTierPlan } from "./types";

const local = (y: number, m: number, d: number, h: number, min = 0) => new Date(y, m - 1, d, h, min).getTime();
const tier = (finish: number, extra: Partial<FreeTierPlan> = {}): FreeTierPlan => ({
  requests_left: 1100, today_left: 600, per_day: 1000, days: 1, finish_at: new Date(finish).toISOString(),
  runs_out_at_part: 612, rough: false, ...extra });

test("the Library's line: work, where the free limit runs out, and the finish in the reader's time", () => {
  const now = local(2026, 11, 17, 20);
  expect(freeTierLine(tier(local(2026, 11, 18, 9, 50)), 7200, now)).toBe(
    "About 2 h of work. Gemini’s free daily limit runs out about 600 parts in, so it should finish tomorrow around"
    + " 10:00 (your time). LORE continues automatically.");
});

test("honest rounding: a rough guess says the part of the day; further out, days", () => {
  const now = local(2026, 11, 17, 20);
  expect(finishPhrase(new Date(local(2026, 11, 18, 9)).toISOString(), now, true)).toBe("tomorrow morning");
  expect(finishPhrase(new Date(local(2026, 11, 20, 9)).toISOString(), now, false)).toBe("in about 3 days");
  expect(roughParts(612)).toBe(600);
  expect(roughParts(1057)).toBe(1050);
  expect(roughParts(40)).toBe(40);
});

test("past extraction the line says only that the limit runs out before it's done", () => {
  const now = local(2026, 11, 17, 20);
  expect(freeTierLine(tier(local(2026, 11, 18, 9), { runs_out_at_part: null }), null, now)).toBe(
    "Gemini’s free daily limit runs out before it’s done, so it should finish tomorrow around 09:00 (your time)."
    + " LORE continues automatically.");
});

test("the upload's heads-up for a big book on the free tier", () => {
  const now = local(2026, 11, 17, 20);
  const note = { parts: 1057, days: 2, finish_at: new Date(local(2026, 11, 19, 10)).toISOString(), rough: true, sample: true };
  expect(bigBookNote(note, now)).toBe("This is a big book (about 1,050 parts). On Gemini\u2019s free tier it should be ready"
    + " in about 2 days; LORE keeps going on its own, and you can explore the sample book meanwhile.");
  expect(bigBookNote({ ...note, sample: false, finish_at: new Date(local(2026, 11, 18, 10)).toISOString(), rough: false }, now))
    .toBe("This is a big book (about 1,050 parts). On Gemini\u2019s free tier it should be ready tomorrow around 10:00"
      + " (your time); LORE keeps going on its own.");
  expect(bigBookNote(undefined, now)).toBeNull();
});
