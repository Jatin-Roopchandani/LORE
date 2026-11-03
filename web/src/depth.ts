import type { Depth } from "./types";

export const DEPTH_KEY = "lore-depth-v1";
export const DEPTHS: Depth[] = ["auto", "deep", "focused"];

function defaultStorage(): Storage | null {
  try { return window.localStorage; } catch { return null; }
}

/** The remembered Deep dive choice; auto when nothing valid is stored. */
export function loadDepth(storage: Storage | null = defaultStorage()): Depth {
  try {
    const v = storage?.getItem(DEPTH_KEY);
    return DEPTHS.includes(v as Depth) ? (v as Depth) : "auto";
  } catch { return "auto"; }
}

export function saveDepth(depth: Depth, storage: Storage | null = defaultStorage()): void {
  try { storage?.setItem(DEPTH_KEY, depth); } catch { /* private mode: the choice just isn't remembered */ }
}
