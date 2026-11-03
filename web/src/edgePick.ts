// Sigma hit-tests an edge only on its 1-2 px line, so clicking one took pixel
// precision. Clicks and hovers on empty canvas pick the nearest edge within this
// many screen pixels instead.
export const EDGE_PICK_PX = 8;

export function pointSegmentDistance(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax, dy = by - ay;
  const len2 = dx * dx + dy * dy;
  const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

// The nearest edge to (x, y) within `tolerance`, given each edge's endpoints in the same
// screen coordinates; null when none is close enough.
export function nearestEdge(x: number, y: number, edges: Iterable<{ id: string; a: { x: number; y: number }; b: { x: number; y: number } }>,
    tolerance = EDGE_PICK_PX): string | null {
  let best: string | null = null;
  let bestD = tolerance;
  for (const e of edges) {
    const d = pointSegmentDistance(x, y, e.a.x, e.a.y, e.b.x, e.b.y);
    if (d <= bestD) { bestD = d; best = e.id; }
  }
  return best;
}
