import type { AskEvent, SearchResult } from "./types";

// `answer` is not stored: the `answer` event already carries it, and the duplicate
// doubled every item. Items saved before that may still have it; answerOf reads either.
// `world`: the world the question was asked in (an id as a string, or "all"); absent on items saved before worlds.
export interface HistoryItem { id: string; question: string; events: AskEvent[]; answer?: SearchResult | null; askedAt: string; world?: string }
export const HISTORY_KEY = "lore-history-v1";
const MAX_ITEMS = 50;

function defaultStorage(): Storage | null {
  try { return window.localStorage; } catch { return null; }
}

export function answerOf(item: HistoryItem): SearchResult | null {
  const e = item.events.find((x) => x.type === "answer");
  return e && e.type === "answer" ? e.result : item.answer ?? null;
}

const slim = ({ answer: _drop, ...rest }: HistoryItem): HistoryItem => rest;

export function loadHistory(storage: Storage | null = defaultStorage()): HistoryItem[] {
  if (!storage) return [];
  try {
    const parsed = JSON.parse(storage.getItem(HISTORY_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.slice(0, MAX_ITEMS) : [];
  } catch {
    return [];
  }
}

const isQuota = (e: unknown) => e instanceof DOMException
  ? e.name === "QuotaExceededError" || e.name === "NS_ERROR_DOM_QUOTA_REACHED" || e.code === 22 || e.code === 1014
  : (e as { name?: string } | null)?.name === "QuotaExceededError";

// On a full store the oldest items go first, one at a time, until the rest fit.
// Returns how many items were saved, or false when storage refused outright.
export function saveHistory(items: HistoryItem[], storage: Storage | null = defaultStorage()): number | false {
  if (!storage) return false;
  let keep = items.slice(0, MAX_ITEMS).map(slim);
  while (true) {
    try {
      storage.setItem(HISTORY_KEY, JSON.stringify(keep));
      return keep.length;
    } catch (e) {
      if (!isQuota(e) || keep.length === 0) return false;
      keep = keep.slice(0, -1);
    }
  }
}

export function addToHistory(items: HistoryItem[], item: HistoryItem): HistoryItem[] {
  return [item, ...items.filter((x) => x.id !== item.id)].slice(0, MAX_ITEMS);
}
