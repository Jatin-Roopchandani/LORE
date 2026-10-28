import { useCallback, useEffect, useRef, useState } from "react";
import { api, isError } from "../api";
import { LIBRARY } from "../messages";
import type { LibraryBook, LibraryStatus } from "../types";

const POLL_MS = 3000;
const TICK_MS = 30000;
const START_CYCLES = 10; // polls forced after Continue, in case the first GET predates the worker

const clock = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

function Row({ book, now, busy, error, onGo }: { book: LibraryBook; now: number; busy: boolean; error?: string; onGo: () => void }) {
  const job = book.job;
  const meta = [book.author, book.series && (book.series_index != null ? `${book.series} #${book.series_index}` : book.series)].filter(Boolean).join(" · ");
  const btn = (verb: string, disabled = false) => (
    <button className="tbtn" type="button" disabled={disabled || busy} aria-label={LIBRARY.action(verb, book.title)} onClick={onGo}>{verb}</button>
  );
  let body: React.ReactNode;
  if (job?.state === "running") {
    const pct = job.total > 0 ? Math.min(100, Math.round((job.done / job.total) * 100)) : 0;
    body = (<>
      <span className="lib-state">{LIBRARY.stage(job.stage)}</span>
      <div className="lib-bar" role="progressbar" aria-label={LIBRARY.progressLabel(job.stage)} aria-valuemin={0} aria-valuemax={job.total} aria-valuenow={job.done}>
        <span style={{ width: `${pct}%` }} />
      </div>
      <span className="hint">{LIBRARY.progress(job.done, job.total)}</span>
    </>);
  } else if (job?.state === "waiting_quota" || job?.state === "idle") {
    const at = job.resumable_at ? new Date(job.resumable_at) : null;
    const left = at ? Math.max(0, Math.ceil((at.getTime() - now) / 60000)) : 0;
    const enabled = at ? now >= at.getTime() : job.can_continue;
    body = (<>
      <span className="lib-state">{LIBRARY.paused}</span>
      <span className="hint">{job.stopped_reason || LIBRARY.pausedDefault}</span>
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
    <li className="lib-row">
      <div className="lib-title"><strong>{book.title}</strong>{meta && <span className="meta">{meta}</span>}</div>
      <div className="lib-body">{body}</div>
      {error && <p className="hint" role="alert">{error}</p>}
    </li>
  );
}

export function LibraryPanel({ open, onClose, showInternals }: { open: boolean; onClose: () => void; showInternals: boolean }) {
  const [data, setData] = useState<LibraryStatus | null>(null);
  const [failed, setFailed] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});
  // Books just started: poll until we see them run and then stop, or give up.
  const [started, setStarted] = useState<Record<string, { left: number; seen: boolean }>>({});
  const opener = useRef<HTMLElement | null>(null);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const load = useCallback(async () => {
    const r = await api.books();
    if (!alive.current) return;
    if (isError(r)) setFailed(true); else { setFailed(false); setData(r); }
  }, []);
  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement as HTMLElement | null;
    load();
    return () => { opener.current?.focus(); };
  }, [open, load]);
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
  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="dialog library" role="dialog" aria-modal="true" aria-labelledby="lib-title">
        <h2 id="lib-title">{LIBRARY.title}</h2>
        {failed && !data && <p className="hint" role="alert">{LIBRARY.unreachable}</p>}
        {!data && !failed && <p className="hint">{LIBRARY.loading}</p>}
        {data && (data.books.length ? (
          <ul className="lib-list">
            {data.books.map((b) => <Row key={b.id} book={b} now={now} busy={busy.has(b.id)} error={errors[b.id]} onGo={() => go(b.id)} />)}
          </ul>
        ) : <p className="hint">{LIBRARY.empty}</p>)}
        {data && <p className="hint lib-foot">{LIBRARY.gemini(data.gemini.used_today, data.gemini.limit, data.gemini.answer_reserve, showInternals)}</p>}
        <div className="row-end"><button className="tbtn" type="button" onClick={onClose}>{LIBRARY.close}</button></div>
      </div>
    </div>
  );
}
