import { place } from "./layout";
import type { GraphView } from "./graphView";

const node = (id: string, seedPart: number | null = null) => ({ id, label: id, kind: "Character", books: [], seedPart, state: "shown" as const, expanded: false });
const link = (s: string, t: string) => ({ id: `${s}->${t}`, source: s, target: t, label: "", count: 1, chunkIds: [], part: 0, state: "shown" as const, hot: false, books: [], both: false });

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

test("place never mutates or moves placed nodes", () => {
  const v1: GraphView = { nodes: [node("a", 0), node("b")], links: [link("a", "b")], shownEdges: 1, totalEdges: 1 };
  const p1 = place(new Map(), v1);
  const before = structuredClone([...p1]);
  const v2: GraphView = { ...v1, nodes: [...v1.nodes, node("c"), node("d", 1)], links: [...v1.links, link("b", "c")] };
  const p2 = place(p1, v2);
  expect([...p1]).toEqual(before);
  for (const [id, pt] of before) expect(p2.get(id)).toEqual(pt);
});

test("seeds fed one at a time stay spread and never move", () => {
  let pos = new Map<string, { x: number; y: number }>();
  const nodes: ReturnType<typeof node>[] = [];
  for (let i = 0; i < 8; i++) {
    nodes.push(node(`s${i}`, 0));
    const before = structuredClone([...pos]);
    pos = place(pos, { nodes: [...nodes], links: [], shownEdges: 0, totalEdges: 0 });
    for (const [id, pt] of before) expect(pos.get(id)).toEqual(pt);
  }
  const pts = [...pos.values()];
  expect(pts).toHaveLength(8);
  for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++)
    expect(Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y)).toBeGreaterThanOrEqual(60);
});

test("an overview of 40 entities spreads out, most-connected nearest the centre, deterministically", () => {
  const ids = Array.from({ length: 40 }, (_, i) => `e${i}`);
  const v: GraphView = { nodes: ids.map((id) => node(id)),
    links: ids.slice(1).map((id, i) => link(ids[i % 5], id)), shownEdges: 0, totalEdges: 0 };
  const p = place(new Map(), v);
  expect(place(new Map(), v)).toEqual(p);
  const pts = ids.map((id) => p.get(id)!);
  for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++)
    expect(Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y)).toBeGreaterThanOrEqual(60);
  const cx = pts.reduce((a, q) => a + q.x, 0) / pts.length, cy = pts.reduce((a, q) => a + q.y, 0) / pts.length;
  const r = (q: { x: number; y: number }) => Math.hypot(q.x - cx, q.y - cy);
  const inner = pts.slice(0, 5).reduce((a, q) => a + r(q), 0) / 5;
  const outer = pts.slice(-5).reduce((a, q) => a + r(q), 0) / 5;
  expect(inner).toBeLessThan(outer);
});
