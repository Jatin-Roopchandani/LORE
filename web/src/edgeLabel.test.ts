import { expect, test } from "vitest";
import { placeEdgeLabel, type Box } from "./edgeLabel";

const hit = (a: Box, b: Box) => Math.min(a.x1, b.x1) > Math.max(a.x0, b.x0) && Math.min(a.y1, b.y1) > Math.max(a.y0, b.y0);

test("a label sits at the middle of its edge when nothing is there", () => {
  const at = placeEdgeLabel({ x: 0, y: 0 }, { x: 200, y: 0 }, 10, 10, 50, 12, [])!;
  expect(at.cx).toBeCloseTo(100);
  expect(at.cy).toBeCloseTo(0);
  expect(at.angle).toBeCloseTo(0);
});

test("text always reads upright, whichever way the edge points", () => {
  for (const [bx, by] of [[-200, 0], [-150, 150], [-150, -150], [0, 200], [0, -200]]) {
    const at = placeEdgeLabel({ x: 0, y: 0 }, { x: bx, y: by }, 5, 5, 40, 12, [])!;
    expect(Math.abs(at.angle)).toBeLessThanOrEqual(Math.PI / 2 + 1e-9);
  }
});

test("a label slides along its edge to avoid a node's name and another label", () => {
  const taken: Box[] = [{ x0: 70, y0: -20, x1: 130, y1: 20 }];
  const at = placeEdgeLabel({ x: 0, y: 0 }, { x: 300, y: 0 }, 10, 10, 40, 12, taken)!;
  expect(hit(at.box, taken[0])).toBe(false);
});

test("a long label is never cut short, and a crowded edge still gets its label", () => {
  const everywhere: Box[] = [{ x0: -1000, y0: -1000, x1: 1000, y1: 1000 }];
  const at = placeEdgeLabel({ x: 0, y: 0 }, { x: 80, y: 0 }, 5, 5, 200, 12, everywhere);
  expect(at).not.toBeNull();
  expect(at!.box.x1 - at!.box.x0).toBeGreaterThanOrEqual(200);
});

test("an edge too short to hold any label gets none", () => {
  expect(placeEdgeLabel({ x: 0, y: 0 }, { x: 20, y: 0 }, 6, 6, 30, 12, [])).toBeNull();
});
