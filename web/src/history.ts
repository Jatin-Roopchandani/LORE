import type { AskEvent, ChatMessage, ChatSummary, NewChatBody, SearchResult } from "./types";

// `answer` is not stored: the `answer` event already carries it, and the duplicate
// doubled every item. Items saved before that may still have it; answerOf reads either.
// `world`: the world the question was asked in (an id as a string, or "all"); absent on items saved before worlds.
// Server-side history (release step 4): the server is the source of truth and this store is a cache for the first
// paint. `synced`: the item is a chat on the server (its `id` is the chat id). An item the list brought has no
// `events` until it is opened (`deep` says what the drawer needs meanwhile). Items without `synced` were saved by
// this browser before the server kept history: they are what the one-time import offers.
export interface HistoryItem { id: string; question: string; events: AskEvent[]; answer?: SearchResult | null; askedAt: string; world?: string;
  synced?: boolean; deep?: boolean }
export const HISTORY_KEY = "lore-history-v1";
// Items saved only in this browser, kept aside for the import once the server's list has replaced the cache.
export const LEGACY_KEY = "lore-history-legacy-v1";
// Set once the import was done or declined, so it is offered only once.
export const IMPORTED_KEY = "lore-history-imported-v1";
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

// --- server-side history -------------------------------------------------------------------------------------------

/** The events a stored message replays: its steps, then its answer (or the "too large to keep" error). */
export function eventsOf(m: ChatMessage): AskEvent[] {
  const a = m.answer;
  const last = (a?.steps ?? []).reduce((t, e) => Math.max(t, (e as { t?: number }).t ?? 0), 0);
  const head: AskEvent[] = m.request_id ? [{ type: "request", t: 0, request_id: m.request_id }] : [];
  if (!a || a.truncated || !a.result) return [...head, { type: "error", t: last, error: { code: "too_large", message: "" } }];
  return [...head, ...(a.steps ?? []), { type: "answer", t: last, result: a.result, message_id: m.id }];
}

/** A chat from the server's list as a history item, named by its title (its first question); the cached item's
 *  events (its latest answer's) are kept when it has them. */
export function itemFromChat(c: ChatSummary, cached?: HistoryItem): HistoryItem {
  const m = c.latest;
  return { id: c.id, question: c.title || m?.question || "", askedAt: m?.created_at ?? c.updated_at,
    world: m?.world ?? c.world ?? undefined, events: cached?.events ?? [], synced: true, deep: m?.summary?.deep ?? cached?.deep };
}

/** One question and answer of a chat, as a thread draws it: `key` is stable (the message id when the server has it). */
export interface ThreadEntry { key: string; question: string; standalone: string | null; events: AskEvent[] }

/** A chat's messages as thread entries, oldest first. */
export function entriesOf(messages: ChatMessage[]): ThreadEntry[] {
  return messages.map((m) => {
    const events = eventsOf(m);
    const understood = events.find((e) => e.type === "understood");
    return { key: `m${m.id}`, question: m.question, events,
      standalone: m.standalone ?? (understood && understood.type === "understood" ? understood.standalone : null) };
  });
}

/** What importing a browser-only item sends: idempotent by its own id. */
export function importBody(item: HistoryItem): NewChatBody {
  const result = answerOf(item);
  const steps = item.events.filter((e) => e.type !== "answer" && e.type !== "error" && e.type !== "request");
  return { client_id: item.id, title: item.question, created_at: item.askedAt, ...(item.world ? { world: item.world } : {}),
    messages: [{ question: item.question, ...(item.world ? { world: item.world } : {}), ...(result ? { answer: { result, steps } } : {}) }] };
}

function readList(key: string, storage: Storage | null): HistoryItem[] {
  if (!storage) return [];
  try {
    const parsed = JSON.parse(storage.getItem(key) ?? "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/** Browser-only items waiting for the import (from the legacy store, and any still in the cache). */
export function loadLegacy(storage: Storage | null = defaultStorage()): HistoryItem[] {
  const seen = new Set<string>();
  return [...readList(LEGACY_KEY, storage), ...readList(HISTORY_KEY, storage).filter((x) => !x.synced)]
    .filter((x) => x && typeof x.id === "string" && !seen.has(x.id) && seen.add(x.id));
}

export function keepLegacy(items: HistoryItem[], storage: Storage | null = defaultStorage()): void {
  if (!storage) return;
  try { storage.setItem(LEGACY_KEY, JSON.stringify(items.map(slim))); } catch { /* full or blocked: the cache still has them */ }
}

export function importDone(storage: Storage | null = defaultStorage()): boolean {
  try { return storage?.getItem(IMPORTED_KEY) === "1"; } catch { return false; }
}

export function markImportDone(storage: Storage | null = defaultStorage()): void {
  try { storage?.setItem(IMPORTED_KEY, "1"); storage?.removeItem(LEGACY_KEY); } catch { /* blocked: it may be offered again */ }
}
