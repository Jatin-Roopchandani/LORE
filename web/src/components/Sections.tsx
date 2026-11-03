import { Fragment } from "react";
import { citationSource, unwrittenCount, type AskState, type SectionView } from "../askReducer";
import { DEEP } from "../messages";
import { cap } from "../text";
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
    claimsRange: s.claims_range, reason: null, group: s.group ?? "", groupTitle: s.group_title ?? "", title: s.title ?? "" }));
}

function jump(e: React.MouseEvent<HTMLAnchorElement>, id: string) {
  const target = document.getElementById(id);
  if (!target) return;
  e.preventDefault();
  target.scrollIntoView?.({ block: "start" });
  const heading = /^H[1-6]$/.test(target.tagName) ? target : target.querySelector<HTMLElement>("h2, h3");
  heading?.focus({ preventScroll: true });
}

export interface Entry { index: number; heading: string; title: string; group: string; groupTitle?: string; kind: string }
export interface Outline {
  // With several parts, each top-level item is a part (its sections inside, numbered 1.1, 1.2) or a section on its
  // own (a part answered whole, the closing section). With one part there is no part level: sections only, 1..N.
  multi: boolean;
  tops: { num: string; group: string; groupTitle?: string; entry: Entry | null; items: { num: string; entry: Entry }[] }[];
}

// The contents' structure and numbers, from the entries in render order (hidden sections are already gone), so
// the numbers are sequential with no gaps and no repeats.
export function outline(entries: Entry[]): Outline {
  const groups = new Set(entries.filter((e) => e.group).map((e) => e.group));
  const loose = entries.some((e) => !e.group && e.kind !== "close");
  const multi = groups.size >= 2 || (groups.size === 1 && loose);
  const tops: Outline["tops"] = [];
  if (!multi) {
    entries.forEach((e, i) => tops.push({ num: String(i + 1), group: "", entry: e, items: [] }));
    return { multi, tops };
  }
  for (const e of entries) {
    const last = tops[tops.length - 1];
    if (e.group && last && last.group === e.group) {
      last.items.push({ num: `${last.num}.${last.items.length + 1}`, entry: e });
    } else if (e.group) {
      const num = String(tops.length + 1);
      tops.push({ num, group: e.group, groupTitle: e.groupTitle, entry: null, items: [{ num: `${num}.1`, entry: e }] });
    } else {
      tops.push({ num: String(tops.length + 1), group: "", entry: e, items: [] });
    }
  }
  return { multi, tops };
}

const partAnchor = (num: string) => `answer-part-${num}`;
// A subtitle (the full question) is shown only when the heading is not already the start of it.
const subtitleOf = (title: string | undefined, heading: string) =>
  title && !title.toLowerCase().startsWith(heading.replace(/\u2026$/, "").toLowerCase()) ? title : "";
// A top-level number reads "1.", a nested one "1.1".
const label = (num: string) => (num.includes(".") ? num : `${num}.`);

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
      return { index: p.index, heading: v?.heading || p.heading, title: v?.title || p.title || "",
        group: v?.group || p.group || "", groupTitle: v?.groupTitle || p.group_title || "", kind: v?.kind ?? "section" };
    }),
    ...views.filter((v) => !planned.some((p) => p.index === v.index))
      .map((v) => ({ index: v.index, heading: v.heading, title: v.title, group: v.group, groupTitle: v.groupTitle, kind: v.kind })),
  ].sort((a, b) => a.index - b.index);
  const { multi, tops } = outline(entries);
  const partNum = new Map(tops.filter((t) => t.group).map((t) => [t.group, t.num]));
  const numOf = new Map<number, string>();
  for (const t of tops) { if (t.entry) numOf.set(t.entry.index, t.num); for (const i of t.items) numOf.set(i.entry.index, i.num); }
  const unwritten = unwrittenCount(state);
  // One entry is one row: the number and the title side by side in one element ("1. Title", "1.1 Title").
  const link = (c: Entry, num: string) => (
    <li key={c.index} data-num={num}>
      <span className="entry">
        <span className="num">{label(num)}</span>{" "}
        {started.has(c.index)
          ? <a href={`#${anchor(c.index)}`} title={c.title || undefined} onClick={(e) => jump(e, anchor(c.index))}>{cap(c.heading)}</a>
          : <span className="pending" title={c.title || undefined}>{cap(c.heading)}</span>}
      </span>
    </li>
  );
  let lastGroup = "";
  return (
    <div className="sectioned">
      {entries.length > 0 && (
        <nav className="contents" aria-labelledby="answer-contents">
          <p className="contents-title" id="answer-contents">{DEEP.contents}</p>
          <ol>
            {tops.map((t) => t.entry
              ? link(t.entry, t.num)
              : (
                <li key={`p${t.num}`} className="part" data-num={t.num}>
                  <span className="entry">
                    <span className="num">{label(t.num)}</span>{" "}
                    {t.items.some((i) => started.has(i.entry.index))
                      ? <a href={`#${partAnchor(t.num)}`} title={t.groupTitle || undefined} onClick={(e) => jump(e, partAnchor(t.num))}>{cap(t.group)}</a>
                      : <span className="pending" title={t.groupTitle || undefined}>{cap(t.group)}</span>}
                  </span>
                  <ol>{t.items.map((i) => link(i.entry, i.num))}</ol>
                </li>
              ))}
          </ol>
        </nav>
      )}
      {views.map((v) => {
        // With several parts, a part's name heads its first section (h2) and its sections are h3 below it; a part
        // answered whole, or the closing section, is an h2 of its own. With one part there is no part heading.
        const num = partNum.get(v.group);
        const part = multi && v.group && v.group !== lastGroup && num ? (
          <div className="part-head">
            <h2 id={partAnchor(num)} className="part-heading" tabIndex={-1} title={v.groupTitle || undefined}>{cap(v.group)}</h2>
            {subtitleOf(v.groupTitle, v.group) && <p className="section-question">{v.groupTitle}</p>}
          </div>) : null;
        lastGroup = v.group;
        const Heading = multi && !v.group ? "h2" : "h3";
        const subtitle = subtitleOf(v.title, v.heading);
        return (
          <Fragment key={v.index}>
            {part}
            <section id={anchor(v.index)} className={`answer-section ${v.state}${Heading === "h2" ? " standalone" : ""}`}
              aria-labelledby={`${anchor(v.index)}-h`} data-num={numOf.get(v.index)}>
              <div className="section-head">
                <Heading id={`${anchor(v.index)}-h`} className={Heading === "h2" ? "part-heading section-heading" : "section-heading"}
                  tabIndex={-1} title={v.title || undefined}>{cap(v.heading)}</Heading>
                {subtitle && <p className="section-question">{subtitle}</p>}
              </div>
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
