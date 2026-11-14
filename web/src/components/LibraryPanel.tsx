import { useCallback, useEffect, useRef, useState } from "react";
import { api, isError } from "../api";
import { LIBRARY, PROVIDERS, WORLDS } from "../messages";
import type { LibraryBook, LibraryStatus, StepPlan, World } from "../types";

const POLL_MS = 3000;
const TICK_MS = 30000;
const START_CYCLES = 10; // polls forced after Continue, in case the first GET predates the worker

const clock = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

function Row({ book, now, busy, processing = false, error, focus = false, onGo, onMove, onDelete, children }: { book: LibraryBook; now: number; busy: boolean; processing?: boolean; error?: string; focus?: boolean; onGo: () => void; onMove: () => void; onDelete: () => Promise<boolean>; children?: React.ReactNode }) {
  const ref = useRef<HTMLLIElement>(null);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  useEffect(() => { if (focus) ref.current?.scrollIntoView?.({ block: "nearest" }); }, [focus]);
  const job = book.job;
  const meta = [book.author, book.series && (book.series_index != null ? `${book.series} #${book.series_index}` : book.series)].filter(Boolean).join(" · ");
  const btn = (verb: string, disabled = false) => (
    <button className="tbtn" type="button" disabled={disabled || busy} aria-label={LIBRARY.action(verb, book.title)} onClick={onGo}>{verb}</button>
  );
  let body: React.ReactNode;
  if (job?.state === "running" && job.total <= 1) {
    // No real count (a one-step stage, or a load that hasn't counted its mentions yet): an indeterminate bar, never a
    // frozen 0 / 1. Reduce-motion stops the animation (theme.css).
    body = (<>
      <span className="lib-state">{job.provider
        ? LIBRARY.stageOn(job.stage, PROVIDERS.names[job.provider] ?? job.provider) : LIBRARY.stage(job.stage)}</span>
      <div className="lib-bar indeterminate" role="progressbar" aria-label={LIBRARY.progressLabel(job.stage)} aria-busy="true">
        <span />
      </div>
      <span className="hint">{LIBRARY.slowStep}</span>
    </>);
  } else if (job?.state === "running") {
    const pct = job.total > 0 ? Math.min(100, Math.round((job.done / job.total) * 100)) : 0;
    body = (<>
      <span className="lib-state">{job.provider
        ? LIBRARY.stageOn(job.stage, PROVIDERS.names[job.provider] ?? job.provider) : LIBRARY.stage(job.stage)}</span>
      <div className="lib-bar" role="progressbar" aria-label={LIBRARY.progressLabel(job.stage)} aria-valuemin={0} aria-valuemax={job.total} aria-valuenow={job.done}>
        <span style={{ width: `${pct}%` }} />
      </div>
      <span className="hint">{LIBRARY.progress(job.done, job.total)}</span>
    </>);
  } else if (job?.state === "waiting_quota" || job?.state === "idle" || job?.state === "waiting_key") {
    const at = job.resumable_at ? new Date(job.resumable_at) : null;
    const left = at ? Math.max(0, Math.ceil((at.getTime() - now) / 60000)) : 0;
    const enabled = at ? now >= at.getTime() : job.can_continue;
    body = (<>
      <span className="lib-state">{LIBRARY.paused}</span>
      <span className="hint">{job.stopped_reason || (job.state === "waiting_key" ? LIBRARY.pausedKey : LIBRARY.pausedDefault)}</span>
      {at && !enabled && <span className="hint">{LIBRARY.continuesAfter(clock(at), LIBRARY.inHM(left))}</span>}
      {btn(LIBRARY.continue, !enabled)}
    </>);
  } else if (job?.state === "failed") {
    body = (<>
      <span className="lib-state">{LIBRARY.failed}</span>
      <span className="hint" role="alert">{job.stopped_reason || LIBRARY.failedDefault}</span>
      {btn(LIBRARY.retry)}
    </>);
  } else if (job?.state === "done" || (!job && book.status === "graphed")) {
    body = <span className="lib-state">{LIBRARY.ready}</span>;
  } else {
    body = (<><span className="lib-state">{LIBRARY.unprocessed}</span>{btn(LIBRARY.process)}</>);
  }
  return (
    <li ref={ref} className={`lib-row${focus ? " focus" : ""}`} aria-current={focus ? "true" : undefined}>
      <div className="lib-title"><strong>{book.title}</strong>
        {book.is_sample && <span className="tag sample-tag" title={LIBRARY.sampleTagHint}>{LIBRARY.sampleTag}</span>}
        {meta && <span className="meta">{meta}</span>}</div>
      <div className="lib-body">
        {body}
        <button className="tbtn" type="button" disabled={busy || job?.state === "running"} aria-label={WORLDS.moveOpenLabel(book.title)} onClick={onMove}>{WORLDS.moveOpen}</button>
        {!confirming && (
          <button className="tbtn" type="button" disabled={busy || processing} aria-label={LIBRARY.deleteBookLabel(book.title)}
            onClick={() => setConfirming(true)}>{LIBRARY.deleteBook}</button>
        )}
        {processing && !confirming && <span className="hint">{LIBRARY.deleteBookBusy}</span>}
      </div>
      {confirming && (
        <div className="lib-wform" role="group" aria-label={LIBRARY.deleteBookLabel(book.title)}>
          <span className="hint">{LIBRARY.deleteBookConfirm(book.title)}</span>
          <button className="tbtn primary" type="button" disabled={deleting || processing} onClick={async () => {
            setDeleting(true);
            const ok = await onDelete();
            setDeleting(false);
            if (!ok) setConfirming(false);
          }}>{deleting ? LIBRARY.deletingBook : LIBRARY.deleteBookConfirmButton}</button>
          <button className="tbtn" type="button" disabled={deleting} onClick={() => setConfirming(false)}>{LIBRARY.deleteBookCancel}</button>
        </div>
      )}
      {job?.state === "running" && job.plan && <Steps plan={job.plan} />}
      {children}
      {error && <p className="hint" role="alert">{error}</p>}
    </li>
  );
}

// The whole pipeline under a running book, collapsed to "Step 4 of 6 · Writing entity profiles · ~50 min left"; the
// disclosure lists every step: done ones ticked, the current one with its live count, upcoming ones greyed out.
// The list comes from the server (the stages it will really run); only the labels live here. No animation.
function Steps({ plan }: { plan: StepPlan }) {
  const current = plan.steps.find((s) => s.state === "current");
  const label = current ? LIBRARY.stage(current.stage) : "";
  return (
    <details className="lib-steps">
      <summary>{LIBRARY.stepOf(plan.step, plan.of, label, LIBRARY.left(plan.seconds_left, plan.unknown_left))}</summary>
      <ol aria-label={LIBRARY.stepsLabel}>
        {plan.steps.map((s) => (
          <li key={s.stage} className={`step ${s.state}`} aria-current={s.state === "current" ? "step" : undefined}>
            <span className="tick" aria-hidden="true">{s.state === "done" ? "\u2713" : s.state === "current" ? "\u25B8" : s.state === "skipped" ? "\u2013" : "\u00B7"}</span>
            <span>{LIBRARY.stage(s.stage)}</span>
            {s.state === "done" && <span className="hint">{LIBRARY.stepDone}</span>}
            {s.state === "current" && (s.total ?? 0) > 1 && <span className="hint">{LIBRARY.stepCount(s.done ?? 0, s.total ?? 0)}</span>}
            {s.state === "skipped" && <span className="hint">{s.note ?? LIBRARY.stepSkipped}</span>}
            {s.state !== "done" && s.state !== "skipped" && <span className="hint">{LIBRARY.duration(s.seconds_left)}</span>}
          </li>
        ))}
      </ol>
    </details>
  );
}

const NEW_WORLD = "new";

// Pick a world (or name a new one) and confirm: nothing is sent before the confirmation.
function MovePicker({ book, worlds, onConfirm, onCancel }:
  { book: LibraryBook; worlds: World[]; onConfirm: (world: string | number, create: boolean) => void; onCancel: () => void }) {
  const others = worlds.filter((w) => w.name !== book.world);
  const [target, setTarget] = useState(others[0] ? String(others[0].id) : NEW_WORLD);
  const [name, setName] = useState("");
  const isNew = target === NEW_WORLD;
  const chosen = isNew ? name.trim() : others.find((w) => String(w.id) === target)?.name ?? "";
  return (
    <div className="lib-move" role="group" aria-label={WORLDS.moveOpenLabel(book.title)}>
      <select aria-label={WORLDS.moveTargetLabel(book.title)} value={target} onChange={(e) => setTarget(e.target.value)}>
        {others.map((w) => <option key={w.id} value={String(w.id)}>{w.name}</option>)}
        <option value={NEW_WORLD}>{WORLDS.newWorld}</option>
      </select>
      {isNew && <input type="text" aria-label={WORLDS.newWorldName} value={name} onChange={(e) => setName(e.target.value)} autoFocus />}
      {chosen && <p className="hint">{WORLDS.moveConfirm(book.title, chosen, isNew)}</p>}
      <div className="row-end">
        <button className="tbtn" type="button" onClick={onCancel}>{WORLDS.moveCancel}</button>
        <button className="tbtn primary" type="button" disabled={!chosen}
          onClick={() => onConfirm(isNew ? chosen : Number(target), isNew)}>{WORLDS.moveConfirmButton}</button>
      </div>
    </div>
  );
}

interface Group { key: string; world: World | null; name: string; books: LibraryBook[] }

// The worlds in id order, then any world a book names that the list lacks, then books with no world.
function groupBooks(books: LibraryBook[], worlds: World[]): Group[] {
  const groups: Group[] = worlds.map((w) => ({ key: `w${w.id}`, world: w, name: w.name, books: books.filter((b) => b.world === w.name) }));
  const known = new Set(worlds.map((w) => w.name));
  for (const name of [...new Set(books.map((b) => b.world).filter((n): n is string => !!n && !known.has(n)))])
    groups.push({ key: `n${name}`, world: null, name, books: books.filter((b) => b.world === name) });
  const loose = books.filter((b) => !b.world);
  if (loose.length) groups.push({ key: "none", world: null, name: WORLDS.ungrouped, books: loose });
  return groups;
}

const seriesOf = (books: LibraryBook[]) => [...new Set(books.map((b) => b.series).filter((x): x is string => !!x))];

// Rename and delete for a world, each with its own inline form (a refusal stays on screen with its reason).
function WorldHeader({ group, error, onRename, onDelete }:
  { group: Group; error?: string; onRename: (name: string) => Promise<boolean>; onDelete: () => Promise<boolean> }) {
  const [renaming, setRenaming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const series = seriesOf(group.books);
  const w = group.world;
  return (
    <div className="lib-whead">
      <div className="lib-wtitle">
        <h3>{group.name}</h3>
        {series.length > 0 && <span className="meta">{WORLDS.series(series)}</span>}
      </div>
      {w && renaming === null && !deleting && (
        <div className="lib-wactions">
          <button className="tbtn" type="button" aria-label={WORLDS.renameLabel(w.name)} onClick={() => setRenaming(w.name)}>{WORLDS.rename}</button>
          {group.books.length === 0 && (
            <button className="tbtn" type="button" aria-label={WORLDS.deleteLabel(w.name)} onClick={() => setDeleting(true)}>{WORLDS.delete}</button>
          )}
        </div>
      )}
      {w && renaming !== null && (
        <form className="lib-wform" onSubmit={async (e) => { e.preventDefault(); if (renaming.trim() && await onRename(renaming)) setRenaming(null); }}>
          <input type="text" aria-label={WORLDS.renameInput(w.name)} value={renaming} onChange={(e) => setRenaming(e.target.value)} autoFocus />
          <button className="tbtn primary" type="submit" disabled={!renaming.trim()}>{WORLDS.save}</button>
          <button className="tbtn" type="button" onClick={() => setRenaming(null)}>{WORLDS.cancel}</button>
        </form>
      )}
      {w && deleting && (
        <div className="lib-wform">
          <span className="hint">{WORLDS.deleteConfirm(w.name)}</span>
          <button className="tbtn primary" type="button" onClick={async () => { if (!await onDelete()) setDeleting(false); }}>{WORLDS.deleteConfirmButton}</button>
          <button className="tbtn" type="button" onClick={() => setDeleting(false)}>{WORLDS.cancel}</button>
        </div>
      )}
      {error && <p className="hint" role="alert">{error}</p>}
    </div>
  );
}

// `onChange`: called once a world was renamed or deleted, and when a move has run its course, so the page
// can refetch the worlds and the graph. `focusBook`: a book just added; the panel opens at it and polls until its
// processing shows (a queued book's row may take a moment to appear).
// The footer follows the user's provider: Gemini's free tier shows today's requests; Claude, OpenAI and a paid Gemini
// plan show tokens (and an estimated cost where a price is known, and the daily cap when set).
function footer(data: LibraryStatus, showInternals: boolean): string {
  const u = data.usage;
  if (u && u.mode === "metered")
    return LIBRARY.metered(PROVIDERS.names[u.provider] ?? u.provider, u.tokens_today, u.cost_usd, u.daily_token_cap);
  return LIBRARY.gemini(data.gemini.used_today, data.gemini.limit, data.gemini.answer_reserve, showInternals);
}

export function LibraryPanel({ open, onClose, showInternals, onChange, focusBook = null }:
  { open: boolean; onClose: () => void; showInternals: boolean; onChange?: () => void; focusBook?: string | null }) {
  const [data, setData] = useState<LibraryStatus | null>(null);
  const [worlds, setWorlds] = useState<World[]>([]);
  const [moving, setMoving] = useState<string | null>(null);
  const [worldErrors, setWorldErrors] = useState<Record<string, string>>({});
  const dirty = useRef(false); // a move was started: tell the page when it is over
  const [failed, setFailed] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [addingSample, setAddingSample] = useState(false);
  const [sampleError, setSampleError] = useState<string | null>(null);
  // Books just started: poll until we see them run and then stop, or give up.
  const [started, setStarted] = useState<Record<string, { left: number; seen: boolean }>>({});
  const opener = useRef<HTMLElement | null>(null);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const load = useCallback(async () => {
    const [r, w] = await Promise.all([api.books(), api.worlds()]);
    if (!alive.current) return;
    if (!isError(w) && Array.isArray(w)) setWorlds(w);
    if (isError(r)) setFailed(true); else { setFailed(false); setData(r); }
  }, []);
  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement as HTMLElement | null;
    load();
    return () => { opener.current?.focus(); };
  }, [open, load]);
  useEffect(() => {
    if (open && focusBook) { setStarted((cur) => ({ ...cur, [focusBook]: { left: START_CYCLES, seen: false } })); dirty.current = true; }
  }, [open, focusBook]);
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [open, onClose]);
  // Re-check the clock so a paused book's Continue enables itself when its time passes.
  useEffect(() => {
    if (!open) return;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(t);
  }, [open]);
  useEffect(() => {
    if (!data) return;
    setStarted((cur) => {
      let changed = false;
      const next: typeof cur = {};
      for (const [id, st] of Object.entries(cur)) {
        const state = data.books.find((b) => b.id === id)?.job?.state;
        if (state === "running") { next[id] = st.seen ? st : { ...st, seen: true }; changed = changed || !st.seen; }
        else if (st.seen) changed = true; // ran, and has now stopped
        else next[id] = st;
      }
      return changed ? next : cur;
    });
  }, [data]);
  const anyRunning = !!data?.books.some((b) => b.job?.state === "running") || Object.keys(started).length > 0;
  const notify = useRef(onChange);
  notify.current = onChange;
  const flush = useCallback(() => { if (dirty.current) { dirty.current = false; notify.current?.(); } }, []);
  useEffect(() => { if (!anyRunning) flush(); }, [anyRunning, flush]);
  useEffect(() => { if (!open) flush(); }, [open, flush]);
  useEffect(() => {
    if (!open || !anyRunning) return;
    const t = setInterval(() => {
      setStarted((cur) => {
        const next: typeof cur = {};
        for (const [id, st] of Object.entries(cur)) if (st.left > 1) next[id] = { ...st, left: st.left - 1 };
        return next;
      });
      load();
    }, POLL_MS);
    return () => clearInterval(t);
  }, [open, anyRunning, load]);
  if (!open) return null;
  const go = async (id: string) => {
    setBusy((s) => new Set(s).add(id));
    setErrors((e) => { const { [id]: _drop, ...rest } = e; return rest; });
    const r = await api.continueBook(id);
    if (!alive.current) return;
    if (isError(r)) setErrors((e) => ({ ...e, [id]: r.error.message || LIBRARY.startFailed }));
    else setStarted((cur) => ({ ...cur, [id]: { left: START_CYCLES, seen: false } }));
    setBusy((s) => { const n = new Set(s); n.delete(id); return n; });
    await load();
  };
  const move = async (book: LibraryBook, world: string | number, create: boolean) => {
    setBusy((b) => new Set(b).add(book.id));
    setErrors((e) => { const { [book.id]: _drop, ...rest } = e; return rest; });
    const r = await api.moveBook(book.id, world, create);
    if (!alive.current) return;
    if (isError(r)) setErrors((e) => ({ ...e, [book.id]: r.error.message || WORLDS.moveFailed }));
    else if (!r.started) { setErrors((e) => ({ ...e, [book.id]: WORLDS.moveSame })); setMoving(null); }
    else { setMoving(null); dirty.current = true; setStarted((cur) => ({ ...cur, [book.id]: { left: START_CYCLES, seen: false } })); }
    setBusy((b) => { const n = new Set(b); n.delete(book.id); return n; });
    await load();
  };
  // Deleting a book: the list, the worlds, the graph and the reading progress refetch (onChange tells the page).
  // Resolves false when refused (the reason stays on the row).
  const remove = async (book: LibraryBook): Promise<boolean> => {
    setBusy((b) => new Set(b).add(book.id));
    setErrors((e) => { const { [book.id]: _drop, ...rest } = e; return rest; });
    const r = await api.deleteBook(book.id);
    if (!alive.current) return false;
    setBusy((b) => { const n = new Set(b); n.delete(book.id); return n; });
    if (isError(r)) {
      setErrors((e) => ({ ...e, [book.id]: r.error.message || LIBRARY.deleteBookFailed }));
      await load();
      return false;
    }
    if (moving === book.id) setMoving(null);
    await load();
    onChange?.();
    return true;
  };
  // The sample book: copied in whole on the server (already processed), so the page refetches at once.
  const addSample = async () => {
    setAddingSample(true);
    setSampleError(null);
    const r = await api.addSample();
    if (!alive.current) return;
    setAddingSample(false);
    if (isError(r)) setSampleError(r.error.message || LIBRARY.sampleFailed);
    await load();
    if (!isError(r)) onChange?.();
  };
  // A world change that went through: refetch here and tell the page. Resolves false when refused (reason shown).
  const changeWorld = async (key: string, call: () => Promise<unknown>): Promise<boolean> => {
    setWorldErrors((e) => { const { [key]: _drop, ...rest } = e; return rest; });
    const r = await call();
    if (!alive.current) return false;
    if (isError(r)) { setWorldErrors((e) => ({ ...e, [key]: r.error.message || WORLDS.changeFailed })); return false; }
    await load();
    onChange?.();
    return true;
  };
  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="dialog library" role="dialog" aria-modal="true" aria-labelledby="lib-title">
        <h2 id="lib-title">{LIBRARY.title}</h2>
        {failed && !data && <p className="hint" role="alert">{LIBRARY.unreachable}</p>}
        {!data && !failed && <p className="hint">{LIBRARY.loading}</p>}
        {data && (data.books.length || worlds.length ? groupBooks(data.books, worlds).map((g) => (
          <div key={g.key} className="lib-world" role="group" aria-label={WORLDS.groupLabel(g.name)}>
            <WorldHeader group={g} error={worldErrors[g.key]}
              onRename={(name) => changeWorld(g.key, () => api.renameWorld(g.world!.id, name))}
              onDelete={() => changeWorld(g.key, () => api.deleteWorld(g.world!.id))} />
            {g.books.length ? (
              <ul className="lib-list">
                {g.books.map((b) => (
                  <Row key={b.id} book={b} now={now} busy={busy.has(b.id)} error={errors[b.id]} focus={b.id === focusBook} onGo={() => go(b.id)}
                    processing={!!b.processing || b.job?.state === "running" || b.id in started}
                    onMove={() => setMoving(moving === b.id ? null : b.id)} onDelete={() => remove(b)}>
                    {moving === b.id && <MovePicker book={b} worlds={worlds} onCancel={() => setMoving(null)} onConfirm={(w, create) => move(b, w, create)} />}
                  </Row>
                ))}
              </ul>
            ) : <p className="hint">{WORLDS.noBooksInWorld}</p>}
          </div>
        )) : <p className="hint">{LIBRARY.empty}</p>)}
        {data && data.sample && !data.books.length && (
          <div className="lib-sample">
            <p className="hint">{LIBRARY.sampleLead}</p>
            <button className="tbtn" type="button" disabled={addingSample} onClick={addSample}>
              {addingSample ? LIBRARY.addingSample : LIBRARY.addSample}</button>
            {sampleError && <p className="hint" role="alert">{sampleError}</p>}
          </div>
        )}
        {data && <p className="hint lib-foot">{footer(data, showInternals)}</p>}
        <div className="row-end"><button className="tbtn" type="button" onClick={onClose}>{LIBRARY.close}</button></div>
      </div>
    </div>
  );
}
