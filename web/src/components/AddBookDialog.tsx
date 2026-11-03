import { useEffect, useRef, useState } from "react";
import { UI, WORLDS } from "../messages";
import { shellQuote } from "../shell";
import type { World } from "../types";
import { suggestWorld } from "../worlds";
import { Snip } from "./SettingsMenu";

const NEW_WORLD = "new";

export function AddBookDialog({ open, onClose, worlds = [] }: { open: boolean; onClose: () => void; worlds?: World[] }) {
  const [file, setFile] = useState<File | null>(null);
  // The world the book goes to: an existing world's id (as a string) or NEW_WORLD with a name. Starts at the suggestion.
  const [target, setTarget] = useState(NEW_WORLD);
  const [newName, setNewName] = useState("");
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
    const suggestion = suggestWorld(f.name, worlds);
    setTarget(suggestion.worldId === null ? NEW_WORLD : String(suggestion.worldId));
    setNewName(suggestion.name);
  };
  const existing = worlds.find((w) => String(w.id) === target);
  const worldName = (existing ? existing.name : newName).trim();
  const command = file && `lore ingest ${shellQuote(file.name)}${worldName ? ` --world ${shellQuote(worldName)}` : ""}`;
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
        {file && (
          <div className="addworld">
            <label htmlFor="add-world">{WORLDS.addWorldLabel}</label>
            <select id="add-world" value={target} onChange={(e) => setTarget(e.target.value)}>
              {worlds.map((w) => <option key={w.id} value={String(w.id)}>{w.name}</option>)}
              <option value={NEW_WORLD}>{WORLDS.newWorld}</option>
            </select>
            {target === NEW_WORLD && <input type="text" aria-label={WORLDS.newWorldName} value={newName} onChange={(e) => setNewName(e.target.value)} />}
            <p className="hint">{WORLDS.addWorldHint}</p>
          </div>
        )}
        {file ? (<><p className="hint">{UI.ingestHint}</p><Snip text={command!} />{target === NEW_WORLD && <p className="hint">{UI.ingestWorldNote}</p>}</>)
          : <p className="hint">LORE splits the book into passages, then extracts characters, places and relationships.</p>}
        <div className="row-end"><button className="tbtn" type="button" onClick={onClose}>Close</button></div>
      </div>
    </div>
  );
}
