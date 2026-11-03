import type { HistoryItem } from "./history";
import { HISTORY } from "./messages";

export type GroupKey = "today" | "yesterday" | "week" | "earlier";
export const GROUP_ORDER: GroupKey[] = ["today", "yesterday", "week", "earlier"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAY = 86400000;
const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

/** Whole calendar days between `at` and `now` (0 = today); null when the date can't be read. */
function daysAgo(askedAt: string, now: Date): number | null {
  const t = new Date(askedAt);
  if (Number.isNaN(t.getTime())) return null;
  return Math.round((startOfDay(now) - startOfDay(t)) / DAY);
}

/** Today, Yesterday, This week (up to a week ago) or Earlier; an unreadable date is Earlier. */
export function groupOf(askedAt: string, now: Date): GroupKey {
  const n = daysAgo(askedAt, now);
  if (n === null || n < 0) return n === null ? "earlier" : "today";
  return n === 0 ? "today" : n === 1 ? "yesterday" : n < 7 ? "week" : "earlier";
}

/** "10:42" for today, "Yesterday", otherwise the date ("3 Nov", with the year when it isn't this one). */
export function timeLabel(askedAt: string, now: Date): string {
  const n = daysAgo(askedAt, now);
  if (n === null) return "";
  const t = new Date(askedAt);
  if (n <= 0) return `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
  if (n === 1) return HISTORY.yesterday;
  return `${t.getDate()} ${MONTHS[t.getMonth()]}${t.getFullYear() === now.getFullYear() ? "" : ` ${t.getFullYear()}`}`;
}

/** The items whose question contains `query` (ignoring case); all of them for a blank query. */
export function filterHistory(items: HistoryItem[], query: string): HistoryItem[] {
  const q = query.trim().toLowerCase();
  return q ? items.filter((h) => h.question.toLowerCase().includes(q)) : items;
}

/** Items under Today, Yesterday, This week and Earlier, newest first; empty groups left out. */
export function groupHistory(items: HistoryItem[], now: Date): { key: GroupKey; items: HistoryItem[] }[] {
  const when = (h: HistoryItem) => { const t = new Date(h.askedAt).getTime(); return Number.isNaN(t) ? 0 : t; };
  const sorted = [...items].sort((a, b) => when(b) - when(a));
  return GROUP_ORDER.map((key) => ({ key, items: sorted.filter((h) => groupOf(h.askedAt, now) === key) })).filter((g) => g.items.length > 0);
}

/** A deep answer: sectioned, or planned in sections. Items saved before deep answers are never deep. */
export function isDeep(item: HistoryItem): boolean {
  return item.events.some((e) => (e.type === "answer" && e.result.answer_style === "sectioned") || e.type === "plan");
}
