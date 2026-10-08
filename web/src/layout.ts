import type { GraphView } from "./graphView";

export type Positions = Map<string, { x: number; y: number }>;
const CX = 500, CY = 350;
// Label box in layout units, for the overview's label-collision pass.
const LABEL_W = 150, LABEL_H = 24;
const SPIRAL = 60, GOLDEN = Math.PI * (3 - Math.sqrt(5));

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  h ^= h >>> 16; h = Math.imul(h, 2246822507); h ^= h >>> 13; h = Math.imul(h, 3266489909); h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

// `overview`: the library overview, placed on a spiral (only then; answer graphs hang off their seeds).
export function place(previous: Positions, view: GraphView, anchor: string | null = null, overview = false): Positions {
  const pos: Positions = new Map(previous);
  const fresh = view.nodes.filter((n) => !pos.has(n.id));
  const seeds = fresh.filter((n) => n.seedPart !== null);
  const totalSeeds = view.nodes.filter((n) => n.seedPart !== null).length;
  const radius = Math.max(220, 12 * totalSeeds);
  for (const n of seeds) {
    const a = (n.seedPart ?? 0) * 0.6 + 2 * Math.PI * hash(n.id);
    pos.set(n.id, { x: CX + radius * Math.cos(a), y: CY + radius * Math.sin(a) });
  }
  const neighbours = new Map<string, string[]>();
  for (const l of view.links) {
    neighbours.set(l.source, [...(neighbours.get(l.source) ?? []), l.target]);
    neighbours.set(l.target, [...(neighbours.get(l.target) ?? []), l.source]);
  }
  const others = fresh.filter((n) => n.seedPart === null);
  if (overview && pos.size === 0 && others.length > 0) {
    // Nothing to hang them on (the library overview): a sunflower spiral in view order, so the
    // most-connected nodes, which come first, sit nearest the centre and the rest spread evenly.
    others.forEach((n, i) => {
      const r = SPIRAL * Math.sqrt(i + 0.5), a = i * GOLDEN;
      pos.set(n.id, { x: CX + r * Math.cos(a), y: CY + r * Math.sin(a) });
    });
  }
  for (const n of others) {
    if (pos.has(n.id)) continue;
    const near = (neighbours.get(n.id) ?? []).find((m) => pos.has(m)) ?? anchor ?? null;
    const c = (near && pos.get(near)) || { x: CX, y: CY };
    const a = 2 * Math.PI * hash(n.id);
    pos.set(n.id, { x: c.x + 140 * Math.cos(a), y: c.y + 140 * Math.sin(a) });
  }
  const movable = new Set(fresh.map((n) => n.id));
  const ids = [...pos.keys()];
  for (let it = 0; it < 60; it++) {
    for (const id of movable) {
      const p = pos.get(id)!;
      for (const other of ids) {
        if (other === id) continue;
        const q = pos.get(other)!;
        let dx = p.x - q.x, dy = p.y - q.y;
        let d = Math.hypot(dx, dy);
        if (d === 0) { dx = hash(id + other) - 0.5; dy = 0.5 - hash(other + id); d = Math.hypot(dx, dy) || 1; }
        if (d < 70) { const push = (70 - d) / 2 + 0.5; p.x += (dx / d) * push; p.y += (dy / d) * push; }
        // The overview labels every node, and labels run to the right of their node: two nodes on
        // nearly the same row closer than a label's width are pushed apart vertically.
        if (overview && Math.abs(dx) < LABEL_W && Math.abs(dy) < LABEL_H) {
          const push = (LABEL_H - Math.abs(dy)) / 2 + 0.5;
          p.y += (dy >= 0 ? 1 : -1) * push;
        }
      }
    }
  }
  return pos;
}
