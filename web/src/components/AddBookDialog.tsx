import { useEffect, useRef, useState } from "react";
import { UI } from "../messages";
import { shellQuote } from "../shell";
import { Snip } from "./SettingsMenu";

export function AddBookDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [msg, setMsg] = useState("");
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const zone = useRef<HTMLButtonElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement as HTMLElement | null;
    zone.current?.focus();
    return () => { opener.current?.focus(); };
  }, [open]);
  useEffect(() => { if (!open) { setFile(null); setMsg(""); } }, [open]);
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [open, onClose]);
  if (!open) return null;
  const pick = (f?: File) => {
    if (!f) return;
    if (!/\.(epub|pdf)$/i.test(f.name)) { setFile(null); setMsg(UI.wrongFileType(f.name)); return; }
    setFile(f); setMsg("");
  };
  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="add-title">
        <h2 id="add-title">Add a book</h2>
        <button ref={zone} type="button" className={`drop${over ? " over" : ""}`} onClick={() => input.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files[0]); }}>
          <strong>Drop an EPUB or PDF here</strong><br />or click to choose a file
        </button>
        <input ref={input} id="add-file" type="file" accept=".epub,.pdf" className="visually-hidden" tabIndex={-1} aria-hidden="true" onChange={(e) => pick(e.target.files?.[0])} />
        {msg && <p className="hint" role="alert">{msg}</p>}
        {file ? (<><p className="hint">{UI.ingestHint}</p><Snip text={`lore ingest ${shellQuote(file.name)}`} /></>)
          : <p className="hint">LORE splits the book into passages, then extracts characters, places and relationships.</p>}
        <div className="row-end"><button className="tbtn" type="button" onClick={onClose}>Close</button></div>
      </div>
    </div>
  );
}
