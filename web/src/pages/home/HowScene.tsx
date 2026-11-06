import { KeyboardEvent, useRef } from "react";
import { HOME } from "../../messages";
import { usePinProgress } from "./scroll";
import { Segs } from "./Segs";

const STEPS = HOME.steps.length;
const M = HOME.miniGraph;

// The scroll position inside the pinned section picks the step (the whole view holds still meanwhile).
export const stepAt = (progress: number) => Math.min(STEPS, 1 + Math.floor(progress * STEPS));

// "How LORE answers a question": five steps, pinned. A dot jumps to the middle of its step's stretch of scroll.
export function HowScene({ reduced }: { reduced: boolean }) {
  const pin = useRef<HTMLElement>(null);
  const dots = useRef<(HTMLButtonElement | null)[]>([]);
  const step = stepAt(usePinProgress(pin));
  const go = (n: number) => {
    const el = pin.current;
    if (!el) return;
    const top = el.getBoundingClientRect().top + window.scrollY, travel = el.offsetHeight - window.innerHeight;
    window.scrollTo({ top: top + travel * ((n - 0.5) / STEPS), behavior: reduced ? "instant" : "smooth" });
  };
  const keys = (e: KeyboardEvent, n: number) => {
    const to = e.key === "ArrowRight" || e.key === "ArrowDown" ? n + 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? n - 1
      : e.key === "Home" ? 1 : e.key === "End" ? STEPS : 0;
    if (to < 1 || to > STEPS) return;
    e.preventDefault();
    dots.current[to - 1]?.focus();
    go(to);
  };
  const now = HOME.steps[step - 1];
  return (
    <section id="how" className="h-pin" aria-labelledby="how-title" ref={pin}>
      <div className="h-pin-view h-band">
        <div className="h-pin-grid">
          <div className="h-pin-text">
            <h2 id="how-title">{HOME.howTitle}</h2>
            <p className="h-intro"><Segs parts={HOME.howIntro} /></p>
            <div className="h-pin-steps">
              {HOME.steps.map((s, i) => (
                <div key={s.n} className={`h-step${step === i + 1 ? " is-on" : ""}`} data-step={i + 1} aria-hidden={step !== i + 1}>
                  <div className="h-n">{s.n}</div><h3>{s.h}</h3><p>{s.p}</p>
                </div>
              ))}
            </div>
            <div className="h-pin-dots" role="group" aria-label={HOME.stepsLabel}>
              {HOME.steps.map((s, i) => (
                <button key={s.n} type="button" ref={(b) => { dots.current[i] = b; }} className={step === i + 1 ? "is-on" : ""}
                  aria-label={HOME.stepDot(i + 1)} aria-current={step === i + 1 ? "step" : undefined}
                  onClick={() => go(i + 1)} onKeyDown={(e) => keys(e, i + 1)} />
              ))}
            </div>
          </div>
          <div className="h-stage" data-step={step}>
            <div className="h-card">
              <p className="h-sr" aria-live="polite" data-testid="how-live">{HOME.stepNow(step, STEPS, `${now.n}. ${now.h}`)}</p>
              <ol className="h-progress" aria-label={HOME.progressLabel}>{HOME.progress.map((p) => <li key={p}>{p}</li>)}</ol>
              <div className="h-phase h-p1">
                <div className="h-q h-flush">{HOME.youAsked} <span>{HOME.howQuestion}</span></div>
              </div>
              <div className="h-phase h-p2">
                <div className="h-label">{HOME.graphLabel}</div>
                <svg className="h-mini" viewBox="0 0 560 190" preserveAspectRatio="xMidYMid meet" role="img" aria-label={HOME.miniGraphLabel}>
                  <line className="h-e h-path" x1="90" y1="110" x2="250" y2="60" /><line className="h-e h-path" x1="250" y1="60" x2="420" y2="110" />
                  <line className="h-e h-path" x1="90" y1="110" x2="200" y2="160" />
                  <line className="h-e" x1="90" y1="110" x2="40" y2="40" /><line className="h-e" x1="420" y1="110" x2="520" y2="160" /><line className="h-e" x1="420" y1="110" x2="500" y2="40" />
                  <circle cx="90" cy="110" r="9" fill="var(--n-char)" /><circle cx="250" cy="60" r="8" fill="var(--n-char)" /><circle cx="420" cy="110" r="10" fill="var(--n-char)" />
                  <circle cx="200" cy="160" r="7" fill="var(--n-place)" /><circle cx="40" cy="40" r="6" fill="var(--n-place)" /><circle cx="520" cy="160" r="6" fill="var(--n-place)" /><circle cx="500" cy="40" r="6" fill="var(--n-place)" />
                  <text x="70" y="136">{M.watson}</text><text x="232" y="44">{M.stamford}</text><text x="396" y="138">{M.holmes}</text>
                  <text x="212" y="176">{M.criterion}</text><text x="52" y="32">{M.afghanistan}</text><text x="452" y="178">{M.baker}</text><text x="452" y="30">{M.lab}</text>
                </svg>
              </div>
              <div className="h-phase h-p3">
                <div className="h-label">{HOME.fromBook}</div>
                {HOME.passages.map((p, i) => <div key={i} className="h-src"><Segs parts={p.text} /><small>{p.where}</small></div>)}
              </div>
              <div className="h-phase h-p4">
                <div className="h-label">{HOME.checkedLabel}</div>
                {HOME.checked.map((c, i) => <div key={i} className="h-checked"><b>{HOME.tick}</b><span><Segs parts={c} /></span></div>)}
                <div className="h-checked is-cut"><b>{HOME.cross}</b><span>{HOME.cutClaim}</span><small>{HOME.cutWhy}</small></div>
              </div>
              <div className="h-written" role="group" aria-label={HOME.writtenLabel}>
                <div className="h-q">{HOME.youAsked} <span>{HOME.howQuestion}</span></div>
                <div className="h-label">{HOME.shortAnswerLabel}</div>
                <p className="h-short"><Segs parts={HOME.shortAnswer} /></p>
                {HOME.written.map((p, i) => <p key={i}><Segs parts={p} /></p>)}
                <div className="h-reading"><em>{HOME.readingKind}</em>{HOME.reading}</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
