import { ALL_WORLDS, loadWorld, resolveWorld, saveWorld, suggestWorld, titleOfFile, WORLD_KEY, worldSlot } from "./worlds";
import type { World } from "./types";

const worlds: World[] = [
  { id: 1, name: "Alpha Saga", books: [{ id: "a1", title: "Alpha One", series: "Alpha Saga", series_index: 1 }] },
  { id: 2, name: "Beta Realm", books: [{ id: "b1", title: "Beta Solo", series: "The Beta Cycle", series_index: null }] },
];

beforeEach(() => localStorage.clear());

test("the choice round-trips through storage and tolerates a missing store", () => {
  expect(loadWorld()).toBeNull();
  saveWorld("2");
  expect(localStorage.getItem(WORLD_KEY)).toBe("2");
  expect(loadWorld()).toBe("2");
  expect(loadWorld(null)).toBeNull();
  expect(() => saveWorld("1", null)).not.toThrow();
});

test("resolveWorld keeps a known choice and falls back to all worlds", () => {
  expect(resolveWorld(null, worlds)).toBe(ALL_WORLDS);
  expect(resolveWorld("2", worlds)).toBe("2");
  expect(resolveWorld("9", worlds)).toBe(ALL_WORLDS);   // deleted since last visit
  expect(resolveWorld("9", null)).toBe("9");             // list still loading: no flicker
  expect(resolveWorld(ALL_WORLDS, worlds)).toBe(ALL_WORLDS);
});

test("a file named after an existing world or series suggests that world", () => {
  expect(suggestWorld("Alpha_Saga-03.epub", worlds)).toEqual({ worldId: 1, name: "Alpha Saga" });
  expect(suggestWorld("the beta cycle book 2.epub", worlds)).toEqual({ worldId: 2, name: "Beta Realm" });
});

test("an unknown file suggests a new world named after its title", () => {
  expect(suggestWorld("Gamma_Isles.epub", worlds)).toEqual({ worldId: null, name: "Gamma Isles" });
  expect(suggestWorld("x.epub", [])).toEqual({ worldId: null, name: "x" });
  expect(titleOfFile("a-b_c.d.pdf")).toBe("a b c d");
});

test("a world's mark is stable and one of six", () => {
  expect(worldSlot(1)).toBe(worldSlot(7));
  expect(new Set([1, 2, 3, 4, 5, 6].map(worldSlot)).size).toBe(6);
});
