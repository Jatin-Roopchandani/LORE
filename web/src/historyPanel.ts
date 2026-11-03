import { useEffect, useState } from "react";

export const HISTORY_OPEN_KEY = "lore-history-open-v1";
/** Below this width the sidebar is a drawer over the page, not a column. */
export const NARROW_QUERY = "(max-width: 900px)";

function defaultStorage(): Storage | null {
  try { return window.localStorage; } catch { return null; }
}

/** Whether the sidebar was left open; open when nothing is stored. */
export function loadHistoryOpen(storage: Storage | null = defaultStorage()): boolean {
  try { return storage?.getItem(HISTORY_OPEN_KEY) !== "0"; } catch { return true; }
}

export function saveHistoryOpen(open: boolean, storage: Storage | null = defaultStorage()): void {
  try { storage?.setItem(HISTORY_OPEN_KEY, open ? "1" : "0"); } catch { /* private mode: not remembered */ }
}

/** True while the window is narrow enough for the drawer layout. */
export function useNarrow(query = NARROW_QUERY): boolean {
  const read = () => (typeof window !== "undefined" && window.matchMedia?.(query).matches) || false;
  const [narrow, setNarrow] = useState(read);
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const on = () => setNarrow(mq.matches);
    on();
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, [query]);
  return narrow;
}
