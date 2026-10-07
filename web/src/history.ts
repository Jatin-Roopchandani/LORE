import type { AskEvent, SearchResult } from "./types";

export interface HistoryItem { id: string; question: string; events: AskEvent[]; answer: SearchResult | null; askedAt: string }
export const HISTORY_KEY = "lore-history-v1";
const MAX_ITEMS = 50;

function defaultStorage(): Storage | null {
  try { return window.localStorage; } catch { return null; }
}

export function loadHistory(storage: Storage | null = defaultStorage()): HistoryItem[] {
  if (!storage) return [];
  try {
    const parsed = JSON.parse(storage.getItem(HISTORY_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.slice(0, MAX_ITEMS) : [];
  } catch {
    return [];
  }
}

export function saveHistory(items: HistoryItem[], storage: Storage | null = defaultStorage()): boolean {
  if (!storage) return false;
  try {
    storage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, MAX_ITEMS)));
    return true;
  } catch {
    return false;
  }
}

export function addToHistory(items: HistoryItem[], item: HistoryItem): HistoryItem[] {
  return [item, ...items.filter((x) => x.id !== item.id)].slice(0, MAX_ITEMS);
}
