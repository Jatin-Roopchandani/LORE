import { useEffect, useRef, useState } from "react";
import { UI, UPLOAD, WORLDS, uploadError } from "../messages";
import { shellQuote } from "../shell";
import type { UploadError, UploadResult, World } from "../types";
import { MAX_UPLOAD_BYTES, uploadBook, type UploadProgress } from "../upload";
import { suggestWorld } from "../worlds";
import { Snip } from "./SettingsMenu";

const NEW_WORLD = "new";
const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

// Add a book: pick or drop the file, choose its world (the suggestion first), upload it with a progress bar, then
// processing starts on the server. `multi`: the multi-user app, which has no terminal to offer. `onUploaded`: a book
// was added (the page refetches its books and worlds). `onOpenBook`: open the Library at that book.
export function AddBookDialog({ open, onClose, worlds = [], multi = false, onUploaded, onOpenBook }:
  { open: boolean; onClose: () => void; worlds?: World[]; multi?: boolean; onUploaded?: (r: UploadResult) => void;
    onOpenBook?: (bookId: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  // The world the book goes to: an existing world's id (as a string) or NEW_WORLD with a name. Starts at the suggestion.
  const [target, setTarget] = useState(NEW_WORLD);
  const [newName, setNewName] = useState("");
  const [msg, setMsg] = useState("");
  const [over, setOver] = useState(false);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [sending, setSending] = useState(false);
  const [done, setDone] = useState<UploadResult | null>(null);
  const [failure, setFailure] = useState<UploadError | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const zone = useRef<HTMLButtonElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement as HTMLElement | null;
    zone.current?.focus();
    return () => { opener.current?.focus(); };
  }, [open]);
  const reset = () => { setFile(null); setMsg(""); setProgress(null); setSending(false); setDone(null); setFailure(null); };
  useEffect(() => { if (!open) { abort.current?.abort(); abort.current = null; reset(); } }, [open]);
  useEffect(() => () => abort.current?.abort(), []);
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [open, onClose]);
  if (!open) return null;
  const pick = (f?: File) => {
    if (!f) return;
    setFailure(null);
    if (!/\.(epub|pdf)$/i.test(f.name)) { setFile(null); setMsg(UI.wrongFileType(f.name)); return; }
    if (f.size > MAX_UPLOAD_BYTES) { setFile(null); setMsg(UPLOAD.tooBig(f.name)); return; }
    setFile(f); setMsg("");
    const suggestion = suggestWorld(f.name, worlds);
    setTarget(suggestion.worldId === null ? NEW_WORLD : String(suggestion.worldId));
    setNewName(suggestion.name);
  };
  const existing = worlds.find((w) => String(w.id) === target);
  const worldName = (existing ? existing.name : newName).trim();
  const command = file && `lore ingest ${shellQuote(file.name)}${worldName ? ` --world ${shellQuote(worldName)}` : ""}`;
  const send = async () => {
    if (!file || sending) return;
    const controller = new AbortController();
    abort.current = controller;
    setSending(true); setFailure(null); setProgress({ loaded: 0, total: file.size });
    // An existing world goes by id; a new one by its name (the server creates it).
    const r = await uploadBook(file, existing ? String(existing.id) : worldName, setProgress, controller.signal);
    if (controller.signal.aborted) return; // cancelled or closed: already reset
    abort.current = null;
    setSending(false);
    if ("error" in r) { setFailure(r); return; }
    setDone(r);
    onUploaded?.(r);
  };
  const cancel = () => {
    abort.current?.abort(); abort.current = null;
    setSending(false); setProgress(null); setFailure({ error: { code: "aborted", message: "" } });
  };
  const pct = progress && progress.total > 0 ? Math.min(100, Math.round((progress.loaded / progress.total) * 100)) : 0;
  const nextText = (r: UploadResult) => {
    const p = r.processing;
    return p.state === "waiting" ? UPLOAD.next.waiting(clock(p.resumable_at)) : UPLOAD.next[p.state];
  };
  const openBook = (id: string) => onOpenBook?.(id);
  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !sending) onClose(); }}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="add-title">
        <h2 id="add-title">{UPLOAD.title}</h2>
        {done ? (
          <div className="upload-done" role="status">
            <strong>{done.processing.state === "waiting" ? UPLOAD.waitingTitle : UPLOAD.started}</strong>
            <p className="hint">{UPLOAD.addedTo(done.title, done.world?.name ?? null)}</p>
            <p className="hint">{nextText(done)}</p>
            <div className="row-end">
              <button className="tbtn" type="button" onClick={reset}>{UPLOAD.another}</button>
              <button className="tbtn primary" type="button" onClick={() => openBook(done.book_id)}>{UPLOAD.openLibrary}</button>
            </div>
          </div>
        ) : (<>
          <button ref={zone} type="button" className={`drop${over ? " over" : ""}`} disabled={sending} onClick={() => input.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
            onDrop={(e) => { e.preventDefault(); setOver(false); if (!sending) pick(e.dataTransfer.files[0]); }}>
            <strong>{UPLOAD.drop}</strong><br />{UPLOAD.dropOr}
          </button>
          <input ref={input} id="add-file" type="file" accept=".epub,.pdf,application/epub+zip,application/pdf" className="visually-hidden" tabIndex={-1} aria-hidden="true"
            onChange={(e) => pick(e.target.files?.[0])} />
          <p className="hint">{UPLOAD.limits}</p>
          {msg && <p className="hint" role="alert">{msg}</p>}
          {file && (<>
            <p className="upload-file">{UPLOAD.chosen(file.name, file.size)}</p>
            <div className="addworld">
              <label htmlFor="add-world">{WORLDS.addWorldLabel}</label>
              <select id="add-world" value={target} disabled={sending} onChange={(e) => setTarget(e.target.value)}>
                {worlds.map((w) => <option key={w.id} value={String(w.id)}>{w.name}</option>)}
                <option value={NEW_WORLD}>{WORLDS.newWorld}</option>
              </select>
              {target === NEW_WORLD && <input type="text" aria-label={WORLDS.newWorldName} value={newName} disabled={sending} onChange={(e) => setNewName(e.target.value)} />}
              <p className="hint">{WORLDS.addWorldHint}</p>
            </div>
            {progress && sending && (
              <div className="upload-progress">
                <div className="lib-bar" role="progressbar" aria-label={UPLOAD.progressLabel} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
                  <span style={{ width: `${pct}%` }} />
                </div>
                <span className="hint">{pct >= 100 ? UPLOAD.checking : UPLOAD.progress(progress.loaded, progress.total)}</span>
              </div>
            )}
            {failure && (
              <p className="hint upload-error" role="alert">
                {uploadError(failure.error)}
                {failure.error.code === "already_added" && failure.book_id && (
                  <> <button className="linkbtn" type="button" onClick={() => openBook(failure.book_id!)}>{UPLOAD.openLibrary}</button></>
                )}
              </p>
            )}
            {!multi && (
              <details className="upload-cli">
                <summary>{UPLOAD.terminal}</summary>
                <p className="hint">{UI.ingestHint}</p>
                <Snip text={command!} />
                {target === NEW_WORLD && <p className="hint">{UI.ingestWorldNote}</p>}
              </details>
            )}
          </>)}
          {!file && <p className="hint">{UPLOAD.intro}</p>}
          <div className="row-end">
            {sending
              ? <button className="tbtn" type="button" onClick={cancel}>{UPLOAD.cancel}</button>
              : <button className="tbtn" type="button" onClick={onClose}>{UPLOAD.close}</button>}
            {file && <button className="tbtn primary" type="button" disabled={sending || !worldName} onClick={send}>{sending ? UPLOAD.uploading : UPLOAD.upload}</button>}
          </div>
        </>)}
      </div>
    </div>
  );
}
