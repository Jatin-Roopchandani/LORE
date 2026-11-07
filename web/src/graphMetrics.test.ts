import { contentBounds, measureGraph, sigmaScale } from "./graphMetrics";
import type { GraphView } from "./graphView";

const node = (id: string, label = id) => ({ id, label, kind: "Character", books: [], seedPart: null, state: "shown" as const, expanded: false });
const view = (ids: string[]): GraphView => ({ nodes: ids.map((id) => node(id, `Entity ${id}`)), links: [], shownEdges: 0, totalEdges: 0 });

test("sigma's fit: the larger side of the box spans the smaller side of the canvas less 30 px padding", () => {
  expect(sigmaScale({ x0: 0, x1: 100, y0: 0, y1: 100 }, { width: 800, height: 600 })).toBeCloseTo(5.4);
  // A box as wide as the canvas's shape fills it edge to edge (sigma's aspect correction).
  expect(sigmaScale({ x0: 0, x1: 400, y0: 0, y1: 300 }, { width: 800, height: 600 })).toBeCloseTo((540 * (4 / 3)) / 400);
});

test("the content box widens the node centres by every dot and name, at the scale it gives itself", () => {
  const b = contentBounds([{ x: 0, y: 0, r: 10, w: 100 }, { x: 500, y: 300, r: 10, w: 100 }], { width: 954, height: 714 });
  const s = sigmaScale(b, { width: 954, height: 714 });
  expect(b.x1 - 500).toBeCloseTo((10 + 3 + 100) / s, 1);
  expect(-b.x0).toBeCloseTo(10 / s, 1);
});

test("the metric counts dots on dots and names on names at the fitted zoom", () => {
  const v = view(["a", "b", "c", "d"]);
  // a and b sit on each other, and so (just) do c and d, whose names then run over each other too.
  const p = new Map([["a", { x: 0, y: 0 }], ["b", { x: 1, y: 0 }], ["c", { x: 300, y: 300 }], ["d", { x: 305, y: 300 }]]);
  const m = measureGraph(v, p, { width: 800, height: 600 });
  expect(m.nodeOverlaps).toBe(2);
  expect(m.labelOverlaps).toBe(2);
  const apart = measureGraph(v, new Map([["a", { x: 0, y: 0 }], ["b", { x: 0, y: 300 }], ["c", { x: 300, y: 0 }], ["d", { x: 300, y: 300 }]]), { width: 800, height: 600 });
  expect([apart.nodeOverlaps, apart.labelOverlaps, apart.labelOnNode]).toEqual([0, 0, 0]);
});
