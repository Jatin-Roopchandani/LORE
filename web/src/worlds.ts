import type { World } from "./types";

export const ALL_WORLDS = "all";
export const WORLD_KEY = "lore-world-v1";

function defaultStorage(): Storage | null {
  try { return window.localStorage; } catch { return null; }
}

/** The remembered choice: a world id as a string, or "all"; null when nothing is stored. */
export function loadWorld(storage: Storage | null = defaultStorage()): string | null {
  try { return storage?.getItem(WORLD_KEY) || null; } catch { return null; }
}

export function saveWorld(world: string, storage: Storage | null = defaultStorage()): void {
  try { storage?.setItem(WORLD_KEY, world); } catch { /* private mode: the choice just isn't remembered */ }
}

/** The world to use: the stored choice while it still exists (or while the list is loading), else all worlds. */
export function resolveWorld(stored: string | null, worlds: World[] | null): string {
  if (!stored) return ALL_WORLDS;
  if (stored === ALL_WORLDS || worlds === null) return stored;
  return worlds.some((w) => String(w.id) === stored) ? stored : ALL_WORLDS;
}

const squash = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();

/** A readable title from a file name: no extension, separators as spaces. */
export function titleOfFile(fileName: string): string {
  return fileName.replace(/\.[^.]+$/, "").replace(/[_\-.]+/g, " ").replace(/\s+/g, " ").trim();
}

export interface WorldSuggestion { worldId: number | null; name: string }

/**
 * The default world for a new book, from its file name alone (the browser can't read EPUB metadata):
 * an existing world whose name, or whose books' series, appears in the file name; otherwise a new world named
 * after the title. The CLI makes the same choice from the EPUB's metadata when no --world is given.
 */
export function suggestWorld(fileName: string, worlds: World[]): WorldSuggestion {
  const title = titleOfFile(fileName);
  const hay = ` ${squash(title)} `;
  for (const w of worlds) {
    const names = [w.name, ...w.books.map((b) => b.series ?? "")].map(squash).filter((n) => n.length >= 3);
    if (names.some((n) => hay.includes(` ${n} `))) return { worldId: w.id, name: w.name };
  }
  return { worldId: null, name: title };
}

/** Which of the six world marks a world gets: stable per id, shared by the graph and its legend. */
export const worldSlot = (id: number) => ((id % 6) + 6) % 6;
