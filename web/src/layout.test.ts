import { place } from "./layout";
import type { GraphView } from "./graphView";

const node = (id: string, seedPart: number | null = null) => ({ id, label: id, kind: "Character", books: [], seedPart, state: "shown" as const, expanded: false });
const link = (s: string, t: string) => ({ id: `${s}->${t}`, source: s, target: t, label: "", count: 1, chunkIds: [], part: 0, state: "shown" as const, hot: false, books: [] });

test("placed nodes never move when the graph grows", () => {
  const v1: GraphView = { nodes: [node("a", 0), node("b")], links: [link("a", "b")], shownEdges: 1, totalEdges: 1 };
  const p1 = place(new Map(), v1);
  const v2: GraphView = { ...v1, nodes: [...v1.nodes, node("c")], links: [...v1.links, link("b", "c")] };
  const p2 = place(p1, v2);
  expect(p2.get("a")).toEqual(p1.get("a"));
  expect(p2.get("b")).toEqual(p1.get("b"));
  expect(p2.has("c")).toBe(true);
});

test("deterministic and spread out", () => {
  const v: GraphView = { nodes: ["a", "b", "c", "d"].map((id, i) => node(id, i === 0 ? 0 : null)),
    links: [link("a", "b"), link("a", "c"), link("a", "d")], shownEdges: 3, totalEdges: 3 };
  const p = place(new Map(), v);
  expect(place(new Map(), v)).toEqual(p);
  const pts = [...p.values()];
  for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++)
    expect(Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y)).toBeGreaterThanOrEqual(60);
});
