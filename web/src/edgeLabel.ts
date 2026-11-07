// Edge labels drawn by LORE instead of Sigma's default. Sigma puts a label just off its line, cuts a long
// one short with "…", and doesn't check what else is there, so labels ran into nodes and each other.
// Here a label sits on its line inside a small pill, is never cut short, reads upright, and slides along
// its edge to the free spot that best avoids the nodes, their names and the labels already placed.

// `owner`: the node whose dot or name this is (absent for an edge label); `quad`: a drawn label's pill as its
// four corners (the box is then the pill's bounding box).
export interface Box { x0: number; y0: number; x1: number; y1: number; owner?: string; quad?: number[][] }
export interface Pt { x: number; y: number }
// `clear`: nothing already there is under the spot; `quad`: the pill's corners.
export interface Placed { cx: number; cy: number; angle: number; box: Box; clear: boolean; quad: number[][] }

export const EDGE_LABEL_PX = 12;
// Where along the edge (between the two node rims) a label may sit, best first. A dense graph also tries the
// in-between spots, last, when none of the first seven is clear.
const SPOTS = [0.5, 0.38, 0.62, 0.28, 0.72, 0.18, 0.82];
const DENSE_SPOTS = [...SPOTS, 0.44, 0.56, 0.33, 0.67, 0.23, 0.77];
const PAD_X = 4, PAD_Y = 2;

const overlap = (a: Box, b: Box): number =>
  Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));

/** A rectangle as its four corners: a box, or a pill of width w and height h centred at (cx, cy) and turned by `angle`. */
export const boxQuad = (b: Box): number[][] => b.quad ?? [[b.x0, b.y0], [b.x1, b.y0], [b.x1, b.y1], [b.x0, b.y1]];
export function pillQuad(cx: number, cy: number, angle: number, w: number, h: number): number[][] {
  const c = Math.cos(angle), s = Math.sin(angle);
  return [[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]].map(([x, y]) => [cx + x * c - y * s, cy + x * s + y * c]);
}
/** How far two rectangles (as corners) cut into each other along the axis that separates them best; 0 if apart. */
export function quadDepth(a: number[][], b: number[][]): number {
  let out = Infinity;
  for (const q of [a, b]) for (let i = 0; i < 2; i++) {
    const ex = q[i + 1][0] - q[i][0], ey = q[i + 1][1] - q[i][1], len = Math.hypot(ex, ey) || 1;
    const ax = ex / len, ay = ey / len;
    let a0 = Infinity, a1 = -Infinity, b0 = Infinity, b1 = -Infinity;
    for (const [x, y] of a) { const v = x * ax + y * ay; a0 = Math.min(a0, v); a1 = Math.max(a1, v); }
    for (const [x, y] of b) { const v = x * ax + y * ay; b0 = Math.min(b0, v); b1 = Math.max(b1, v); }
    out = Math.min(out, Math.min(a1, b1) - Math.max(a0, b0));
    if (out <= 0) return 0;
  }
  return out;
}

// The spot for a `w` x `h` label on the edge a -> b (node radii ra, rb), or null when the edge is too short
// to hold any label. Among the spots, the first with nothing under it wins; when every spot is taken, the
// one with the least overlap does, so a label is never dropped for want of room. `exact` (dense graphs) scores a
// spot by the turned pill itself rather than its bounding box, which on a slanted edge is far bigger than the pill.
export function placeEdgeLabel(a: Pt, b: Pt, ra: number, rb: number, w: number, h: number, obstacles: readonly Box[], exact = false): Placed | null {
  const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy);
  if (d - ra - rb < 12) return null;
  const ux = dx / d, uy = dy / d;
  const sx = a.x + ux * ra, sy = a.y + uy * ra, len = d - ra - rb;
  let angle = Math.atan2(dy, dx);
  if (angle > Math.PI / 2) angle -= Math.PI;          // keep the text upright
  else if (angle < -Math.PI / 2) angle += Math.PI;
  const bw = w + 2 * PAD_X, bh = h + 2 * PAD_Y;
  const c = Math.abs(Math.cos(angle)), s = Math.abs(Math.sin(angle));
  const halfW = (bw * c + bh * s) / 2, halfH = (bw * s + bh * c) / 2;
  let best: Placed | null = null, bestScore = Infinity;
  for (const t of exact ? DENSE_SPOTS : SPOTS) {
    const cx = sx + ux * len * t, cy = sy + uy * len * t;
    const box = { x0: cx - halfW, y0: cy - halfH, x1: cx + halfW, y1: cy + halfH };
    const quad = pillQuad(cx, cy, angle, bw, bh);
    const score = exact
      ? obstacles.reduce((sum, o) => sum + (overlap(box, o) > 0 ? quadDepth(quad, boxQuad(o)) : 0), 0)
      : obstacles.reduce((sum, o) => sum + overlap(box, o), 0);
    if (score < bestScore) { bestScore = score; best = { cx, cy, angle, box, clear: score === 0, quad }; }
    if (score === 0) break;
  }
  return best;
}

// The obstacles of one frame: node dots, node names, and the edge labels placed so far. Reset before each
// render (GraphCanvas fills it from the nodes' display data); each drawn label adds its box.
export const frame: { obstacles: Box[] } = { obstacles: [] };

interface EdgeLabelSettings { edgeLabelSize: number; edgeLabelFont: string; edgeLabelWeight: string; edgeLabelColor: { color?: string } }
type Display = { x: number; y: number; size: number; label?: string | null; key?: string };

// A quiet edge (context, not evidence: the overview's links, an answer's faint ones) shows its label only
// when the view is zoomed in this far (camera ratio) or the edge touches the node in focus.
export const QUIET_ZOOM = 0.55;

// `focusing`: some node is in focus (open or under the pointer) in a dense graph; `dimmed`: a node stepped back
// meanwhile (neither in focus nor linked to it).
export interface LabelLook { pill: () => string; border: () => string; zoom: () => number; focus: (node: string) => boolean; hot: (edge: string) => boolean;
  focusing?: () => boolean; dimmed?: (node: string) => boolean }

// A Sigma `defaultDrawEdgeLabel`. A `tidy` edge (a quiet one in a dense answer graph) is labelled only where its
// label has a clear spot, so zooming in brings labels in as room opens up instead of piling them on a hub; and
// while a node is in focus, only the quiet labels on that node's edges show. Labels of cited edges, and of the
// edge under the pointer, always show. A focused edge's label may cover the names of nodes stepped back for the
// focus (their names are muted), so a hub's own links can be read while it is in focus.
export function makeDrawEdgeLabel(look: LabelLook) {
  return (ctx: CanvasRenderingContext2D, edge: { label?: string | null; quiet?: boolean; tidy?: boolean; dense?: boolean; key?: string }, source: Display, target: Display, settings: EdgeLabelSettings) => {
    const label = edge.label;
    if (!label) return;
    const hot = !!edge.key && look.hot(edge.key);
    const focused = (!!source.key && look.focus(source.key)) || (!!target.key && look.focus(target.key));
    if (edge.quiet && !hot) {
      if (look.zoom() > QUIET_ZOOM && !focused) return;
      if (edge.tidy && !focused && look.focusing?.()) return;
    }
    const size = settings.edgeLabelSize;
    ctx.font = `${settings.edgeLabelWeight} ${size}px ${settings.edgeLabelFont}`;
    const w = ctx.measureText(label).width;
    const stepBack = edge.tidy && focused && look.focusing?.() ? look.dimmed : undefined;
    const obstacles = stepBack ? frame.obstacles.filter((o) => !o.owner || !stepBack(o.owner)) : frame.obstacles;
    const at = placeEdgeLabel(source, target, source.size, target.size, w, size, obstacles, !!edge.dense);
    if (!at) return;
    if (edge.quiet && edge.tidy && !hot && !at.clear) return;
    frame.obstacles.push(edge.dense ? { ...at.box, quad: at.quad } : at.box);
    ctx.save();
    ctx.translate(at.cx, at.cy);
    ctx.rotate(at.angle);
    ctx.fillStyle = look.pill();
    ctx.globalAlpha = 0.92;
    ctx.beginPath();
    const bw = w + 2 * PAD_X, bh = size + 2 * PAD_Y;
    if (typeof ctx.roundRect === "function") ctx.roundRect(-bw / 2, -bh / 2, bw, bh, 4); else ctx.rect(-bw / 2, -bh / 2, bw, bh);
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = look.border();   // a thin edge, so the pill shows on a background of the same colour
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = settings.edgeLabelColor.color ?? "#000";
    ctx.textBaseline = "middle";
    ctx.fillText(label, -w / 2, 0);
    ctx.restore();
  };
}
