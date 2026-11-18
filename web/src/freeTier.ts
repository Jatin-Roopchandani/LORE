import { LIBRARY } from "./messages";
import type { FreeTierPlan } from "./types";

const DAY_MS = 86400000;
const dayOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

/** "about 600": a round number, never false precision. */
export function roughParts(n: number): number {
  if (n < 100) return n;
  const step = n < 1000 ? 50 : 100;
  return Math.max(step, Math.round(n / step) * step);
}

/** When it should finish, in the reader's own calendar: "tomorrow around 10:00 (your time)", "tomorrow morning" for
 * a rough guess, "in about 3 days" further out. */
export function finishPhrase(finishAt: string, now: number, rough: boolean): string {
  const at = new Date(finishAt);
  const days = Math.round((dayOf(at) - dayOf(new Date(now))) / DAY_MS);
  if (days >= 2) return LIBRARY.finishInDays(days);
  // To the hour: the estimate is good to about that, not to the minute.
  const hour = new Date(at.getTime() + 30 * 60000);
  hour.setMinutes(0, 0, 0);
  const time = hour.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  if (days <= 0) return LIBRARY.finishToday(time);
  if (!rough) return LIBRARY.finishTomorrowAt(time);
  const h = at.getHours();
  return LIBRARY.finishTomorrow(h < 12 ? "morning" : h < 17 ? "afternoon" : "evening");
}

/** The Library's line for a book that won't finish on today's free requests. */
export function freeTierLine(tier: FreeTierPlan, secondsLeft: number | null, now: number): string {
  const work = secondsLeft != null ? LIBRARY.duration(secondsLeft) : null;
  const runsOut = tier.runs_out_at_part !== null ? roughParts(tier.runs_out_at_part) : null;
  return LIBRARY.freeTier(work, runsOut, finishPhrase(tier.finish_at, now, tier.rough));
}
