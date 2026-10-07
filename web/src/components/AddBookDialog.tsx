import { useEffect, useState } from "react";
import { UI } from "../messages";
import { Snip } from "./SettingsMenu";

export function AddBookDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [msg, setMsg] = useState("");
  const [over, setOver] = useState(false);
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
    if (!/\.(epub|pdf)$/i.test(f.name)) { setFile(null); setMsg(`${f.name} is not an EPUB or PDF. Choose a .epub or .pdf file.`); return; }
    setFile(f); setMsg("");
  };
  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="add-title">
        <h2 id="add-title">Add a book</h2>
        <label className={`drop${over ? " over" : ""}`} htmlFor="add-file"
          onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files[0]); }}>
          <strong>Drop an EPUB or PDF here</strong><br />or click to choose a file
        </label>
        <input id="add-file" type="file" accept=".epub,.pdf" hidden onChange={(e) => pick(e.target.files?.[0])} />
        {msg && <p className="hint" role="alert">{msg}</p>}
        {file ? (<><p className="hint">{UI.ingestHint}</p><Snip text={`lore ingest "${file.name}"`} /></>)
          : <p className="hint">LORE splits the book into passages, then extracts characters, places and relationships.</p>}
        <div className="row-end"><button className="tbtn" type="button" onClick={onClose}>Close</button></div>
      </div>
    </div>
  );
}
