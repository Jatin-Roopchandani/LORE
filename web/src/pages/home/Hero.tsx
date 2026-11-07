import type { MouseEvent } from "react";
import { HOME } from "../../messages";
import { Segs } from "./Segs";
import { goToForm } from "./session";
import { EDGES, HOT, LABELS, NODES, TAGS } from "../constellation";

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
