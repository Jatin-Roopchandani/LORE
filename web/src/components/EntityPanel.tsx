import { useEffect, useId, useRef, useState } from "react";
import { api, isError } from "../api";
import { EXPLORE } from "../messages";
import type { Beat, Depth, EntityProfile, Relation, SearchHit, SubgraphData, Timeline } from "../types";
import { EntitySearch } from "./EntitySearch";
import { QuoteChip } from "./QuoteChip";
import { useLoad } from "./useLoad";

type Tab = "profile" | "timeline" | "relations" | "ask";
const TABS: Tab[] = ["profile", "timeline", "relations", "ask"];

export interface EntityPanelProps {
  id: string; world?: string; bookTitles?: Map<string, string>; seed?: boolean; busy?: boolean;
  onClose: () => void;
  /** Open another entity (and centre it in the graph). */
  onSelect: (id: string) => void;
  onExpand: (d: SubgraphData) => void;
  /** Open the edge view between this entity and another. */
  onOpenEdge: (a: string, b: string) => void;
  onAsk: (question: string, depth?: Depth) => void;
  onConnect: (from: { id: string; name: string }, to: SearchHit) => void;
}

export function EntityPanel({ id, world, bookTitles, seed = false, busy = false, onClose, onSelect, onExpand, onOpenEdge, onAsk, onConnect }: EntityPanelProps) {
  const [tab, setTab] = useState<Tab>("profile");
  const [expanding, setExpanding] = useState(false);
  const [problem, setProblem] = useState("");
  const uid = useId();
  const profile = useLoad<EntityProfile>(() => api.profile(id, world), [id, world]);
  const current = useRef<string | null>(id);
  useEffect(() => { current.current = id; return () => { current.current = null; }; }, [id]);
  const name = profile.status === "ok" ? profile.data.name : "";
  const expand = async () => {
    setExpanding(true); setProblem("");
    const r = await api.subgraph(id, world);
    if (current.current !== id) return; // closed, or moved to another entity
    setExpanding(false);
    if (isError(r)) setProblem(EXPLORE.expandFailed); else onExpand(r);
  };
  const move = (e: React.KeyboardEvent, i: number) => {
    const next = e.key === "ArrowRight" ? (i + 1) % TABS.length : e.key === "ArrowLeft" ? (i + TABS.length - 1) % TABS.length : -1;
    if (next < 0) return;
    e.preventDefault();
    setTab(TABS[next]);
    document.getElementById(`${uid}-tab-${TABS[next]}`)?.focus();
  };
  return (
    <aside className="side-card" aria-label={EXPLORE.panelLabel} onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
      <button className="close" type="button" aria-label={EXPLORE.close} onClick={onClose}>×</button>
      {profile.status === "error" && <p className="note">{profile.message}</p>}
      {profile.status === "loading" && <p className="hint">{EXPLORE.loading}</p>}
      {profile.status === "ok" && (<>
        <span className="kind">{[EXPLORE.kindLabel[profile.data.kind] ?? profile.data.kind, seed ? EXPLORE.startingEntity : null].filter(Boolean).join(" · ")}</span>
        <h3>{profile.data.name}</h3>
        <span className="mono">{profile.data.books.map((b) => bookTitles?.get(b) ?? b).join(", ") || "—"}</span>
        <div>
          <button className="btn" type="button" disabled={expanding} onClick={expand}>{expanding ? EXPLORE.expanding : EXPLORE.expand}</button>
          {problem && <span className="note">{` ${problem}`}</span>}
        </div>
      </>)}
      {profile.status === "ok" && (<>
        <div className="tabs" role="tablist" aria-label={EXPLORE.tabsLabel}>
          {TABS.map((t, i) => (
            <button key={t} id={`${uid}-tab-${t}`} role="tab" type="button" aria-selected={tab === t} aria-controls={`${uid}-panel`}
              tabIndex={tab === t ? 0 : -1} onClick={() => setTab(t)} onKeyDown={(e) => move(e, i)}>{EXPLORE.tabs[t]}</button>
          ))}
        </div>
        <div id={`${uid}-panel`} role="tabpanel" aria-labelledby={`${uid}-tab-${tab}`} className="tabpanel">
          {tab === "profile" && <ProfileTab id={id} world={world} profile={profile.data} bookTitles={bookTitles} />}
          {tab === "timeline" && <TimelineTab id={id} world={world} bookTitles={bookTitles} onSelect={onSelect} />}
          {tab === "relations" && <RelationsTab id={id} name={name} world={world} bookTitles={bookTitles} onOpenEdge={onOpenEdge} onConnect={onConnect} />}
          {tab === "ask" && <AskTab name={name} busy={busy} onAsk={onAsk} />}
        </div>
      </>)}
    </aside>
  );
}

function Quotes({ items, world, bookTitles }: { items: { quote: string; chunk_id: string; book_id: string; chapter: number }[]; world?: string; bookTitles?: Map<string, string> }) {
  return <>{items.map((q) => <QuoteChip key={`${q.chunk_id}:${q.quote}`} chunkId={q.chunk_id} quote={q.quote} bookId={q.book_id} chapter={q.chapter} world={world} bookTitles={bookTitles} />)}</>;
}

function ProfileTab({ id, world, profile, bookTitles }: { id: string; world?: string; profile: EntityProfile; bookTitles?: Map<string, string> }) {
  const empty = !profile.summary && profile.aspects.length === 0;
  return (
    <div className="stack">
      <span className="mono">{EXPLORE.counts(profile.counts)}</span>
      {profile.summary && <p className="summary">{profile.summary}</p>}
      {profile.aspects.map((a) => (
        <section key={a.aspect}>
          <h4>{EXPLORE.aspect(a.aspect)}</h4>
          <ul className="plain">
            {a.statements.map((s, i) => (
              <li key={i}>{s.text} <Quotes items={[s]} world={world} bookTitles={bookTitles} /></li>
            ))}
          </ul>
        </section>
      ))}
      {empty && <RawFacts id={id} world={world} bookTitles={bookTitles} />}
    </div>
  );
}

// An entity without a codex entry: the facts the graph holds about it, straight from its edges.
function RawFacts({ id, world, bookTitles }: { id: string; world?: string; bookTitles?: Map<string, string> }) {
  const card = useLoad((() => api.entity(id, world)), [id, world]);
  const edges = card.status === "ok" ? [...card.data.edges_out, ...card.data.edges_in].slice(0, 8) : [];
  return (
    <section>
      <p className="note">{EXPLORE.noProfile}</p>
      <p className="hint">{EXPLORE.noProfileHint}</p>
      <h4>{EXPLORE.rawFactsTitle}</h4>
      {card.status === "loading" && <p className="hint">{EXPLORE.loading}</p>}
      {card.status === "error" && <p className="note">{card.message}</p>}
      {card.status === "ok" && (edges.length === 0 ? <p className="hint">{EXPLORE.noFacts}</p> : (
        <ul className="plain">
          {edges.map((e, i) => (
            <li key={i}>{EXPLORE.edgeStatement(e.subject, e.predicate, e.object)}{" "}
              <QuoteChip chunkId={e.chunk_id} quote={e.evidence} bookId={e.book_id} chapter={e.chapter_seq} world={world} bookTitles={bookTitles} /></li>
          ))}
        </ul>
      ))}
    </section>
  );
}

// Beats in story order, grouped by book and then chapter.
function groupBeats(beats: Beat[]) {
  const books: { key: string; title: string; chapters: { chapter: number; beats: Beat[] }[] }[] = [];
  for (const b of beats) {
    let book = books[books.length - 1];
    if (!book || book.key !== b.book_id) { book = { key: b.book_id, title: b.book_title ?? b.book_id, chapters: [] }; books.push(book); }
    let ch = book.chapters[book.chapters.length - 1];
    if (!ch || ch.chapter !== b.chapter) { ch = { chapter: b.chapter, beats: [] }; book.chapters.push(ch); }
    ch.beats.push(b);
  }
  return books;
}

function TimelineTab({ id, world, bookTitles, onSelect }: { id: string; world?: string; bookTitles?: Map<string, string>; onSelect: (id: string) => void }) {
  const t = useLoad<Timeline>(() => api.timeline(id, world), [id, world]);
  if (t.status === "loading") return <p className="hint">{EXPLORE.loading}</p>;
  if (t.status === "error") return <p className="note">{t.message}</p>;
  if (!t.data.beats.length) return <p className="hint">{EXPLORE.noBeats}</p>;
  return (
    <div className="stack">
      {t.data.order_note && <p className="hint">{t.data.order_note}</p>}
      {groupBeats(t.data.beats).map((book) => (
        <section key={book.key}>
          <h4>{book.title}</h4>
          {book.chapters.map((ch) => (
            <div key={ch.chapter}>
              <h5>{EXPLORE.chapter(ch.chapter)}</h5>
              <ul className="plain">
                {ch.beats.map((b, i) => (
                  <li key={i}>
                    <span className="beat-kind">{EXPLORE.beatKind(b.kind)}</span> {b.text}{" "}
                    <QuoteChip chunkId={b.chunk_id} quote={b.quote} bookId={b.book_id} chapter={b.chapter} world={world} bookTitles={bookTitles} />
                    {b.others.length > 0 && (
                      <span className="others">{` ${EXPLORE.withOthers} `}
                        {b.others.map((o, j) => (
                          <span key={j}>{j > 0 && ", "}
                            {o.id ? <button className="linkbtn" type="button" aria-label={EXPLORE.openEntity(o.name)} onClick={() => onSelect(o.id!)}>{o.name}</button> : o.name}
                          </span>
                        ))}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}

function RelationsTab({ id, name, world, bookTitles, onOpenEdge, onConnect }:
  { id: string; name: string; world?: string; bookTitles?: Map<string, string>; onOpenEdge: (a: string, b: string) => void;
    onConnect: (from: { id: string; name: string }, to: SearchHit) => void }) {
  const r = useLoad(() => api.relations(id, world), [id, world]);
  const [picking, setPicking] = useState(false);
  return (
    <div className="stack">
      <div>
        {!picking ? (
          <button className="btn" type="button" aria-label={EXPLORE.connectLabel} onClick={() => setPicking(true)}>{EXPLORE.connect}</button>
        ) : (
          <div className="picker">
            <EntitySearch world={world} label={EXPLORE.connectPick} placeholder={EXPLORE.connectPick} exclude={id} autoFocus
              onPick={(hit) => { setPicking(false); onConnect({ id, name }, hit); }} />
            <button className="btn" type="button" onClick={() => setPicking(false)}>{EXPLORE.connectCancel}</button>
          </div>
        )}
      </div>
      {r.status === "loading" && <p className="hint">{EXPLORE.loading}</p>}
      {r.status === "error" && <p className="note">{r.message}</p>}
      {r.status === "ok" && (r.data.relations.length === 0 ? <p className="hint">{EXPLORE.noRelations}</p> : (<>
        {r.data.order_note && <p className="hint">{r.data.order_note}</p>}
        <ul className="plain rels">
          {r.data.relations.map((rel) => <RelationRow key={rel.other.id} rel={rel} from={name} world={world} bookTitles={bookTitles} onOpen={() => onOpenEdge(id, rel.other.id)} />)}
        </ul>
      </>))}
    </div>
  );
}

function RelationRow({ rel, from, world, bookTitles, onOpen }: { rel: Relation; from: string; world?: string; bookTitles?: Map<string, string>; onOpen: () => void }) {
  const span = rel.first_chapter && rel.last_chapter
    ? EXPLORE.chapterSpan(EXPLORE.chapter(rel.first_chapter.chapter), EXPLORE.chapter(rel.last_chapter.chapter)) : "";
  return (
    <li>
      <button className="rel" type="button" aria-label={EXPLORE.showEdge(from, rel.other.name)} onClick={onOpen}>
        <strong>{rel.other.name}</strong> <span className="kind">{EXPLORE.kindLabel[rel.other.kind] ?? rel.other.kind}</span>
        <span className="preds">{rel.predicates.map((p) => <span key={`${p.predicate}:${p.direction}`} className="pred">{EXPLORE.predicateLabel(p)}</span>)}</span>
        {span && <span className="mono">{span}</span>}
      </button>
      {rel.evidence.map((q) => (
        <div key={`${q.chunk_id}:${q.quote}`} className="ev"><span className="qtext">{`“${q.quote}”`}</span>{" "}
          <QuoteChip chunkId={q.chunk_id} quote={q.quote} bookId={q.book_id} chapter={q.chapter} world={world} bookTitles={bookTitles} /></div>
      ))}
    </li>
  );
}

/** The one-click questions: each is sent through the ordinary ask path and uses the daily allowance. */
export function AskButtons({ questions, busy, onAsk }: { questions: { label: string; depth?: Depth }[]; busy: boolean; onAsk: (q: string, depth?: Depth) => void }) {
  return (
    <div className="stack">
      <p className="hint">{EXPLORE.askIntro}</p>
      <ul className="plain asks">
        {questions.map((q) => (
          <li key={q.label}>
            <button className="btn" type="button" disabled={busy} title={busy ? EXPLORE.askBusy : q.depth === "deep" ? EXPLORE.askCostDeep : EXPLORE.askCost}
              onClick={() => onAsk(q.label, q.depth)}>{q.label}</button>
          </li>
        ))}
      </ul>
      <p className="hint">{busy ? EXPLORE.askBusy : EXPLORE.askCost}</p>
    </div>
  );
}

function AskTab({ name, busy, onAsk }: { name: string; busy: boolean; onAsk: (q: string, depth?: Depth) => void }) {
  return <AskButtons busy={busy} onAsk={onAsk} questions={[
    { label: EXPLORE.deepDive(name), depth: "deep" }, { label: EXPLORE.change(name) }, { label: EXPLORE.role(name) }]} />;
}
