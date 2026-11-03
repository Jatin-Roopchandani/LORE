import { FILTERS_KEY, activeFilters, loadFilters, saveFilters, toggled } from "./filters";

test("filters round-trip through storage and survive junk", () => {
  localStorage.clear();
  expect(activeFilters(loadFilters())).toBe(0);
  saveFilters({ kinds: new Set(["Place"]), books: new Set(["b1"]) });
  const f = loadFilters();
  expect([...f.kinds]).toEqual(["Place"]);
  expect([...f.books]).toEqual(["b1"]);
  localStorage.setItem(FILTERS_KEY, "{not json");
  expect(activeFilters(loadFilters())).toBe(0);
  localStorage.setItem(FILTERS_KEY, JSON.stringify({ kinds: "Place", books: [1, "b2"] }));
  expect([...loadFilters().books]).toEqual(["b2"]);
});

test("toggled flips one value without touching the other set", () => {
  const f = toggled({ kinds: new Set(), books: new Set(["b1"]) }, "kinds", "Place");
  expect([...f.kinds]).toEqual(["Place"]);
  expect(toggled(f, "kinds", "Place").kinds.size).toBe(0);
  expect([...f.books]).toEqual(["b1"]);
});
