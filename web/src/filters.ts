// The graph filters: the kinds and books hidden in the graph pane, and whether an answer's "uncategorized"
// relationships are shown (hidden unless asked for). Remembered per browser.
export const FILTERS_KEY = "lore-graph-filters-v1";
export interface Filters { kinds: ReadonlySet<string>; books: ReadonlySet<string>; uncategorized?: boolean }
export const NO_FILTERS: Filters = { kinds: new Set(), books: new Set() };

function defaultStorage(): Storage | null {
  try { return window.localStorage; } catch { return null; }
}

const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

export function loadFilters(storage: Storage | null = defaultStorage()): Filters {
  try {
    const raw = storage?.getItem(FILTERS_KEY);
    if (!raw) return NO_FILTERS;
    const v = JSON.parse(raw) as { kinds?: unknown; books?: unknown; uncategorized?: unknown };
    const f: Filters = { kinds: new Set(strings(v?.kinds)), books: new Set(strings(v?.books)) };
    return v?.uncategorized === true ? { ...f, uncategorized: true } : f;
  } catch { return NO_FILTERS; }
}

export function saveFilters(f: Filters, storage: Storage | null = defaultStorage()): void {
  try { storage?.setItem(FILTERS_KEY, JSON.stringify({ kinds: [...f.kinds], books: [...f.books], ...(f.uncategorized ? { uncategorized: true } : {}) })); }
  catch { /* private mode: the filters just aren't remembered */ }
}

export const activeFilters = (f: Filters) => f.kinds.size + f.books.size;

// The answer graph's focused or full view (graphFocus.ts), remembered for the browser session.
export const FOCUS_KEY = "lore-graph-focus-v1";
export type FocusChoice = "focused" | "full";
function sessionStore(): Storage | null {
  try { return window.sessionStorage; } catch { return null; }
}
export function loadFocus(storage: Storage | null = sessionStore()): FocusChoice {
  try { return storage?.getItem(FOCUS_KEY) === "full" ? "full" : "focused"; } catch { return "focused"; }
}
export function saveFocus(choice: FocusChoice, storage: Storage | null = sessionStore()): void {
  try { storage?.setItem(FOCUS_KEY, choice); } catch { /* not remembered */ }
}

/** The filters with one kind or book flipped between shown and hidden. */
export function toggled(f: Filters, which: "kinds" | "books", value: string): Filters {
  const next = new Set(f[which]);
  if (next.has(value)) next.delete(value); else next.add(value);
  return { ...f, [which]: next };
}
