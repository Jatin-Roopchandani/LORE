import { citationSource, type AskState, type SectionView } from "../askReducer";
import { DEEP } from "../messages";
import { Prose } from "./Prose";

const anchor = (index: number) => `answer-section-${index}`;

// The sections to show: what the stream has said, or, with no section events (an answer that arrived whole),
// the sections of the final result.
function viewsOf(state: AskState): SectionView[] {
  if (state.sections.length) return state.sections.filter((s) => s.state !== "empty");
  return (state.result?.sections ?? []).map((s, index) => ({
    index, kind: s.kind, heading: s.heading, state: "done" as const, style: s.style, text: s.text, prose: s.prose,
    claimsRange: s.claims_range, reason: null }));
}

function jump(e: React.MouseEvent<HTMLAnchorElement>, index: number) {
  const target = document.getElementById(anchor(index));
  if (!target) return;
  e.preventDefault();
  target.scrollIntoView?.({ block: "start" });
  const heading = target.querySelector<HTMLElement>("h3");
  heading?.focus({ preventScroll: true });
}

export function Sections({ state, onHoverChunks }: { state: AskState; onHoverChunks: (chunks: string[]) => void }) {
  const source = citationSource(state);
  const views = viewsOf(state);
  const started = new Set(views.map((v) => v.index));
  // Every section the plan promised, then any (the closing one) it did not: all listed, linked once written.
  const planned = (state.plan?.sections ?? []).filter((p) => !state.sections.some((v) => v.index === p.index && v.state === "empty"));
  const contents = [
    ...planned.map((p) => ({ index: p.index, heading: views.find((v) => v.index === p.index)?.heading ?? p.heading })),
    ...views.filter((v) => !planned.some((p) => p.index === v.index)).map((v) => ({ index: v.index, heading: v.heading })),
  ].sort((a, b) => a.index - b.index);
  const overview = state.overview ?? state.result?.overview ?? null;
  return (
    <div className="sectioned">
      {overview && overview.length > 0 && (
        <section className="overview" aria-labelledby="answer-overview">
          <h3 id="answer-overview">{DEEP.overview}</h3>
          <Prose result={source} prose={overview} showMissing={false} onHoverChunks={onHoverChunks} />
        </section>
      )}
      {contents.length > 0 && (
        <nav className="contents" aria-labelledby="answer-contents">
          <p className="contents-title" id="answer-contents">{DEEP.contents}</p>
          <ol>
            {contents.map((c) => (
              <li key={c.index}>
                {started.has(c.index)
                  ? <a href={`#${anchor(c.index)}`} onClick={(e) => jump(e, c.index)}>{c.heading}</a>
                  : <span className="pending">{c.heading}</span>}
              </li>
            ))}
          </ol>
        </nav>
      )}
      {views.map((v) => (
        <section key={v.index} id={anchor(v.index)} className={`answer-section ${v.state}`} aria-labelledby={`${anchor(v.index)}-h`}>
          <h3 id={`${anchor(v.index)}-h`} tabIndex={-1}>{v.heading}</h3>
          {v.state === "writing" && <p className="writing" role="status">{DEEP.writing}</p>}
          {v.state === "failed" && <p className="note">{DEEP.failed}</p>}
          {v.state === "skipped" && <p className="note">{DEEP.skipped[v.reason ?? "budget"]}</p>}
          {v.state === "done" && v.style === "claims" && (
            <>
              <p className="note">{DEEP.claimsOnly}</p>
              <SectionClaims view={v} state={state} onHoverChunks={onHoverChunks} />
            </>
          )}
          {v.state === "done" && v.style !== "claims" && (
            <Prose result={source} prose={v.prose} showMissing={false} onHoverChunks={onHoverChunks} />
          )}
        </section>
      ))}
    </div>
  );
}

// A section whose prose did not pass: its checked claims, each with its passage chip as in the evidence list.
function SectionClaims({ view, state, onHoverChunks }: { view: SectionView; state: AskState; onHoverChunks: (chunks: string[]) => void }) {
  const source = citationSource(state);
  const [a, b] = view.claimsRange ?? [0, 0];
  const claims = source.claims.slice(Math.max(0, a - 1), b).filter(Boolean);
  if (!claims.length) return <p className="section-claims">{view.text}</p>;
  // Each claim reads as one cited sentence, so the same chips, hover and passages as prose apply.
  const prose = claims.map((c, i) => ({ text: c.text, claims: [a + i], passages: [c.passage], new_paragraph: true }));
  return <Prose result={source} prose={prose} showMissing={false} onHoverChunks={onHoverChunks} />;
}
