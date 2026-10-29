import { useEffect, useRef } from "react";
import Graph from "graphology";
import Sigma from "sigma";
import { createNodeBorderProgram } from "@sigma/node-border";
import type { GraphView } from "../graphView";
import type { Positions } from "../layout";

const KIND_VAR: Record<string, string> = { Character: "--k-character", Place: "--k-place", Faction: "--k-faction", Object: "--k-object",
  Concept: "--k-concept", Event: "--k-event" };

// Every node is filled by its kind. A seed also gets a ring in its part colour, set off from the fill by a
// thin gap in the panel colour (sizes are fractions of the radius; 0 draws nothing).
const RingedNode = createNodeBorderProgram({
  borders: [
    { size: { attribute: "ring", defaultValue: 0 }, color: { attribute: "ringColor" } },
    { size: { attribute: "gap", defaultValue: 0 }, color: { attribute: "gapColor" } },
    { size: { fill: true }, color: { attribute: "color" } },
  ],
});

function palette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  return { v, ink: v("--ink"), muted: v("--muted"), edge: v("--edge"), faint: v("--edge-faint"), glow: v("--glow"), panel: v("--panel") };
}

// Sigma's own hover box is always white while the label takes the theme's ink, so in dark mode the
// hovered name vanished (white on white). Same geometry as sigma's drawDiscNodeHover, in the theme's
// accent highlight: the panel colour sits too close to the dark canvas to show as a highlight.
type HoverData = { x: number; y: number; size: number; label?: string | null };
type HoverSettings = { labelSize: number; labelFont: string; labelWeight: string };
function drawThemedHover(ctx: CanvasRenderingContext2D, data: HoverData, settings: HoverSettings) {
  const p = palette();
  const size = settings.labelSize, pad = 2;
  ctx.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;
  ctx.fillStyle = p.v("--accent-soft");
  ctx.strokeStyle = p.v("--accent");
  ctx.lineWidth = 1.5;
  ctx.shadowOffsetX = 0;
  ctx.shadowOffsetY = 0;
  ctx.shadowBlur = 8;
  ctx.shadowColor = "rgba(0, 0, 0, 0.45)";
  if (typeof data.label === "string" && data.label) {
    const boxWidth = Math.round(ctx.measureText(data.label).width + 5);
    const boxHeight = Math.round(size + 2 * pad);
    const radius = Math.max(data.size, size / 2) + pad;
    const angle = Math.asin(boxHeight / 2 / radius);
    const dx = Math.sqrt(Math.abs(radius ** 2 - (boxHeight / 2) ** 2));
    ctx.beginPath();
    ctx.moveTo(data.x + dx, data.y + boxHeight / 2);
    ctx.lineTo(data.x + radius + boxWidth, data.y + boxHeight / 2);
    ctx.lineTo(data.x + radius + boxWidth, data.y - boxHeight / 2);
    ctx.lineTo(data.x + dx, data.y - boxHeight / 2);
    ctx.arc(data.x, data.y, radius, angle, -angle);
    ctx.closePath();
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.stroke();
  } else {
    ctx.beginPath();
    ctx.arc(data.x, data.y, data.size + pad, 0, Math.PI * 2);
    ctx.closePath();
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.stroke();
  }
  if (typeof data.label === "string" && data.label) {
    ctx.fillStyle = p.ink;
    ctx.fillText(data.label, data.x + data.size + 3, data.y + size / 3);
  }
}

// While `pinned` is false the camera fits the whole layout on every change (staging and the answer).
// Once it turns true (an expansion) the current framing is frozen before new nodes land, so nodes
// already on screen stay put. Bumping `fitRequest` refits once and re-pins if still pinned.
export function GraphCanvas({ view, positions, onNodeClick, pinned = false, fitRequest = 0 }:
  { view: GraphView; positions: Positions; onNodeClick: (id: string) => void; pinned?: boolean; fitRequest?: number }) {
  const box = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma | null>(null);
  const graph = useRef(new Graph({ multi: false, type: "directed" }));
  const click = useRef(onNodeClick);
  click.current = onNodeClick;
  const latest = useRef({ view, positions, pinned });
  latest.current = { view, positions, pinned };

  const paint = () => {
    const g = graph.current;
    const { view: v, positions: pos, pinned: pin } = latest.current;
    const p = palette();
    const s = sigma.current;
    if (s && pin && !s.getCustomBBox()) s.setCustomBBox(s.getBBox());
    if (s && !pin && s.getCustomBBox()) s.setCustomBBox(null);
    const keepNodes = new Set(v.nodes.map((n) => n.id));
    g.forEachNode((id) => { if (!keepNodes.has(id)) g.dropNode(id); });
    for (const n of v.nodes) {
      const at = pos.get(n.id) ?? { x: 500, y: 350 };
      const dim = n.state === "dim";
      const seed = n.seedPart !== null;
      const color = dim ? p.faint : p.v(KIND_VAR[n.kind] ?? "--k-other");
      const attrs = { x: at.x, y: -at.y, size: seed ? 11 : 7, label: n.label.replace(/^Mr\. /, ""), color,
        ring: seed ? 0.3 : 0, gap: seed ? 0.12 : 0, gapColor: p.panel,
        ringColor: seed && !dim ? p.v(`--part-${n.seedPart! % 4}`) : p.faint, zIndex: dim ? 0 : 1 };
      if (g.hasNode(n.id)) g.mergeNodeAttributes(n.id, attrs); else g.addNode(n.id, attrs);
    }
    const keepEdges = new Set(v.links.map((l) => l.id));
    g.forEachEdge((id) => { if (!keepEdges.has(id)) g.dropEdge(id); });
    for (const l of v.links) {
      if (!g.hasNode(l.source) || !g.hasNode(l.target)) continue;
      const color = l.hot || l.state === "cited" ? p.glow
        : l.state === "shown" ? (l.part !== null ? p.v(`--part-${l.part % 4}`) : p.edge) : p.faint;
      const attrs = { type: l.both ? "line" : "arrow", label: l.state === "faint" || l.state === "cut" ? "" : l.label, color,
        size: l.hot ? 4 : l.state === "cited" ? 2.6 : v.overview && l.state === "faint" ? 1 : 1.4, zIndex: l.hot ? 3 : l.state === "cited" ? 2 : l.state === "shown" ? 1 : 0 };
      if (g.hasEdge(l.id)) g.mergeEdgeAttributes(l.id, attrs);
      else if (!g.hasDirectedEdge(l.source, l.target)) g.addEdgeWithKey(l.id, l.source, l.target, attrs);
    }
    // Sigma's label grid hides labels that share a cell; the overview names every node, so it lifts the cap.
    sigma.current?.setSetting("labelDensity", v.overview ? 100 : 1);
    sigma.current?.setSetting("labelGridCellSize", v.overview ? 30 : 100);
    sigma.current?.setSetting("labelColor", { color: p.ink });
    sigma.current?.setSetting("edgeLabelColor", { color: p.muted });
    sigma.current?.refresh();
  };

  useEffect(() => {
    const p = palette();
    sigma.current = new Sigma(graph.current, box.current!, {
      renderEdgeLabels: true, defaultEdgeType: "arrow", defaultNodeType: "ringed", nodeProgramClasses: { ringed: RingedNode }, labelFont: "Public Sans", labelSize: 12, edgeLabelSize: 10,
      labelRenderedSizeThreshold: 0, labelColor: { color: p.ink }, edgeLabelColor: { color: p.muted }, zIndex: true,
      allowInvalidContainer: true, defaultDrawNodeHover: drawThemedHover,
    });
    sigma.current.on("clickNode", ({ node }) => click.current(node));
    paint();
    const obs = new MutationObserver(paint);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", paint);
    return () => { obs.disconnect(); mq.removeEventListener("change", paint); sigma.current?.kill(); sigma.current = null; };
    // paint reads everything through refs, so the mount-time closure stays current.
  }, []);
  useEffect(paint, [view, positions, pinned]);
  useEffect(() => {
    const s = sigma.current;
    if (!s || fitRequest === 0) return;
    s.setCustomBBox(null);
    s.getCamera().setState({ x: 0.5, y: 0.5, ratio: 1, angle: 0 });
    s.refresh();
    if (latest.current.pinned) s.setCustomBBox(s.getBBox());
  }, [fitRequest]);
  return <div ref={box} className="gcanvas" role="img" aria-label="Entities and relationships used for the answer" />;
}
