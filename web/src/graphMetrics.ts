// How legible a laid-out graph is at the fitted zoom, measured rather than eyeballed: node-node overlaps,
// name-name overlaps, names over other nodes' dots, and how many edge labels get drawn (and how many of
// those collide). It follows the canvas: sigma's fit (stagePadding 30, its aspect correction), node radii
// from graphSizing, names drawn right of the dot, and edge labels placed by edgeLabel.ts in draw order.
import { boxQuad, placeEdgeLabel, QUIET_ZOOM, EDGE_LABEL_PX, quadDepth, type Box } from "./edgeLabel";
import type { GraphView } from "./graphView";
import { drawnLabel, edgeOrder, estimateWidth, isLarge, LABEL_GAP, NODE_LABEL_PX, nodeSizes } from "./graphSizing";
import type { Positions } from "./layout";

export interface Viewport { width: number; height: number }
export interface GraphMetrics {
  nodes: number; links: number;
  nodeOverlaps: number; labelOverlaps: number; labelOnNode: number;
  edgeLabels: number; edgeLabelsDrawn: number; citedLabels: number; citedLabelsDrawn: number; edgeLabelOverlaps: number;
  // px per layout unit at the fit, and the layout's extent in layout units.
  scale: number; extent: { width: number; height: number };
  // Nodes whose dot or name runs past the canvas edge.
  offCanvas: number;
}

const PADDING = 30;

export interface Bounds { x0: number; x1: number; y0: number; y1: number }
export interface Item { x: number; y: number; r: number; w: number }

// Sigma's camera at rest: the box `b` (by default the node centres' bounding box), normalised by its larger side,
// scaled by the smaller viewport side less the padding and by sigma's aspect correction (getCorrectionRatio).
// Returns screen px per layout unit.
export function sigmaScale(b: Bounds, vp: Viewport): number {
  const dx = Math.max(b.x1 - b.x0, 1e-9), dy = Math.max(b.y1 - b.y0, 1e-9);
  const vr = vp.height / vp.width, gr = dy / dx;
  const correction = (vr < 1 && gr > 1) || (vr > 1 && gr < 1) ? 1 : Math.min(Math.max(gr, 1 / gr), Math.max(1 / vr, vr));
  return ((Math.min(vp.width, vp.height) - 2 * PADDING) * correction) / Math.max(dx, dy);
}

export function centreBounds(items: Iterable<{ x: number; y: number }>): Bounds {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const p of items) { x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); y0 = Math.min(y0, p.y); y1 = Math.max(y1, p.y); }
  return x0 === Infinity ? { x0: 0, x1: 1, y0: 0, y1: 1 } : { x0, x1, y0, y1 };
}

// The box a large graph's camera fits: the node centres widened by every dot and name (`r` and `w` in screen px,
// which keep their size whatever the zoom), solved for the scale the box itself gives. Sigma fits node centres
// only, so the names of the right-most nodes ran off the canvas.
export function contentBounds(items: readonly Item[], vp: Viewport): Bounds {
  let b = centreBounds(items);
  for (let i = 0; i < 6; i++) {
    const s = sigmaScale(b, vp);
    b = { x0: Infinity, x1: -Infinity, y0: Infinity, y1: -Infinity };
    for (const it of items) {
      const half = Math.max(it.r, NODE_LABEL_PX / 2 + 1) / s;
      b.x0 = Math.min(b.x0, it.x - it.r / s); b.x1 = Math.max(b.x1, it.x + (it.r + LABEL_GAP + it.w) / s);
      b.y0 = Math.min(b.y0, it.y - half); b.y1 = Math.max(b.y1, it.y + half);
    }
  }
  return b;
}

const area = (a: Box, b: Box) =>
  Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));

// `measure`: text width in px at a font size (a canvas in the browser; an estimate in tests).
// `ratio`: the camera ratio (1 = fitted); `focus`: nodes whose edges show their labels (the open entity, the hovered one).
// Where the canvas draws each node at rest (camera fitted, zoomed by `ratio` about the centre), in px from the
// canvas's top left, with its radius.
export function screenPositions(view: GraphView, positions: Positions, vp: Viewport,
    measure: (text: string, px: number) => number = estimateWidth, ratio = 1): { at: Map<string, { x: number; y: number; r: number }>; scale: number; box: Bounds } {
  const sizes = nodeSizes(view);
  const items = view.nodes.map((n) => ({ ...(positions.get(n.id) ?? { x: 500, y: 350 }), r: sizes.get(n.id) ?? 7, w: measure(drawnLabel(n), NODE_LABEL_PX) }));
  const box = isLarge(view) ? contentBounds(items, vp) : centreBounds(items);
  const scale = sigmaScale(box, vp), cx = (box.x0 + box.x1) / 2, cy = (box.y0 + box.y1) / 2;
  const zoom = scale / ratio, sizeScale = 1 / Math.sqrt(ratio);
  const at = new Map<string, { x: number; y: number; r: number }>();
  view.nodes.forEach((n, i) => {
    const p = items[i];
    at.set(n.id, { x: vp.width / 2 + (p.x - cx) * zoom, y: vp.height / 2 + (p.y - cy) * zoom, r: p.r * sizeScale });
  });
  return { at, scale, box };
}

export function measureGraph(view: GraphView, positions: Positions, vp: Viewport,
    opts: { measure?: (text: string, px: number) => number; ratio?: number; focus?: ReadonlySet<string> } = {}): GraphMetrics {
  const measure = opts.measure ?? estimateWidth;
  const ratio = opts.ratio ?? 1;
  const focus = opts.focus ?? new Set<string>();
  const large = isLarge(view);
  const { at, scale, box } = screenPositions(view, positions, vp, measure, ratio);
  const dots: Box[] = [], names: Box[] = [];
  const nodes = view.nodes.map((n) => {
    const p = at.get(n.id)!;
    const dot = { x0: p.x - p.r, y0: p.y - p.r, x1: p.x + p.r, y1: p.y + p.r };
    const w = measure(drawnLabel(n), NODE_LABEL_PX);
    const name = { x0: p.x + p.r + LABEL_GAP, y0: p.y - NODE_LABEL_PX / 2 - 1, x1: p.x + p.r + LABEL_GAP + w, y1: p.y + NODE_LABEL_PX / 2 + 1 };
    dots.push(dot); names.push(name);
    return { p, dot, name };
  });
  let nodeOverlaps = 0, labelOverlaps = 0, labelOnNode = 0;
  const offCanvas = nodes.filter(({ dot, name }) => dot.x0 < 0 || dot.y0 < 0 || name.x1 > vp.width || dot.y1 > vp.height || name.y0 < 0 || name.y1 > vp.height).length;
  for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
    const a = nodes[i], b = nodes[j];
    if (Math.hypot(a.p.x - b.p.x, a.p.y - b.p.y) < a.p.r + b.p.r) nodeOverlaps++;
    if (area(a.name, b.name) > 0) labelOverlaps++;
    if (area(a.name, b.dot) > 0) labelOnNode++;
    if (area(b.name, a.dot) > 0) labelOnNode++;
  }
  // Edge labels, as GraphCanvas draws them: in the graph's edge order, each placed clear of what is already there.
  const obstacles: Box[] = nodes.flatMap(({ p, name }, i) => [{ x0: p.x - p.r - 3, y0: p.y - p.r - 3, x1: p.x + p.r + 3, y1: p.y + p.r + 3, owner: view.nodes[i].id },
    { x0: p.x + p.r, y0: p.y - 9, x1: p.x + p.r + 10 + (name.x1 - name.x0) * 1.08, y1: p.y + 9, owner: view.nodes[i].id }]);
  // While a node is in focus in a large graph, it and its neighbours stay; the rest step back (GraphCanvas).
  const near = new Set(focus);
  for (const l of view.links) { if (focus.has(l.source)) near.add(l.target); if (focus.has(l.target)) near.add(l.source); }
  const drawn: Box[] = [], pills: number[][][] = [];
  let edgeLabels = 0, edgeLabelsDrawn = 0, citedLabels = 0, citedLabelsDrawn = 0, edgeLabelOverlaps = 0;
  for (const l of edgeOrder(view)) {
    if (!l.label) continue;
    const a = at.get(l.source), b = at.get(l.target);
    if (!a || !b) continue;
    edgeLabels++;
    const lit = !!l.onPath || !!l.picked;
    const cited = l.state === "cited" || lit;
    if (cited) citedLabels++;
    const quiet = !lit && (l.state === "faint" || l.state === "cut");
    const focused = focus.has(l.source) || focus.has(l.target);
    if (quiet && ratio > QUIET_ZOOM && !focused) continue;
    if (quiet && large && !focused && focus.size > 0) continue;
    const lw = measure(l.label, EDGE_LABEL_PX);
    const stepBack = large && quiet && focused;
    const placed = placeEdgeLabel(a, b, a.r, b.r, lw, EDGE_LABEL_PX, [...obstacles.filter((o) => !stepBack || near.has(o.owner!)), ...drawn], large);
    if (!placed) continue;
    if (quiet && large && !placed.clear) continue;           // a dense graph's quiet label waits for room
    edgeLabelsDrawn++;
    if (cited) citedLabelsDrawn++;
    // What a reader sees collide: the pill against other names, dots and pills (its own ends' dots excepted).
    const pill = placed.quad;
    const others = [...nodes.filter((_, i) => view.nodes[i].id !== l.source && view.nodes[i].id !== l.target).flatMap((x) => [boxQuad(x.dot), boxQuad(x.name)]),
      ...nodes.filter((_, i) => view.nodes[i].id === l.source || view.nodes[i].id === l.target).map((x) => boxQuad(x.name)), ...pills];
    if (others.some((o) => quadDepth(pill, o) > 1)) edgeLabelOverlaps++;
    drawn.push(large ? { ...placed.box, quad: pill } : placed.box);
    pills.push(pill);
  }
  return { nodes: view.nodes.length, links: view.links.length, nodeOverlaps, labelOverlaps, labelOnNode,
    edgeLabels, edgeLabelsDrawn, citedLabels, citedLabelsDrawn, edgeLabelOverlaps,
    scale, extent: { width: box.x1 - box.x0, height: box.y1 - box.y0 }, offCanvas };
}
