// The graph filters: the kinds and books hidden in the graph pane. Remembered per browser.
export const FILTERS_KEY = "lore-graph-filters-v1";
export interface Filters { kinds: ReadonlySet<string>; books: ReadonlySet<string> }
export const NO_FILTERS: Filters = { kinds: new Set(), books: new Set() };

function defaultStorage(): Storage | null {
  try { return window.localStorage; } catch { return null; }
}

const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

export function loadFilters(storage: Storage | null = defaultStorage()): Filters {
  try {
    const raw = storage?.getItem(FILTERS_KEY);
    if (!raw) return NO_FILTERS;
    const v = JSON.parse(raw) as { kinds?: unknown; books?: unknown };
    return { kinds: new Set(strings(v?.kinds)), books: new Set(strings(v?.books)) };
  } catch { return NO_FILTERS; }
}

export function saveFilters(f: Filters, storage: Storage | null = defaultStorage()): void {
  try { storage?.setItem(FILTERS_KEY, JSON.stringify({ kinds: [...f.kinds], books: [...f.books] })); }
  catch { /* private mode: the filters just aren't remembered */ }
}

export const activeFilters = (f: Filters) => f.kinds.size + f.books.size;

/** The filters with one kind or book flipped between shown and hidden. */
export function toggled(f: Filters, which: "kinds" | "books", value: string): Filters {
  const next = new Set(f[which]);
  if (next.has(value)) next.delete(value); else next.add(value);
  return { ...f, [which]: next };
}
