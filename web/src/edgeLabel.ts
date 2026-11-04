// Edge labels drawn by LORE instead of Sigma's default. Sigma puts a label just off its line, cuts a long
// one short with "…", and doesn't check what else is there, so labels ran into nodes and each other.
// Here a label sits on its line inside a small pill, is never cut short, reads upright, and slides along
// its edge to the free spot that best avoids the nodes, their names and the labels already placed.

export interface Box { x0: number; y0: number; x1: number; y1: number }
export interface Pt { x: number; y: number }
export interface Placed { cx: number; cy: number; angle: number; box: Box }

export const EDGE_LABEL_PX = 12;
// Where along the edge (between the two node rims) a label may sit, best first.
const SPOTS = [0.5, 0.38, 0.62, 0.28, 0.72, 0.18, 0.82];
const PAD_X = 4, PAD_Y = 2;

const overlap = (a: Box, b: Box): number =>
  Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));

// The spot for a `w` x `h` label on the edge a -> b (node radii ra, rb), or null when the edge is too short
// to hold any label. Among the spots, the first with nothing under it wins; when every spot is taken, the
// one with the least overlap does, so a label is never dropped for want of room.
export function placeEdgeLabel(a: Pt, b: Pt, ra: number, rb: number, w: number, h: number, obstacles: readonly Box[]): Placed | null {
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
  for (const t of SPOTS) {
    const cx = sx + ux * len * t, cy = sy + uy * len * t;
    const box = { x0: cx - halfW, y0: cy - halfH, x1: cx + halfW, y1: cy + halfH };
    const score = obstacles.reduce((sum, o) => sum + overlap(box, o), 0);
    if (score < bestScore) { bestScore = score; best = { cx, cy, angle, box }; }
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

export interface LabelLook { pill: () => string; border: () => string; zoom: () => number; focus: (node: string) => boolean; hot: (edge: string) => boolean }

// A Sigma `defaultDrawEdgeLabel`.
export function makeDrawEdgeLabel(look: LabelLook) {
  return (ctx: CanvasRenderingContext2D, edge: { label?: string | null; quiet?: boolean; key?: string }, source: Display, target: Display, settings: EdgeLabelSettings) => {
    const label = edge.label;
    if (!label) return;
    if (edge.quiet && look.zoom() > QUIET_ZOOM && !(source.key && look.focus(source.key))
        && !(target.key && look.focus(target.key)) && !(edge.key && look.hot(edge.key))) return;
    const size = settings.edgeLabelSize;
    ctx.font = `${settings.edgeLabelWeight} ${size}px ${settings.edgeLabelFont}`;
    const w = ctx.measureText(label).width;
    const at = placeEdgeLabel(source, target, source.size, target.size, w, size, frame.obstacles);
    if (!at) return;
    frame.obstacles.push(at.box);
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
