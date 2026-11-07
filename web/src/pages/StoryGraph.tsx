import "./StoryGraph.css";
import { AUTH, HOME } from "../messages";

// The constellation of the homepage hero and the sign-in pages: chapters 1-2 of A Study in Scarlet (spoiler-free), laid out
// mirror-symmetric about the vertical axis. Watson and Holmes are a pair at the centre with Stamford, who
// introduces them, above. Watson's world fans out on the left and Holmes's on the right; 221B Baker Street,
// which they share, sits at the bottom between them. With a `label` it is an image with that description (the
// homepage); without one it is decorative and the caller hides it from assistive technology (the sign-in pages).
const G = HOME.heroGraph;
const CX = 300, CY = 270, R = 205;

type Node = { id: string; x: number; y: number; r: number; fill: string; label: string; anchor: "start" | "middle" | "end"; lx: number; ly: number; ring?: boolean };

function outer(id: string, deg: number, label: string, fill: string): Node {
  const a = (deg * Math.PI) / 180, x = CX + R * Math.cos(a), y = CY + R * Math.sin(a);
  const c = Math.cos(a), s = Math.sin(a);
  const anchor = c > 0.25 ? "start" : c < -0.25 ? "end" : "middle";
  return { id, x, y, r: 6.5, fill, label, anchor, lx: x + c * 15, ly: y + s * 15 + (Math.abs(c) <= 0.25 ? (s > 0 ? 14 : -6) : 5), ring: true };
}

const CHAR = "var(--n-char)", PLACE = "var(--n-place)", GROUP = "var(--n-group)", IDEA = "var(--spark)";
// Left (Watson's world) mirrors right (Holmes's world) angle for angle.
const NODES: Node[] = [
  { id: "stamford", x: CX, y: 150, r: 9, fill: CHAR, label: G.stamford, anchor: "middle", lx: CX, ly: 128 },
  { id: "watson", x: 212, y: 290, r: 12, fill: CHAR, label: G.watson, anchor: "middle", lx: 212, ly: 322 },
  { id: "holmes", x: 388, y: 290, r: 12, fill: IDEA, label: G.holmes, anchor: "middle", lx: 388, ly: 322 },
  outer("criterion", -120, G.criterion, PLACE),
  outer("afghanistan", -160, G.afghanistan, PLACE),
  outer("maiwand", 160, G.maiwand, PLACE),
  outer("murray", 120, G.murray, CHAR),
  outer("lab", -60, G.lab, PLACE),
  outer("bookoflife", -20, AUTH.graphBookOfLife, GROUP),
  outer("lestrade", 20, G.lestrade, CHAR),
  outer("yard", 60, G.yard, GROUP),
  outer("baker", 90, G.baker, PLACE),
];
const at = Object.fromEntries(NODES.map((n) => [n.id, n]));

// Quiet links, in mirrored pairs so the picture balances.
const QUIET: [string, string][] = [
  ["watson", "criterion"], ["holmes", "lab"],
  ["watson", "afghanistan"], ["holmes", "bookoflife"],
  ["watson", "maiwand"], ["holmes", "lestrade"],
  ["watson", "murray"], ["holmes", "yard"],
  ["maiwand", "murray"], ["lestrade", "yard"],
  ["stamford", "criterion"], ["stamford", "lab"],
  ["watson", "baker"], ["holmes", "baker"],
];
// The three that tell how the two met, drawn in turn, then carrying a travelling spark.
const HOT: { from: string; to: string; tag: string; tx: number; ty: number; anchor: "start" | "middle" | "end" }[] = [
  { from: "watson", to: "stamford", tag: G.meetsAgain, tx: 244, ty: 210, anchor: "end" },
  { from: "stamford", to: "holmes", tag: G.introduces, tx: 356, ty: 210, anchor: "start" },
  { from: "holmes", to: "watson", tag: G.sharesRooms, tx: CX, ty: 276, anchor: "middle" },
];

const line = (a: string, b: string) => ({ x1: at[a].x, y1: at[a].y, x2: at[b].x, y2: at[b].y });

export function StoryGraph({ label }: { label?: string }) {
  return (
    <svg className="ag" viewBox="0 0 600 540" preserveAspectRatio="xMidYMid meet"
      {...(label ? { role: "img", "aria-label": label } : { "aria-hidden": true })}>
      <defs>
        <radialGradient id="ag-glow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="rgb(120 174 234 / .22)" />
          <stop offset="60%" stopColor="rgb(120 174 234 / .05)" />
          <stop offset="100%" stopColor="rgb(120 174 234 / 0)" />
        </radialGradient>
      </defs>
      <circle className="ag-halo" cx={CX} cy={CY} r={R + 40} fill="url(#ag-glow)" />
      <g className="ag-orbits">
        <circle className="ag-orbit ag-orbit-outer" cx={CX} cy={CY} r={R} />
        <circle className="ag-orbit ag-orbit-inner" cx={CX} cy={CY} r={128} />
      </g>
      <g className="ag-quiet">
        {QUIET.map(([a, b], i) => <line key={`${a}-${b}`} className={`ag-q${i >> 1}`} pathLength={1} {...line(a, b)} />)}
      </g>
      {HOT.map((h, i) => (
        <g key={h.from} className={`ag-hot ag-hot${i + 1}`}>
          <line className="ag-hot-line" pathLength={1} {...line(h.from, h.to)} />
          <line className="ag-flow" pathLength={1} {...line(h.from, h.to)} />
        </g>
      ))}
      <g className="ag-nodes">
        {NODES.map((n, i) => (
          <g key={n.id} className={`ag-node${n.ring ? ` ag-outer ag-n${i % 4}` : ""}${n.id === "holmes" ? " ag-lit" : ""}`}>
            <circle className={`ag-dot ag-t${i % 6}`} cx={n.x} cy={n.y} r={n.r} fill={n.fill} />
          </g>
        ))}
      </g>
      <g className="ag-labels">
        {NODES.map((n) => <text key={n.id} className={n.ring ? "" : "ag-main"} x={n.lx} y={n.ly} textAnchor={n.anchor}>{n.label}</text>)}
      </g>
      {HOT.map((h, i) => <text key={h.tag} className={`ag-tag ag-tag${i + 1}`} x={h.tx} y={h.ty} textAnchor={h.anchor}>{h.tag}</text>)}
    </svg>
  );
}
