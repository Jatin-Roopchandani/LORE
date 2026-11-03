// Two remembered choices about the steps panel: whether it is open once an answer has landed, and whether the
// list of the question's parts (inside it) is open. Per viewer, in this browser; the page works without them.
export const STEPS_KEY = "lore-steps-open-v1";
export const SPLIT_KEY = "lore-split-open-v1";

function defaultStorage(): Storage | null {
  try { return window.localStorage; } catch { return null; }
}

/** The remembered choice, or `fallback` when nothing valid is stored (or storage is unavailable). */
export function loadOpen(key: string, fallback: boolean, storage: Storage | null = defaultStorage()): boolean {
  try {
    const v = storage?.getItem(key);
    return v === "open" ? true : v === "closed" ? false : fallback;
  } catch { return fallback; }
}

export function saveOpen(key: string, open: boolean, storage: Storage | null = defaultStorage()): void {
  try { storage?.setItem(key, open ? "open" : "closed"); } catch { /* private mode: the choice just isn't remembered */ }
}
