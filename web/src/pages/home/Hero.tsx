import type { MouseEvent } from "react";
import { HOME } from "../../messages";
import { Segs } from "./Segs";
import { goToForm } from "./session";

const G = HOME.heroGraph;
// The chapters 1-2 constellation: quiet edges, then the three that answer the question drawn in turn.
const EDGES = [
  [150, 250, 120, 110], [270, 120, 120, 110], [270, 120, 480, 110], [390, 260, 480, 110], [150, 250, 70, 370],
  [150, 250, 230, 380], [150, 250, 40, 290], [390, 260, 540, 340], [150, 250, 540, 340], [390, 260, 600, 210], [600, 210, 640, 90],
];
const HOT = [[150, 250, 270, 120], [270, 120, 390, 260], [390, 260, 150, 250]];
const NODES: [number, number, number, string][] = [
  [150, 250, 11, "var(--n-char)"], [270, 120, 8, "var(--n-char)"], [120, 110, 7, "var(--n-place)"], [480, 110, 7, "var(--n-place)"],
  [70, 370, 6, "var(--n-place)"], [230, 380, 6, "var(--n-place)"], [40, 290, 6, "var(--n-char)"], [540, 340, 8, "var(--n-place)"],
  [600, 210, 7, "var(--n-char)"], [640, 90, 6, "var(--n-group)"],
];
const LABELS: [number, number, string][] = [
  [96, 282, G.watson], [284, 112, G.stamford], [62, 98, G.criterion], [420, 96, G.lab], [82, 374, G.afghanistan],
  [242, 384, G.maiwand], [20, 316, G.murray], [554, 344, G.baker], [614, 214, G.lestrade], [600, 78, G.yard], [406, 286, G.holmes],
];
const TAGS: [number, number, string][] = [[148, 176, G.meetsAgain], [338, 180, G.introduces], [214, 244, G.sharesRooms]];

export function Hero({ signedIn, reduced }: { signedIn: boolean; reduced: boolean }) {
  const invite = (e: MouseEvent) => { e.preventDefault(); goToForm(reduced); };
  return (
    <header className="h-band"><div className="h-inner h-hero">
      <div>
        <span className="h-eyebrow">{HOME.eyebrow}</span>
        <h1>{HOME.headline}<br /><span>{HOME.headlineGlow}</span></h1>
        <p className="h-lede">{HOME.lede}</p>
        <div className="h-ctas">
          {signedIn
            ? <a className="h-btn is-primary" href="/app">{HOME.openLore}</a>
            : <a className="h-btn is-primary" href="#beta" onClick={invite}>{HOME.requestInvite}</a>}
          <a className="h-btn" href="#how">{HOME.seeHow}</a>
        </div>
      </div>
      <div className="h-hero-art">
        <svg className="h-constellation" viewBox="0 0 680 430" role="img" aria-label={HOME.heroGraphLabel}>
          {EDGES.map(([x1, y1, x2, y2], i) => <line key={i} className="h-e" x1={x1} y1={y1} x2={x2} y2={y2} />)}
          {HOT.map(([x1, y1, x2, y2], i) => <line key={`h${i}`} className={`h-e h-hot h-hot${i + 1}`} x1={x1} y1={y1} x2={x2} y2={y2} />)}
          {NODES.map(([cx, cy, r, fill], i) => <g key={i} className="h-node"><circle cx={cx} cy={cy} r={r} fill={fill} /></g>)}
          <g className="h-node h-lit"><circle cx="390" cy="260" r="13" fill="var(--spark)" /></g>
          {LABELS.map(([x, y, t]) => <text key={t} x={x} y={y}>{t}</text>)}
          {TAGS.map(([x, y, t], i) => <text key={t} className={`h-tag h-tag${i + 1}`} x={x} y={y}>{t}</text>)}
        </svg>
        <div className="h-card h-hero-answer">
          <div className="h-q">{HOME.youAsked} <span>{HOME.heroQuestion}</span></div>
          <p className="h-prose"><Segs parts={HOME.heroAnswer} /></p>
        </div>
      </div>
      <a className="h-scroll-cue" href="#how"><i /> {HOME.scrollCue}</a>
    </div></header>
  );
}
