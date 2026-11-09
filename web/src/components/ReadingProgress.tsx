import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api, isError } from "../api";
import { POSITION } from "../messages";
import { chapterOptions, limits, pickedSeq, storyOf, summaryLine, topBarLabel } from "../progress";
import type { PositionBook, ProgressState, ReadingPositionData, World } from "../types";
import { ALL_WORLDS } from "../worlds";

const STATES: ProgressState[] = ["finished", "reading", "not_started"];
// The raw section of a book's first story chapter: where Reading starts when no chapter is chosen (as the server does).
const storyFirst = (book: PositionBook) => storyOf(book)[0]?.seq ?? 0;

/** The reading progress control in the top bar: a summary of where the reader is ("Ch. 7 · The Sign of the Four",
 *  "All read", "3 of 5 read") that opens the list of the world's books, each finished, being read (with a chapter
 *  picker) or not started. Spoiler protection is always on: LORE uses only what the list allows, and every book is
 *  finished until the reader says otherwise. In "All worlds" the rows are grouped by world. The server stores each
 *  change and fills in the series order around it (the list says which other books it marked); `onChange` is called
 *  after each stored change so the graph, the panels and the book list refetch, and `onLimited` says whether anything
 *  is held back (the ask box offers its one-question override then). */
export function ReadingProgress({ world, worlds = [], refreshKey = 0, onChange = () => {}, onLimited }:
  { world: string; worlds?: World[]; refreshKey?: number; onChange?: () => void; onLimited?: (limited: boolean) => void }) {
  const ids = world === ALL_WORLDS ? worlds.map((w) => String(w.id)) : [world];
  const key = ids.join(",");
  const [data, setData] = useState<Record<string, ReadingPositionData>>({});
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState<"load" | "save" | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const live = useRef(true);
  // Changes are sent one after another, so a quick run of them is stored in the order made.
  const queue = useRef<Promise<void>>(Promise.resolve());
  const uid = useId();

  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  // Load every shown world's list (a reply for worlds since left is dropped); again when the library changes, so a
  // book just added is listed (finished, like every new book).
  const shownKey = useRef("");
  useEffect(() => {
    // A refresh of the same worlds keeps the list on screen until the new one comes.
    if (shownKey.current !== key) { setData({}); setLoaded(false); setNote(null); }
    shownKey.current = key;
    setFailed(null);
    const shown = key ? key.split(",") : [];
    if (!shown.length) { setLoaded(true); return; }
    let current = true;
    Promise.all(shown.map((id) => api.readingPosition(id))).then((replies) => {
      if (!current) return;
      const got: Record<string, ReadingPositionData> = {};
      replies.forEach((r, i) => { if (!isError(r)) got[shown[i]] = r; });
      setData(got); setLoaded(true);
      if (replies.every(isError)) setFailed("load");
    });
    return () => { current = false; };
  }, [key, refreshKey]);
  const lists = ids.map((id) => data[id]).filter((d): d is ReadingPositionData => !!d);
  const limited = limits(lists);
  useEffect(() => { onLimited?.(limited); }, [limited, onLimited]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener("mousedown", close); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  // Opening moves focus into the list: the chosen state of the first book (arrow keys then move within its group).
  useEffect(() => {
    if (!open || !loaded) return;
    const first = dialog.current?.querySelector<HTMLElement>("input[type=radio]:checked") ?? dialog.current;
    first?.focus();
  }, [open, loaded]);

  const change = useCallback((wid: string, book: PositionBook, state: ProgressState, chapter?: number) => {
    // The choice shows at once; the server's list (with the series filled in) replaces it when it comes, and a
    // refused change puts the list back as it was.
    let before: ReadingPositionData | undefined;
    setData((d) => {
      before = d[wid];
      if (!before) return d;
      const shown = { ...book, state, forced: false, progress_chapter: state === "reading" ? chapter ?? (book.state === "reading" ? book.progress_chapter : storyFirst(book)) : 0 };
      return { ...d, [wid]: { ...before, books: before.books.map((b) => (b.id === book.id ? shown : b)) } };
    });
    queue.current = queue.current.then(async () => {
      const r = await api.setReadingPosition(wid, { change: { book_id: book.id, state, ...(chapter != null ? { chapter } : {}) } });
      if (!live.current) return;
      if (isError(r)) { setFailed("save"); if (before) { const was = before; setData((d) => ({ ...d, [wid]: was })); } return; }
      setFailed(null);
      setData((d) => ({ ...d, [wid]: r }));
      const titles = new Map(r.books.map((b) => [b.id, b.title]));
      const filled = (r.filled ?? []).map((f) => ({ title: titles.get(f.id) ?? f.id, state: f.state }));
      setNote(filled.length ? POSITION.filled(filled) : null);
      onChange();
    });
  }, [onChange]);

  const label = loaded && lists.length && lists.some((l) => l.books.length) ? topBarLabel(lists) : POSITION.label;
  const heading = POSITION.heading(world === ALL_WORLDS ? POSITION.allWorlds : lists[0]?.world.name ?? "");
  const books = lists.reduce((n, l) => n + l.books.length, 0);
  return (
    <div className="pop" ref={root}>
      <button ref={trigger} className={`tbtn pos-btn${limited ? " on" : ""}`} type="button" aria-haspopup="dialog" aria-expanded={open}
        aria-label={POSITION.buttonLabel(label)} onClick={() => setOpen(!open)}>
        <OpenBook />
        <span className="pos-title">{label}</span>
        <span className="caret" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="menu pos-menu" role="dialog" aria-labelledby={`${uid}-h`} ref={dialog} tabIndex={-1}>
          <h3 className="prog-heading" id={`${uid}-h`}>{heading}</h3>
          {!loaded && <p className="hint">{POSITION.loading}</p>}
          {loaded && failed === "load" && <p className="note" role="alert">{POSITION.loadFailed}</p>}
          {loaded && failed !== "load" && books === 0 && <p className="hint">{POSITION.noBooks}</p>}
          {loaded && books > 0 && (<>
            <div className="prog-list">
              {ids.map((wid) => data[wid]).filter((d): d is ReadingPositionData => !!d && d.books.length > 0).map((d) => (
                <section key={d.world.id} className="prog-world" aria-label={world === ALL_WORLDS ? d.world.name : undefined}>
                  {world === ALL_WORLDS && <h4>{d.world.name}</h4>}
                  <ul className="prog-books">
                    {d.books.map((b) => <BookRow key={b.id} uid={`${uid}-${d.world.id}-${b.id}`} book={b} worldName={d.world.name}
                      onState={(s) => change(String(d.world.id), b, s)}
                      onChapter={(seq) => change(String(d.world.id), b, "reading", seq)} />)}
                  </ul>
                  {world === ALL_WORLDS && <p className="prog-summary">{summaryLine(d.books)}</p>}
                </section>
              ))}
            </div>
            {note && <p className="prog-filled" role="status">{note}</p>}
            {failed === "save" && <p className="note" role="alert">{POSITION.failed}</p>}
            <div className="prog-foot">
              <p className="prog-lead">{POSITION.lead}</p>
              {world !== ALL_WORLDS && lists[0] && <p className="prog-summary">{summaryLine(lists[0].books)}</p>}
            </div>
          </>)}
        </div>
      )}
    </div>
  );
}

// An open book (the 📖 of the design, drawn so it looks the same on every system and follows the theme).
const OpenBook = () => (
  <svg className="pos-icon" width="16" height="14" viewBox="0 0 16 14" aria-hidden="true">
    <path d="M8 3.2C6.4 2 4 1.6 1.5 1.8v9.6C4 11.2 6.4 11.6 8 12.8c1.6-1.2 4-1.6 6.5-1.4V1.8C12 1.6 9.6 2 8 3.2z"
      fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    <path d="M8 3.2v9.6" stroke="currentColor" strokeWidth="1.4" />
  </svg>
);

function BookRow({ uid, book, worldName, onState, onChapter }:
  { uid: string; book: PositionBook; worldName: string; onState: (s: ProgressState) => void; onChapter: (seq: number) => void }) {
  const titleId = `${uid}-t`;
  return (
    <li className={`prog-book${book.state === "reading" ? " reading" : ""}`}>
      <span className="prog-name">
        <span className="prog-title" id={titleId}>{book.title}</span>
        {book.series && book.series_index != null && <span className="prog-series">{POSITION.seriesTag(book.series, book.series_index, book.series.trim().toLowerCase() === worldName.trim().toLowerCase())}</span>}
      </span>
      <div className="prog-states" role="radiogroup" aria-labelledby={titleId}>
        {STATES.map((s) => (
          <label key={s} className={`prog-state${book.state === s ? " on" : ""}`}>
            <input type="radio" name={`${uid}-state`} value={s} checked={book.state === s} onChange={() => onState(s)} />
            <span>{POSITION.states[s]}</span>
          </label>
        ))}
      </div>
      {book.state === "reading" && (
        <label className="prog-chapter">
          <span aria-hidden="true">{POSITION.chapterLabel}</span>
          <select aria-label={POSITION.chapterLabelFor(book.title)} value={pickedSeq(book)} onChange={(e) => onChapter(Number(e.target.value))}>
            {chapterOptions(book).map((o) => <option key={o.seq} value={o.seq}>{o.label}</option>)}
          </select>
        </label>
      )}
      {book.forced && <p className="hint prog-forced">{POSITION.forced}</p>}
    </li>
  );
}
