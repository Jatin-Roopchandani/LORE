import type { GraphView } from "./graphView";

export type Positions = Map<string, { x: number; y: number }>;
const CX = 500, CY = 350;

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0) / 4294967296;
}

export function place(previous: Positions, view: GraphView, anchor: string | null = null): Positions {
  const pos: Positions = new Map(previous);
  const fresh = view.nodes.filter((n) => !pos.has(n.id));
  const seeds = fresh.filter((n) => n.seedPart !== null);
  seeds.forEach((n, i) => {
    const a = (n.seedPart ?? 0) * 0.6 + (2 * Math.PI * i) / Math.max(1, seeds.length);
    pos.set(n.id, { x: CX + 220 * Math.cos(a), y: CY + 220 * Math.sin(a) });
  });
  const neighbours = new Map<string, string[]>();
  for (const l of view.links) {
    neighbours.set(l.source, [...(neighbours.get(l.source) ?? []), l.target]);
    neighbours.set(l.target, [...(neighbours.get(l.target) ?? []), l.source]);
  }
  const others = fresh.filter((n) => n.seedPart === null);
  for (const n of others) {
    const near = (neighbours.get(n.id) ?? []).find((m) => pos.has(m)) ?? anchor ?? null;
    const c = (near && pos.get(near)) || { x: CX, y: CY };
    const a = 2 * Math.PI * hash(n.id);
    pos.set(n.id, { x: c.x + 140 * Math.cos(a), y: c.y + 140 * Math.sin(a) });
  }
  const movable = new Set(others.map((n) => n.id));
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
      }
    }
  }
  return pos;
}
