import "./auth.css";
import type { ReactNode } from "react";
import { AUTH, HOME } from "../messages";
import { AlertIcon, Spinner } from "./authIcons";
import { EDGES, HOT, LABELS, NODES, TAGS } from "./constellation";
import { Logo } from "./Logo";
import { Segs } from "./home/Segs";

// The frame of the sign-in, waitlist and continue-signing-in pages: the homepage's night sky on the left (logo, the
// promise, the chapters 1-2 constellation drawing itself in and one answer), the form on the right. On a phone the
// sky shrinks to a strip with the logo and the first line.
// `mark` false drops the small logo above the heading, for a page that leads with its own icon instead.
export function AuthShell({ children, mark = true }: { children: ReactNode; mark?: boolean }) {
  return (
    <div className="au">
      <div className="au-brand">
        <a className="au-wordmark" href="/" aria-label={AUTH.homeLabel}>
          <Logo size={32} />
          <b>{AUTH.brand}</b><small>{AUTH.brandTag}</small>
        </a>
        <p className="au-line">{AUTH.brandLine} <span>{AUTH.brandLineGlow}</span></p>
        {/* A picture of the product, not content: hidden from assistive technology so the form comes first. */}
        <div className="au-art" aria-hidden="true">
          <svg className="au-graph" viewBox="0 0 680 430">
            <g className="au-quiet">{EDGES.map(([x1, y1, x2, y2], i) => <line key={i} pathLength={1} x1={x1} y1={y1} x2={x2} y2={y2} />)}</g>
            {HOT.map(([x1, y1, x2, y2], i) => <line key={i} className={`au-hot au-hot${i + 1}`} pathLength={1} x1={x1} y1={y1} x2={x2} y2={y2} />)}
            <g className="au-nodes">{NODES.map(([cx, cy, r, fill], i) => <circle key={i} cx={cx} cy={cy} r={r} fill={fill} />)}</g>
            <circle className="au-lit" cx="390" cy="260" r="13" fill="var(--spark)" />
            <g className="au-labels">{LABELS.map(([x, y, t]) => <text key={t} x={x} y={y}>{t}</text>)}</g>
            {TAGS.map(([x, y, t], i) => <text key={t} className={`au-tag au-tag${i + 1}`} x={x} y={y}>{t}</text>)}
          </svg>
          <div className="au-answer">
            <div className="au-q">{HOME.youAsked} <span>{HOME.heroQuestion}</span></div>
            <p><Segs parts={HOME.heroAnswer} /></p>
          </div>
        </div>
        <p className="au-caption">{AUTH.brandCaption}</p>
      </div>
      <main className="au-main">
        <div className="au-col">
          {mark && <a className="au-mark" href="/" aria-label={AUTH.homeLabel}><Logo size={28} /></a>}
          {children}
        </div>
      </main>
    </div>
  );
}

export function Alert({ children }: { children: ReactNode }) {
  return <div className="au-alert" role="alert"><AlertIcon /><div>{children}</div></div>;
}

// The page's main action: full width, with a spinner and a busy label while it works.
export function PrimaryButton({ busy = false, busyLabel, children, disabled = false, big = false }:
  { busy?: boolean; busyLabel?: string; children: ReactNode; disabled?: boolean; big?: boolean }) {
  return (
    <button className={`au-primary${big ? " is-big" : ""}`} type="submit" disabled={busy || disabled} aria-busy={busy || undefined}>
      {busy ? <><Spinner />{busyLabel}</> : children}
    </button>
  );
}

export function AgreeLine() {
  return (
    <p className="au-fine">
      {AUTH.agreeBefore}<a href="/terms">{AUTH.termsLink}</a>{AUTH.agreeAnd}<a href="/privacy">{AUTH.privacyLink}</a>{AUTH.agreeAfter}
    </p>
  );
}
