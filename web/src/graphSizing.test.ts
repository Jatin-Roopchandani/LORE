import { edgeOrder, isLarge, LARGE_GRAPH, nodeSizes } from "./graphSizing";
import type { GraphView, LinkState } from "./graphView";

const node = (id: string, seedPart: number | null = null) => ({ id, label: id, kind: "Character", books: [], seedPart, state: "shown" as const, expanded: false });
const link = (s: string, t: string, state: LinkState = "faint") => ({ id: `${s}<->${t}`, source: s, target: t, label: "knows", count: 1, chunkIds: [], part: 0, state, hot: false, books: [], both: false });
// A hub with n-1 spokes.
const star = (n: number, states: LinkState[] = []): GraphView => ({ nodes: Array.from({ length: n }, (_, i) => node(`n${i}`, i === 0 ? 0 : null)),
  links: Array.from({ length: n - 1 }, (_, i) => link("n0", `n${i + 1}`, states[i] ?? "faint")), shownEdges: 0, totalEdges: 0 });

test("a small answer keeps its sizes: seeds 11, the rest 7, whatever their links", () => {
  const v = star(10);
  expect(isLarge(v)).toBe(false);
  const s = nodeSizes(v);
  expect(s.get("n0")).toBe(11);
  expect(s.get("n5")).toBe(7);
});

test("a large answer's nodes grow with their links, larger than a small answer's", () => {
  const v = star(LARGE_GRAPH + 10);
  v.links.push(link("n1", "n2"), link("n1", "n3"));
  expect(isLarge(v)).toBe(true);
  const s = nodeSizes(v);
  expect(s.get("n0")).toBe(18);                                  // the hub: a seed with the most links
  expect(s.get("n1")!).toBeGreaterThan(s.get("n9")!);            // three links beat one
  expect(Math.min(...s.values())).toBeGreaterThanOrEqual(8);     // nothing shrinks below a small answer's 7
});

test("a large answer hands sigma its cited edges first; a small one keeps its order", () => {
  const states: LinkState[] = Array.from({ length: LARGE_GRAPH }, (_, i) => (i === 30 ? "cited" : i === 20 ? "shown" : "faint"));
  const big = star(LARGE_GRAPH + 1, states);
  expect(edgeOrder(big).slice(0, 2).map((l) => l.target)).toEqual(["n31", "n21"]);
  const small = star(10, ["faint", "cited"]);
  expect(edgeOrder(small)).toBe(small.links);
});
