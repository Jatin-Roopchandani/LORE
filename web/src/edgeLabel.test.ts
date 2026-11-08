import { expect, test } from "vitest";
import { frame, makeDrawEdgeLabel, placeEdgeLabel, QUIET_ZOOM, type Box, type LabelLook } from "./edgeLabel";

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

test("a placement says whether its spot is clear", () => {
  expect(placeEdgeLabel({ x: 0, y: 0 }, { x: 200, y: 0 }, 10, 10, 50, 12, [])!.clear).toBe(true);
  expect(placeEdgeLabel({ x: 0, y: 0 }, { x: 200, y: 0 }, 10, 10, 50, 12, [{ x0: -1000, y0: -1000, x1: 1000, y1: 1000 }])!.clear).toBe(false);
});

// A recording 2D context: enough of CanvasRenderingContext2D for makeDrawEdgeLabel.
function recorder() {
  const texts: string[] = [];
  const ctx = { font: "", fillStyle: "", strokeStyle: "", lineWidth: 1, globalAlpha: 1, textBaseline: "",
    measureText: (t: string) => ({ width: t.length * 6 }), save() {}, restore() {}, translate() {}, rotate() {}, beginPath() {},
    rect() {}, fill() {}, stroke() {}, fillText: (t: string) => { texts.push(t); } };
  return { ctx: ctx as unknown as CanvasRenderingContext2D, texts };
}
const settings = { edgeLabelSize: 12, edgeLabelFont: "x", edgeLabelWeight: "500", edgeLabelColor: { color: "#000" } };
const look = (over: Partial<LabelLook> = {}): LabelLook => ({ pill: () => "#fff", border: () => "#ccc", zoom: () => 1, focus: () => false,
  hot: () => false, ...over });
const a = { x: 0, y: 0, size: 5, key: "a" }, b = { x: 300, y: 0, size: 5, key: "b" };

test("a quiet label shows only zoomed in, on a node in focus or on the hovered edge", () => {
  const draw = (l: LabelLook) => { frame.obstacles = []; const r = recorder(); makeDrawEdgeLabel(l)(r.ctx, { label: "knows", quiet: true, key: "e" }, a, b, settings); return r.texts; };
  expect(draw(look())).toEqual([]);
  expect(draw(look({ zoom: () => QUIET_ZOOM - 0.1 }))).toEqual(["knows"]);
  expect(draw(look({ focus: (n) => n === "a" }))).toEqual(["knows"]);
  expect(draw(look({ hot: (e) => e === "e" }))).toEqual(["knows"]);
});

test("in a dense graph a quiet label is drawn only where it has room; a cited or hovered one always is", () => {
  const crowded = () => { frame.obstacles = [{ x0: -1000, y0: -1000, x1: 1000, y1: 1000 }]; };
  const run = (edge: { label: string; quiet?: boolean; tidy?: boolean; key: string }, l = look({ zoom: () => 0.3 })) => {
    crowded(); const r = recorder(); makeDrawEdgeLabel(l)(r.ctx, edge, a, b, settings); return r.texts;
  };
  expect(run({ label: "knows", quiet: true, tidy: true, key: "e" })).toEqual([]);
  expect(run({ label: "knows", quiet: true, key: "e" })).toEqual(["knows"]);              // a small graph draws it anyway
  expect(run({ label: "kills", tidy: true, key: "c" })).toEqual(["kills"]);               // cited: not quiet
  expect(run({ label: "knows", quiet: true, tidy: true, key: "e" }, look({ zoom: () => 0.3, hot: (e) => e === "e" }))).toEqual(["knows"]);
  frame.obstacles = [];
  const r = recorder();
  makeDrawEdgeLabel(look({ zoom: () => 0.3 }))(r.ctx, { label: "knows", quiet: true, tidy: true, key: "e" }, a, b, settings);
  expect(r.texts).toEqual(["knows"]);                                                     // with room it shows
});

test("while a node is in focus in a dense graph, quiet labels away from it step back", () => {
  frame.obstacles = [];
  const r = recorder();
  makeDrawEdgeLabel(look({ zoom: () => 0.3, focus: (n) => n === "z", focusing: () => true }))(r.ctx, { label: "knows", quiet: true, tidy: true, key: "e" }, a, b, settings);
  expect(r.texts).toEqual([]);
});

test("an edge of the node in focus may sit over the names of nodes stepped back, never over anything else", () => {
  const draw = (owner: string) => {
    frame.obstacles = [{ x0: -1000, y0: -1000, x1: 1000, y1: 1000, owner }];
    const r = recorder();
    makeDrawEdgeLabel(look({ focus: (n) => n === "a", focusing: () => true, dimmed: (n) => n === "far" }))(r.ctx, { label: "knows", quiet: true, tidy: true, key: "e" }, a, b, settings);
    return r.texts;
  };
  expect(draw("far")).toEqual(["knows"]);
  expect(draw("near")).toEqual([]);
});

test("measured exactly, a slanted label fits beside a name that only its bounding box would touch", () => {
  // A 45-degree edge; a name box sits just off the line, inside the label's bounding box but clear of the pill.
  const name: Box = { x0: 140, y0: 89, x1: 160, y1: 100 };
  const rough = placeEdgeLabel({ x: 0, y: 0 }, { x: 250, y: 250 }, 10, 10, 80, 12, [name], false)!;
  const exact = placeEdgeLabel({ x: 0, y: 0 }, { x: 250, y: 250 }, 10, 10, 80, 12, [name], true)!;
  expect(exact.clear).toBe(true);
  expect(exact.cx).toBeCloseTo(125);
  expect(rough.cx).not.toBeCloseTo(125);
});
