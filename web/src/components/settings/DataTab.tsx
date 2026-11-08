import { useState } from "react";
import { authApi, isError, nav } from "../../api";
import { ACCOUNT_DELETE, SETTINGS } from "../../messages";
import { shellQuote } from "../../shell";
import type { McpInfo } from "../../types";
import { Snip } from "../Snip";

// "Delete my account", behind a typed confirmation: the user's own email address (the server checks it too).
export function DeleteAccount({ email }: { email: string }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [state, setState] = useState<"idle" | "deleting" | "failed">("idle");
  const matches = typed.trim().toLowerCase() === email.toLowerCase();
  const submit = async () => {
    setState("deleting");
    const r = await authApi.deleteMe(typed.trim());
    if (isError(r)) { setState("failed"); return; }
    nav.to("/");
  };
  if (!open) return <div className="key-actions"><button className="tbtn danger" type="button" onClick={() => setOpen(true)}>{ACCOUNT_DELETE.open}</button></div>;
  return (<div className="delete-account">
    <label>{ACCOUNT_DELETE.confirmLabel(email)}
      <input type="text" autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} />
    </label>
    {state === "failed" && <div className="notice error" role="alert">{ACCOUNT_DELETE.failed}</div>}
    <div className="key-actions">
      <button className="tbtn danger" type="button" disabled={!matches || state === "deleting"} onClick={submit}>
        {state === "deleting" ? ACCOUNT_DELETE.deleting : ACCOUNT_DELETE.confirm}</button>
      <button className="tbtn" type="button" onClick={() => { setOpen(false); setTyped(""); setState("idle"); }}>{ACCOUNT_DELETE.cancel}</button>
    </div>
  </div>);
}

function DownloadData() {
  const [state, setState] = useState<"idle" | "busy" | "failed">("idle");
  const go = async () => {
    setState("busy");
    const r = await authApi.exportData();
    if (isError(r)) { setState("failed"); return; }
    const url = URL.createObjectURL(r.blob);
    const a = document.createElement("a");
    a.href = url; a.download = r.filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setState("idle");
  };
  return (
    <section className="set-section" aria-labelledby="set-download">
      <h4 id="set-download">{SETTINGS.download}</h4>
      <p className="hint">{SETTINGS.downloadHint}</p>
      <div className="key-actions">
        <button className="tbtn primary" type="button" disabled={state === "busy"} onClick={go}>
          {state === "busy" ? SETTINGS.downloading : SETTINGS.download}</button>
      </div>
      {state === "failed" && <div className="notice error" role="alert">{SETTINGS.downloadFailed}</div>}
    </section>
  );
}

// Your data. Multi mode: Download my data, and Delete my account. Local mode: where the library lives and how to back
// it up (it's on this computer already).
export function DataTab({ multi, email, mcp }: { multi: boolean; email?: string; mcp: McpInfo | null }) {
  if (multi) {
    return (<>
      <DownloadData />
      {email && (
        <section className="set-section danger-zone" aria-labelledby="set-delete">
          <h4 id="set-delete">{ACCOUNT_DELETE.title}</h4>
          <p className="hint">{ACCOUNT_DELETE.lead}</p>
          <DeleteAccount email={email} />
        </section>
      )}
    </>);
  }
  const lib = mcp?.library;
  if (!lib) return <p className="hint">{SETTINGS.mcpLoading}</p>;
  const slash = lib.data_dir.replace(/\/+$/, "").lastIndexOf("/");
  const parent = slash > 0 ? lib.data_dir.slice(0, slash) : "/";
  const name = lib.data_dir.slice(slash + 1);
  const outside = lib.central_db && !lib.central_db.startsWith(lib.data_dir + "/");
  const backup = `tar -czf ~/lore-backup-$(date +%F).tar.gz -C ${shellQuote(parent)} ${shellQuote(name)}`;
  return (<>
    <section className="set-section">
      <h4>{SETTINGS.libraryAt}</h4>
      <Snip text={lib.data_dir} label={SETTINGS.libraryAt} />
      {outside && (<><h4>{SETTINGS.chatsAt}</h4><Snip text={lib.central_db!} label={SETTINGS.chatsAt} /></>)}
    </section>
    <section className="set-section">
      <h4>{SETTINGS.backup}</h4>
      <p className="hint">{SETTINGS.backupHint}</p>
      <Snip text={backup} label={SETTINGS.backup} />
    </section>
  </>);
}
