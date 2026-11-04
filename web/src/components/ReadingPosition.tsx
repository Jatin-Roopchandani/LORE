import { useCallback, useEffect, useId, useRef, useState, type CSSProperties } from "react";
import { api, isError } from "../api";
import { POSITION } from "../messages";
import type { PositionBook, ReadingPositionBody, ReadingPositionData } from "../types";
import { ALL_WORLDS } from "../worlds";

// Consecutive slider moves make one request, so the graph is refetched once, not on every step.
export const SLIDER_DEBOUNCE_MS = 350;

const bodyOf = (d: ReadingPositionData, patch: Partial<ReadingPositionBody>): ReadingPositionBody =>
  ({ enabled: d.enabled, book_id: d.book_id, chapter: d.chapter, read_books: d.read_books, ...patch });

/** The "Reading position" control (the button shows the position while it is on), next to the world selector: an on/off switch, the book being read
 *  (in series order, with read toggles for books of unknown order) and a slider for the last chapter finished.
 *  The position lives on the server (per world); `onChange` is called after each stored change so the graph,
 *  the panels and the book list can refetch. */
const Bookmark = ({ filled }: { filled: boolean }) => (
  <svg className="pos-icon" width="12" height="14" viewBox="0 0 12 14" aria-hidden="true">
    <path d="M1.5 1.5h9v11L6 9.6l-4.5 2.9z" fill={filled ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
  </svg>
);

export function ReadingPosition({ world, onChange = () => {} }: { world: string; onChange?: () => void }) {
  const [data, setData] = useState<ReadingPositionData | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  // The slider's value while it is being moved; the stored one follows after the debounce.
  const [draft, setDraft] = useState<number | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const live = useRef(true);
  const uid = useId();
  const oneWorld = world !== ALL_WORLDS;

  useEffect(() => { live.current = true; return () => { live.current = false; if (timer.current) clearTimeout(timer.current); }; }, []);
  // Load the world's stored position (a reply for a world since left is dropped).
  useEffect(() => {
    setData(null); setDraft(null); setFailed(false);
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    if (!oneWorld) return;
    let current = true;
    api.readingPosition(world).then((r) => { if (current && !isError(r)) setData(r); else if (current) setFailed(true); });
    return () => { current = false; };
  }, [world, oneWorld]);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener("mousedown", close); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);

  const store = useCallback(async (body: ReadingPositionBody) => {
    const r = await api.setReadingPosition(world, body);
    if (!live.current) return;
    if (isError(r)) { setFailed(true); return; }
    setFailed(false); setData(r); setDraft(null);
    onChange();
  }, [world, onChange]);

  const books = data?.books ?? [];
  const current: PositionBook | undefined = books.find((b) => b.id === data?.book_id);
  const chapter = draft ?? data?.chapter ?? 0;
  const save = (patch: Partial<ReadingPositionBody>) => { if (data) void store(bodyOf(data, patch)); };
  const turn = (on: boolean) => {
    if (!data) return;
    const book = data.book_id ?? books[0]?.id ?? null;
    if (on && !book) return;
    save({ enabled: on, book_id: book });
  };
  const pick = (id: string) => save({ book_id: id, chapter: 0, read_books: [] });
  const toggleRead = (id: string, read: boolean) => {
    if (!data) return;
    save({ read_books: read ? [...new Set([...data.read_books, id])] : data.read_books.filter((b) => b !== id) });
  };
  const slide = (n: number) => {
    if (!data) return;
    setDraft(n);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => { timer.current = null; void store(bodyOf(data, { chapter: n })); }, SLIDER_DEBOUNCE_MS);
  };

  const unknown = books.filter((b) => b.order === "unknown");
  // The book shown on the button: set only while the position is on.
  const reading = data?.enabled && current ? current : null;
  const on = !!reading;
  return (
    <div className="pop" ref={root}>
      <button ref={trigger} className={`tbtn pos-btn${on ? " on" : ""}`} type="button" aria-haspopup="dialog" aria-expanded={open}
        aria-label={reading ? POSITION.badgeLabel(reading.title, chapter) : POSITION.label} title={reading ? POSITION.badgeLabel(reading.title, chapter) : undefined}
        style={reading && reading.chapters > 0 ? ({ "--pos-done": `${Math.min(100, (100 * chapter) / reading.chapters)}%` } as CSSProperties) : undefined}
        onClick={() => setOpen(!open)}>
        <Bookmark filled={on} />
        {reading ? (<><span className="pos-title">{reading.title}</span><span className="pos-ch">{POSITION.chapterShort(chapter)}</span></>)
          : <span>{POSITION.label}</span>}
        <span className="caret" aria-hidden="true">▾</span>
      </button>
      {/* For screen readers; the visible state is the button itself. */}
      {reading && <span className="visually-hidden" role="status">{POSITION.badge(reading.title, chapter)}</span>}
      {open && (
        <div className="menu pos-menu" role="dialog" aria-label={POSITION.label}>
          {!oneWorld && <p className="hint">{POSITION.chooseWorld}</p>}
          {oneWorld && !data && <p className="hint">{failed ? POSITION.failed : POSITION.loading}</p>}
          {oneWorld && data && !books.length && <p className="hint">{POSITION.noBooks}</p>}
          {oneWorld && data && books.length > 0 && (<>
            <label className="pos-switch">
              <input type="checkbox" role="switch" checked={data.enabled} onChange={(e) => turn(e.target.checked)} />
              <span>{POSITION.switchLabel}</span>
            </label>
            <p className="hint">{data.enabled ? POSITION.on : POSITION.off}</p>
            <label className="pos-field" htmlFor={`${uid}-book`}>{POSITION.bookLabel}</label>
            <select id={`${uid}-book`} value={data.book_id ?? ""} onChange={(e) => pick(e.target.value)}>
              {!data.book_id && <option value="" disabled>—</option>}
              {books.map((b) => <option key={b.id} value={b.id}>{POSITION.bookOption(b.title, b.series_index)}</option>)}
            </select>
            {current && (<>
              <label className="pos-field" htmlFor={`${uid}-chapter`}>{POSITION.chapterLabel}</label>
              <input id={`${uid}-chapter`} type="range" min={0} max={Math.max(current.chapters, 0)} step={1} value={Math.min(chapter, current.chapters)}
                aria-valuetext={POSITION.chapterValue(chapter, current.chapters)} onChange={(e) => slide(Number(e.target.value))} />
              <span className="mono">{POSITION.chapterValue(chapter, current.chapters)}</span>
            </>)}
            {unknown.length > 0 && (
              <fieldset className="pos-read">
                <legend>{POSITION.readHeading}</legend>
                <p className="hint">{POSITION.readHint}</p>
                {unknown.map((b) => (
                  <label key={b.id}>
                    <input type="checkbox" checked={data.read_books.includes(b.id)} onChange={(e) => toggleRead(b.id, e.target.checked)} />
                    <span>{POSITION.readLabel(b.title)}</span>
                  </label>
                ))}
              </fieldset>
            )}
            {failed && <p className="note" role="alert">{POSITION.failed}</p>}
          </>)}
        </div>
      )}
    </div>
  );
}
