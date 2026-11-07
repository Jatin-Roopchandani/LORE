import { useEffect, useRef } from "react";
import Graph from "graphology";
import Sigma from "sigma";
import { nearestEdge, nodeAt, type PickNode } from "../edgePick";
import { EDGE_LABEL_PX, frame, makeDrawEdgeLabel } from "../edgeLabel";
import { createNodeBorderProgram } from "@sigma/node-border";
import type { GraphView } from "../graphView";
import { drawnLabel, edgeOrder, estimateWidth, isLarge, NODE_LABEL_PX, nodeSizes } from "../graphSizing";
import { contentBounds } from "../graphMetrics";
import { worldSlot } from "../worlds";
import { prefersReducedMotion } from "../settings";
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

// A colour token at a lower opacity, for sigma (which takes rgba); a colour it can't read is returned as is.
export function withAlpha(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color.trim());
  if (!m) return color;
  const h = m[1].length === 3 ? m[1].split("").map((c) => c + c).join("") : m[1];
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return `rgba(${r},${g},${b},${alpha})`;
}

export function palette() {
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
// Bumping `focusRequest` glides the camera to the node `focusId` (once it is drawn).
export function GraphCanvas({ view, positions, onNodeClick, onEdgeClick, pinned = false, fitRequest = 0, focusId = null, focusRequest = 0 }:
  { view: GraphView; positions: Positions; onNodeClick: (id: string) => void; onEdgeClick?: (source: string, target: string) => void;
    pinned?: boolean; fitRequest?: number; focusId?: string | null; focusRequest?: number }) {
  const box = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma | null>(null);
  // The pill behind edge labels: the theme's panel colour, kept current by each render.
  const pillColor = useRef("#ffffff");
  const pillBorder = useRef("#cccccc");
  // The nodes whose edges always show their labels: the open entity, and the one under the pointer.
  const focusNodes = useRef(new Set<string>());
  const hoverNode = useRef<string | null>(null);
  const hotEdge = useRef<string | null>(null);
  // A large answer, and the colours its focus uses: the edges of the node in focus in the edge colour, the rest dimmed.
  const largeGraph = useRef(false);
  const edgeInk = useRef("#888888");
  const edgeDim = useRef("rgba(136,136,136,0.35)");
  const focusing = () => largeGraph.current && (focusNodes.current.size > 0 || hoverNode.current !== null);
  const inFocus = (node: string) => focusNodes.current.has(node) || hoverNode.current === node;
  // While focusing, the nodes in focus and their neighbours; every other node steps back (its name muted).
  const focusNear = useRef<Set<string> | null>(null);
  const labelMuted = useRef("#888888");
  const updateFocus = () => {
    if (!focusing()) { focusNear.current = null; return; }
    const g = graph.current, near = new Set<string>();
    for (const f of [...focusNodes.current, ...(hoverNode.current ? [hoverNode.current] : [])]) {
      if (!g.hasNode(f)) continue;
      near.add(f);
      g.forEachNeighbor(f, (m) => { near.add(m); });
    }
    focusNear.current = near;
  };
  const dimmed = (node: string) => !!focusNear.current && !focusNear.current.has(node);
  const graph = useRef(new Graph({ multi: false, type: "directed" }));
  const click = useRef(onNodeClick);
  click.current = onNodeClick;
  const edgeClick = useRef(onEdgeClick);
  edgeClick.current = onEdgeClick;
  const latest = useRef({ view, positions, pinned });
  latest.current = { view, positions, pinned };

  const measureCtx = useRef<CanvasRenderingContext2D | null>(null);
  const contentBox = () => {
    const s = sigma.current;
    if (!s) return null;
    measureCtx.current ??= document.createElement("canvas").getContext("2d");
    const ctx = measureCtx.current;
    if (ctx) ctx.font = `${NODE_LABEL_PX}px Public Sans`;
    const items: { x: number; y: number; r: number; w: number }[] = [];
    graph.current.forEachNode((_id, a) => items.push({ x: a.x, y: a.y, r: a.size, w: ctx ? ctx.measureText(String(a.label ?? "")).width : estimateWidth(String(a.label ?? "")) }));
    if (!items.length) return null;
    const b = contentBounds(items, s.getDimensions());
    return { x: [b.x0, b.x1] as [number, number], y: [b.y0, b.y1] as [number, number] };
  };

  const paint = () => {
    const g = graph.current;
    const { view: v, positions: pos, pinned: pin } = latest.current;
    const p = palette();
    const s = sigma.current;
    if (s && pin && !s.getCustomBBox()) s.setCustomBBox(s.getBBox());
    if (s && !pin && s.getCustomBBox()) s.setCustomBBox(null);
    const keepNodes = new Set(v.nodes.map((n) => n.id));
    g.forEachNode((id) => { if (!keepNodes.has(id)) g.dropNode(id); });
    // The world suffix and ring tell same-named entities apart only when the view really
    // shows two or more worlds; with one world they are noise.
    const manyWorlds = new Set(v.nodes.map((n) => n.world?.id).filter((w) => w != null)).size > 1;
    const sizes = nodeSizes(v);
    for (const raw of v.nodes) {
      const n = manyWorlds ? raw : { ...raw, world: undefined };
      const at = pos.get(n.id) ?? { x: 500, y: 350 };
      const dim = n.state === "dim";
      const seed = n.seedPart !== null;
      const color = dim ? p.faint : p.v(KIND_VAR[n.kind] ?? "--k-other");
      // In the all-worlds view a plain node wears a ring in its world's colour (a seed keeps its part ring).
      // The open entity wears an ink ring and a node on the highlighted path an accent one, over any other ring.
      const marked = !!n.selected || !!n.onPath;
      const ringed = seed || !!n.world || marked;
      const ringColor = n.selected ? p.ink : n.onPath ? p.v("--accent") : dim ? p.faint : seed ? p.v(`--part-${n.seedPart! % 4}`)
        : n.world ? p.v(`--world-${worldSlot(n.world.id)}`) : p.faint;
      const attrs = { x: at.x, y: -at.y, size: sizes.get(n.id) ?? 7, label: drawnLabel(n), color,
        ring: seed || marked ? 0.3 : n.world ? 0.28 : 0, gap: ringed ? 0.12 : 0, gapColor: p.panel,
        ringColor, zIndex: marked ? 5 : dim ? 0 : 1 };
      if (g.hasNode(n.id)) g.mergeNodeAttributes(n.id, attrs); else g.addNode(n.id, attrs);
    }
    const keepEdges = new Set(v.links.map((l) => l.id));
    g.forEachEdge((id) => { if (!keepEdges.has(id)) g.dropEdge(id); });
    // A large answer's quiet edges are thinner, and their labels wait for room (edgeLabel.ts: tidy).
    const large = isLarge(v);
    const edgeAttrs = new Map<string, Record<string, unknown>>();
    for (const l of edgeOrder(v)) {
      if (!g.hasNode(l.source) || !g.hasNode(l.target)) continue;
      const lit = !!l.onPath || !!l.picked;
      const color = l.onPath ? p.v("--accent") : l.picked || l.hot || l.state === "cited" ? p.glow
        : l.state === "shown" ? (l.part !== null ? p.v(`--part-${l.part % 4}`) : p.edge) : p.faint;
      const quiet = !lit && (l.state === "faint" || l.state === "cut");
      const attrs = { type: l.both ? "line" : "arrow", label: l.label, quiet, tidy: large && quiet, dense: large, color,
        size: lit ? 4.5 : l.hot ? 4 : l.state === "cited" ? 2.6 : (v.overview || large) && quiet ? 1 : 1.4,
        zIndex: lit ? 4 : l.hot ? 3 : l.state === "cited" ? 2 : l.state === "shown" ? 1 : 0 };
      edgeAttrs.set(l.id, attrs);
      if (g.hasEdge(l.id)) g.mergeEdgeAttributes(l.id, attrs);
      else if (!g.hasDirectedEdge(l.source, l.target)) g.addEdgeWithKey(l.id, l.source, l.target, attrs);
    }
    // Sigma draws edge labels in the graph's edge order, so a large graph keeps its edges in edgeOrder: the
    // cited ones' labels are placed first and get the best spots.
    if (large) {
      const want = [...edgeAttrs.keys()].filter((id) => g.hasEdge(id));
      if (g.edges().join("\n") !== want.join("\n")) {
        const ends = new Map(want.map((id) => [id, g.extremities(id)]));
        g.clearEdges();
        for (const id of want) { const [a, b] = ends.get(id)!; g.addEdgeWithKey(id, a, b, edgeAttrs.get(id)); }
      }
    }
    largeGraph.current = large;
    edgeInk.current = p.edge;
    edgeDim.current = withAlpha(p.faint, 0.35);
    // A large answer is framed on its dots and names, not just the node centres, so no name runs off the edge.
    if (s && !pin) s.setCustomBBox(isLarge(v) ? contentBox() : null);
    // Sigma's label grid hides labels that share a cell, and Sigma labels an edge only when both its ends
    // are labelled, so a hidden name also hid relationships: every view names every node.
    sigma.current?.setSetting("labelDensity", 100);
    sigma.current?.setSetting("labelGridCellSize", 30);
    sigma.current?.setSetting("labelColor", { attribute: "labelColor", color: p.ink });
    sigma.current?.setSetting("edgeLabelColor", { color: p.ink });
    pillColor.current = p.panel;
    pillBorder.current = p.v("--line");
    focusNodes.current = new Set(v.nodes.filter((n) => n.selected).map((n) => n.id));
    labelMuted.current = p.muted;
    updateFocus();
    sigma.current?.refresh();
  };

  useEffect(() => {
    const p = palette();
    sigma.current = new Sigma(graph.current, box.current!, {
      renderEdgeLabels: true, defaultEdgeType: "arrow", defaultNodeType: "ringed", nodeProgramClasses: { ringed: RingedNode }, labelFont: "Public Sans", labelSize: 12, edgeLabelSize: EDGE_LABEL_PX,
      edgeLabelFont: "Public Sans", edgeLabelWeight: "500", defaultDrawEdgeLabel: makeDrawEdgeLabel({
        pill: () => pillColor.current, border: () => pillBorder.current,
        zoom: () => sigma.current?.getCamera().ratio ?? 1,
        focus: inFocus, focusing, dimmed,
        hot: (edge) => hotEdge.current === edge }),
      // In a large answer, a node in focus (open, or under the pointer) brings its edges forward and dims the
      // other quiet ones; cited edges keep their glow.
      nodeReducer: (node, data) => (dimmed(node) ? { ...data, labelColor: labelMuted.current, zIndex: 0 } : data),
      edgeReducer: (edge, data) => {
        if (!focusing()) return data;
        const g = graph.current;
        if (!g.hasEdge(edge)) return data;
        if (inFocus(g.source(edge)) || inFocus(g.target(edge)))
          return { ...data, zIndex: 6, color: data.quiet ? edgeInk.current : data.color, size: Math.max(data.size ?? 1, 1.8) };
        return data.quiet ? { ...data, color: edgeDim.current, size: 0.6 } : data;
      },
      labelRenderedSizeThreshold: 0, labelColor: { attribute: "labelColor", color: p.ink }, edgeLabelColor: { color: p.ink }, zIndex: true,
      allowInvalidContainer: true, defaultDrawNodeHover: drawThemedHover, enableEdgeEvents: true,
    });
    sigma.current.on("clickNode", ({ node }) => click.current(node));
    sigma.current.on("clickEdge", ({ edge }) => {
      const g = graph.current;
      if (g.hasEdge(edge)) edgeClick.current?.(g.source(edge), g.target(edge));
    });
    // Near-miss clicks and hovers on nodes: the dot plus a margin, or its drawn label.
    const measure = document.createElement("canvas").getContext("2d");
    // Before each frame, the edge labels' obstacles: every node's dot and its name.
    sigma.current.on("beforeRender", () => {
      const s = sigma.current;
      frame.obstacles = [];
      if (!s) return;
      graph.current.forEachNode((id, attrs) => {
        if (attrs.hidden) return;
        const d = s.getNodeDisplayData(id);
        if (!d) return;
        const at = s.framedGraphToViewport({ x: d.x, y: d.y });
        const r = s.scaleSize(d.size);
        frame.obstacles.push({ x0: at.x - r - 3, y0: at.y - r - 3, x1: at.x + r + 3, y1: at.y + r + 3, owner: id });
        if (measure && attrs.label) {
          measure.font = "12px Public Sans";
          const w = measure.measureText(String(attrs.label)).width;
          frame.obstacles.push({ x0: at.x + r, y0: at.y - 9, x1: at.x + r + 10 + w * 1.08, y1: at.y + 9, owner: id });
        }
      });
    });
    const nearNode = (x: number, y: number): string | null => {
      const s = sigma.current;
      if (!s) return null;
      const shown = s.getNodeDisplayedLabels();
      const nodes: PickNode[] = [];
      graph.current.forEachNode((id, attrs) => {
        if (attrs.hidden) return;
        const d = s.getNodeDisplayData(id);
        if (!d) return;
        const at = s.framedGraphToViewport({ x: d.x, y: d.y });
        let labelW = 0;
        if (shown.has(id) && measure && attrs.label) { measure.font = "12px Public Sans"; labelW = measure.measureText(String(attrs.label)).width; }
        nodes.push({ id, x: at.x, y: at.y, r: s.scaleSize(d.size), labelW, labelH: 12 });
      });
      return nodeAt(x, y, nodes);
    };
    // Near-miss clicks and hovers: pick the closest edge within EDGE_PICK_PX.
    const near = (x: number, y: number): string | null => {
      const s = sigma.current;
      if (!s) return null;
      const g = graph.current;
      const at = (n: string) => { const d = s.getNodeDisplayData(n); return d ? s.framedGraphToViewport({ x: d.x, y: d.y }) : null; };
      const edges: { id: string; a: { x: number; y: number }; b: { x: number; y: number } }[] = [];
      g.forEachEdge((id, _attrs, src, tgt) => {
        if (g.getEdgeAttribute(id, "hidden")) return;
        const a = at(src), b = at(tgt);
        if (a && b) edges.push({ id, a, b });
      });
      return nearestEdge(x, y, edges);
    };
    let hovered: string | null = null;
    let hoveredSize = 0;
    const setHover = (edge: string | null) => {
      const g = graph.current;
      if (hovered === edge) return;
      if (hovered && g.hasEdge(hovered)) g.setEdgeAttribute(hovered, "size", hoveredSize);
      hovered = edge && g.hasEdge(edge) ? edge : null;
      hotEdge.current = hovered;
      if (hovered) { hoveredSize = g.getEdgeAttribute(hovered, "size") ?? 1.4; g.setEdgeAttribute(hovered, "size", hoveredSize + 2.5); }
      if (box.current) box.current.style.cursor = hovered ? "pointer" : "";
    };
    sigma.current.on("clickStage", ({ event }) => {
      const node = nearNode(event.x, event.y);
      if (node) { click.current(node); return; }
      const edge = near(event.x, event.y);
      const g = graph.current;
      if (edge && g.hasEdge(edge)) edgeClick.current?.(g.source(edge), g.target(edge));
    });
    sigma.current.on("moveBody", ({ event }) => {
      if (nearNode(event.x, event.y)) { setHover(null); if (box.current) box.current.style.cursor = "pointer"; return; }
      setHover(near(event.x, event.y));
    });
    sigma.current.on("enterNode", ({ node }) => { hoverNode.current = node; updateFocus(); sigma.current?.refresh(); });
    sigma.current.on("leaveNode", () => { hoverNode.current = null; updateFocus(); sigma.current?.refresh(); });
    sigma.current.on("enterNode", () => { setHover(null); if (box.current) box.current.style.cursor = "pointer"; });
    sigma.current.on("leaveNode", () => { if (box.current) box.current.style.cursor = ""; });
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
    s.setCustomBBox(isLarge(latest.current.view) ? contentBox() : null);
    s.getCamera().setState({ x: 0.5, y: 0.5, ratio: 1, angle: 0 });
    s.refresh();
    if (latest.current.pinned && !s.getCustomBBox()) s.setCustomBBox(s.getBBox());
  }, [fitRequest]);
  useEffect(() => {
    const s = sigma.current;
    if (!s || focusRequest === 0 || !focusId) return;
    // paint (an earlier effect) has refreshed sigma, so the node's display position is current.
    const at = s.getNodeDisplayData(focusId);
    if (!at) return;
    const ratio = Math.min(s.getCamera().ratio, 0.7);
    s.getCamera().animate({ x: at.x, y: at.y, ratio }, { duration: prefersReducedMotion() ? 0 : 300 });
  }, [focusRequest]);
  return <div ref={box} className="gcanvas" role="img" aria-label="Entities and relationships used for the answer" />;
}
