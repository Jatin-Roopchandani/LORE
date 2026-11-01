import { Fragment } from "react";
import { citationSource, unwrittenCount, type AskState, type SectionView } from "../askReducer";
import { DEEP } from "../messages";
import { Prose } from "./Prose";

const anchor = (index: number) => `answer-section-${index}`;

// The sections to show: what the stream has said, or, with no section events (an answer that arrived whole),
// the sections of the final result.
// A section that failed or found nothing is neither listed nor shown: the answer says how many there were, once.
const written = (v: SectionView) => v.state !== "failed" && v.state !== "empty";
function viewsOf(state: AskState): SectionView[] {
  if (state.sections.length) return state.sections.filter(written);
  return (state.result?.sections ?? []).map((s, index) => ({
    index, kind: s.kind, heading: s.heading, state: "done" as const, style: s.style, text: s.text, prose: s.prose,
    claimsRange: s.claims_range, reason: null, group: s.group ?? "" }));
}

function jump(e: React.MouseEvent<HTMLAnchorElement>, index: number) {
  const target = document.getElementById(anchor(index));
  if (!target) return;
  e.preventDefault();
  target.scrollIntoView?.({ block: "start" });
  const heading = target.querySelector<HTMLElement>("h3, h4");
  heading?.focus({ preventScroll: true });
}

interface Entry { index: number; heading: string; group: string }

// The contents list: consecutive entries of one part sit under that part's name.
function contentsBlocks(entries: Entry[]): { group: string; items: Entry[] }[] {
  const blocks: { group: string; items: Entry[] }[] = [];
  for (const e of entries) {
    const last = blocks[blocks.length - 1];
    if (last && last.group === e.group && e.group) last.items.push(e);
    else blocks.push({ group: e.group, items: [e] });
  }
  return blocks;
}

export function Sections({ state, onHoverChunks }: { state: AskState; onHoverChunks: (chunks: string[]) => void }) {
  const source = citationSource(state);
  const views = viewsOf(state);
  const started = new Set(views.map((v) => v.index));
  const gone = new Set(state.sections.filter((v) => !written(v)).map((v) => v.index));
  // Every section the plan promised (minus any that came back failed or empty), then any (the closing one) it did
  // not: all listed, linked once written.
  const planned = (state.plan?.sections ?? []).filter((p) => !gone.has(p.index));
  const entries: Entry[] = [
    ...planned.map((p) => {
      const v = views.find((x) => x.index === p.index);
      return { index: p.index, heading: v?.heading ?? p.heading, group: v?.group ?? p.group ?? "" };
    }),
    ...views.filter((v) => !planned.some((p) => p.index === v.index)).map((v) => ({ index: v.index, heading: v.heading, group: v.group })),
  ].sort((a, b) => a.index - b.index);
  const overview = state.overview ?? state.result?.overview ?? null;
  const unwritten = unwrittenCount(state);
  const item = (c: Entry) => (
    <li key={c.index}>
      {started.has(c.index)
        ? <a href={`#${anchor(c.index)}`} onClick={(e) => jump(e, c.index)}>{c.heading}</a>
        : <span className="pending">{c.heading}</span>}
    </li>
  );
  let lastGroup = "";
  return (
    <div className="sectioned">
      {overview && overview.length > 0 && (
        <section className="overview" aria-labelledby="answer-overview">
          <h3 id="answer-overview">{DEEP.overview}</h3>
          <Prose result={source} prose={overview} showMissing={false} onHoverChunks={onHoverChunks} />
        </section>
      )}
      {entries.length > 0 && (
        <nav className="contents" aria-labelledby="answer-contents">
          <p className="contents-title" id="answer-contents">{DEEP.contents}</p>
          <ol>
            {contentsBlocks(entries).map((b) => b.group
              ? <li key={`g${b.items[0].index}`} className="group"><span>{b.group}</span><ol>{b.items.map(item)}</ol></li>
              : b.items.map(item))}
          </ol>
        </nav>
      )}
      {views.map((v) => {
        // A part's name heads its first section; its lens sections are one level down.
        const heading = v.group !== lastGroup && v.group ? <h3 className="group-heading">{v.group}</h3> : null;
        lastGroup = v.group;
        const Heading = v.group ? "h4" : "h3";
        return (
          <Fragment key={v.index}>
            {heading}
            <section id={anchor(v.index)} className={`answer-section ${v.state}`} aria-labelledby={`${anchor(v.index)}-h`}>
              <Heading id={`${anchor(v.index)}-h`} tabIndex={-1}>{v.heading}</Heading>
              {v.state === "writing" && <p className="writing" role="status">{DEEP.writing}</p>}
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
          </Fragment>
        );
      })}
      {unwritten > 0 && <p className="unwritten">{DEEP.unwritten(unwritten)}</p>}
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
