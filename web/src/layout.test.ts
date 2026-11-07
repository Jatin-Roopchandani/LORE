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
  const p = place(new Map(), v, null, true);
  expect(place(new Map(), v, null, true)).toEqual(p);
  const pts = ids.map((id) => p.get(id)!);
  for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++)
    expect(Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y)).toBeGreaterThanOrEqual(60);
  const cx = pts.reduce((a, q) => a + q.x, 0) / pts.length, cy = pts.reduce((a, q) => a + q.y, 0) / pts.length;
  const r = (q: { x: number; y: number }) => Math.hypot(q.x - cx, q.y - cy);
  const inner = pts.slice(0, 5).reduce((a, q) => a + r(q), 0) / 5;
  const outer = pts.slice(-5).reduce((a, q) => a + r(q), 0) / 5;
  expect(inner).toBeLessThan(outer);
});

test("only the overview uses the spiral start; a seedless answer graph hangs nodes off their neighbours", () => {
  const v: GraphView = { nodes: [node("a"), node("b"), node("c")], links: [link("a", "b"), link("a", "c")], shownEdges: 2, totalEdges: 2 };
  const spiral = place(new Map(), v, null, true);
  const plain = place(new Map(), v);
  expect(plain).not.toEqual(spiral);
  // The spiral puts the first node nearest the centre (500, 350); the plain path puts it 140 out on a hashed angle.
  expect(Math.hypot(spiral.get("a")!.x - 500, spiral.get("a")!.y - 350)).toBeLessThan(60);
  expect(Math.hypot(plain.get("a")!.x - 500, plain.get("a")!.y - 350)).toBeGreaterThan(100);
});

test("overview nodes on nearly the same row are kept a label's width apart", () => {
  const ids = Array.from({ length: 40 }, (_, i) => `e${i}`);
  const p = place(new Map(), { nodes: ids.map((id) => node(id)), links: [], shownEdges: 0, totalEdges: 0 }, null, true);
  const pts = [...p.values()];
  let clashes = 0;
  for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++)
    if (Math.abs(pts[i].x - pts[j].x) < 140 && Math.abs(pts[i].y - pts[j].y) < 20) clashes++;
  expect(clashes).toBe(0);
});

test("the overview puts linked entities near each other: two clusters stay apart and edges rarely cross", () => {
  const a = Array.from({ length: 10 }, (_, i) => `a${i}`), b = Array.from({ length: 10 }, (_, i) => `b${i}`);
  // Interleave the two clusters in view order, so the spiral alone would mix them.
  const ids = a.flatMap((x, i) => [x, b[i]]);
  const links = [...a.slice(1).map((x) => link(a[0], x)), ...b.slice(1).map((x) => link(b[0], x)), link(a[1], a[2]), link(b[1], b[2])];
  const p = place(new Map(), { nodes: ids.map((id) => node(id)), links, shownEdges: 0, totalEdges: 0 }, null, true);
  const centre = (g: string[]) => ({ x: g.reduce((s, id) => s + p.get(id)!.x, 0) / g.length, y: g.reduce((s, id) => s + p.get(id)!.y, 0) / g.length });
  const spread = (g: string[]) => { const c = centre(g); return g.reduce((s, id) => s + Math.hypot(p.get(id)!.x - c.x, p.get(id)!.y - c.y), 0) / g.length; };
  const ca = centre(a), cb = centre(b);
  expect(Math.hypot(ca.x - cb.x, ca.y - cb.y)).toBeGreaterThan(spread(a) + spread(b) * 0.5);
  const seg = links.map((l) => [p.get(l.source)!, p.get(l.target)!, l.source, l.target] as const);
  const o = (p1: { x: number; y: number }, q: { x: number; y: number }, r: { x: number; y: number }) => Math.sign((q.x - p1.x) * (r.y - p1.y) - (q.y - p1.y) * (r.x - p1.x));
  let crossings = 0;
  for (let i = 0; i < seg.length; i++) for (let j = i + 1; j < seg.length; j++) {
    const [p1, q1, s1, t1] = seg[i], [p2, q2, s2, t2] = seg[j];
    if (new Set([s1, t1, s2, t2]).size < 4) continue;
    if (o(p1, q1, p2) * o(p1, q1, q2) < 0 && o(p2, q2, p1) * o(p2, q2, q1) < 0) crossings++;
  }
  expect(crossings).toBeLessThanOrEqual(2);
});

// ---- answer graphs by size, on recorded answers (src/fixtures) ----
import { denseLayout } from "./layout";
import { replay } from "./fixtures/replay";
import { measureGraph } from "./graphMetrics";
import { nodeSizes, LARGE_GRAPH } from "./graphSizing";
import golden from "./fixtures/small-answer-layout.golden.json";
import single from "./fixtures/single-part.json";
import twoPart from "./fixtures/two-part.json";
import darkOne from "./fixtures/answer-graph-dark-one.json";
import battle from "./fixtures/answer-graph-battle.json";
import type { AskEvent } from "./types";

// The graph pane at 1440x900.
const PANE = { width: 954, height: 714 };

test("a small answer is laid out and sized exactly as before the density layout", () => {
  for (const [name, events] of Object.entries({ one: single, two: twoPart })) {
    const { view, positions } = replay(events as AskEvent[]);
    expect(view.nodes.length).toBeLessThan(LARGE_GRAPH);
    const want = (golden as Record<string, { positions: Record<string, { x: number; y: number }>; sizes: Record<string, number> }>)[name];
    expect(Object.fromEntries(positions)).toEqual(want.positions);
    expect(Object.fromEntries(nodeSizes(view))).toEqual(want.sizes);
  }
});

for (const [name, events] of Object.entries({ "dark one": darkOne, battle })) {
  test(`a large answer (${name}) fits the pane with no dot or name on another, and every cited label drawn`, () => {
    const { view, positions } = replay(events as AskEvent[]);
    expect(view.nodes.length).toBeGreaterThanOrEqual(100);
    const m = measureGraph(view, positions, PANE);
    expect(m.nodeOverlaps).toBe(0);
    expect(m.labelOverlaps).toBe(0);
    expect(m.labelOnNode).toBeLessThanOrEqual(2);
    expect(m.offCanvas).toBe(0);
    expect(m.citedLabelsDrawn).toBe(m.citedLabels);
    // Bounded: about one pane of layout, so the fitted camera barely shrinks it (and names keep their room).
    expect(m.scale).toBeGreaterThan(0.85);
    expect(m.extent.width).toBeLessThan(1400);
    expect(m.extent.height).toBeLessThan(1000);
  });
}

test("the density layout is deterministic, and every link is long enough to see", () => {
  const { view } = replay(darkOne as AskEvent[]);
  const a = denseLayout(view), b = denseLayout(view);
  expect(b).toEqual(a);
  const sizes = nodeSizes(view);
  for (const l of view.links) {
    const p = a.get(l.source)!, q = a.get(l.target)!;
    expect(Math.hypot(p.x - q.x, p.y - q.y)).toBeGreaterThanOrEqual(sizes.get(l.source)! + sizes.get(l.target)! + 16 - 1e-6);
  }
});

test("a hub's satellites ring it, and two hubs keep apart", () => {
  const { view, positions } = replay(darkOne as AskEvent[]);
  const hub = view.nodes.find((n) => n.label === "The Dark One")!.id, other = view.nodes.find((n) => n.label === "Rand al'Thor")!.id;
  const deg = new Map<string, number>();
  for (const l of view.links) for (const e of [l.source, l.target]) deg.set(e, (deg.get(e) ?? 0) + 1);
  const sats = view.links.filter((l) => l.source === hub || l.target === hub).map((l) => (l.source === hub ? l.target : l.source)).filter((id) => deg.get(id) === 1);
  expect(sats.length).toBeGreaterThan(20);
  const h = positions.get(hub)!, o = positions.get(other)!;
  const dist = (id: string) => Math.hypot(positions.get(id)!.x - h.x, positions.get(id)!.y - h.y);
  // Nearer their hub than the other hub, all round it.
  expect(sats.filter((s) => dist(s) < Math.hypot(positions.get(s)!.x - o.x, positions.get(s)!.y - o.y)).length).toBeGreaterThan(sats.length * 0.8);
  const sides = new Set(sats.map((s) => Math.floor((Math.atan2(positions.get(s)!.y - h.y, positions.get(s)!.x - h.x) + Math.PI) / (Math.PI / 2)) % 4));
  expect(sides.size).toBeGreaterThanOrEqual(3);              // round it, except where the other hub is
  expect(Math.hypot(h.x - o.x, h.y - o.y)).toBeGreaterThan(150);
});

test("a synthetic answer of 300 nodes and three hubs stays bounded (no explosion)", () => {
  const ids = Array.from({ length: 300 }, (_, i) => `n${i}`);
  const hubs = ["n0", "n1", "n2"];
  const links = ids.slice(3).map((id, i) => link(hubs[i % 3], id));
  links.push(link("n0", "n1"), link("n1", "n2"));
  const v: GraphView = { nodes: ids.map((id, i) => node(id, i < 3 ? 0 : null)), links, shownEdges: 0, totalEdges: 0 };
  const p = place(new Map(), v);
  const xs = [...p.values()].map((q) => q.x), ys = [...p.values()].map((q) => q.y);
  expect(xs.every(Number.isFinite) && ys.every(Number.isFinite)).toBe(true);
  expect(Math.max(...xs) - Math.min(...xs)).toBeLessThan(4000);
  expect(Math.max(...ys) - Math.min(...ys)).toBeLessThan(4000);
});

test("a large answer is laid out again as it grows, but never once pinned by an expansion", () => {
  const { view, positions } = replay(darkOne as AskEvent[]);
  expect(place(positions, view)).toBe(positions);                       // nothing new: nothing moves
  const grown: GraphView = { ...view, nodes: [...view.nodes, node("zz-new")], links: [...view.links, link(view.nodes[0].id, "zz-new")] };
  const pinned = place(positions, grown, view.nodes[0].id, false, true);
  for (const [id, pt] of positions) expect(pinned.get(id)).toEqual(pt);
  expect(pinned.has("zz-new")).toBe(true);
  const free = place(positions, grown);
  expect(free.has("zz-new")).toBe(true);
});

test("hiding nodes of a large answer with a filter moves nothing, and they come back where they were", () => {
  const { view, positions } = replay(darkOne as AskEvent[]);
  const hiddenIds = new Set(view.nodes.slice(50, 60).map((n) => n.id));
  const fewer: GraphView = { ...view, nodes: view.nodes.filter((n) => !hiddenIds.has(n.id)),
    links: view.links.filter((l) => !hiddenIds.has(l.source) && !hiddenIds.has(l.target)) };
  const p1 = place(positions, fewer);
  for (const n of fewer.nodes) expect(p1.get(n.id)).toEqual(positions.get(n.id));
  const p2 = place(p1, view);
  for (const n of view.nodes) expect(p2.get(n.id)).toEqual(positions.get(n.id));
});
