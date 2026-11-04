import { useEffect, useRef, useState } from "react";
import { api, authApi, isError, nav } from "../api";
import { AUTH, UI } from "../messages";
import { isMulti } from "../routing";
import type { McpInfo, Me } from "../types";

function Snip({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const copy = () => {
    const select = () => { const r = document.createRange(); r.selectNodeContents(ref.current!); const s = getSelection(); s?.removeAllRanges(); s?.addRange(r); };
    try { navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1400); }, select); } catch { select(); }
  };
  return <div className="snip"><span ref={ref}>{text}</span><button className="copy" type="button" onClick={copy}>{copied ? "Copied" : "Copy"}</button></div>;
}

export { Snip };

export function SettingsMenu({ showInternals }: { showInternals: boolean }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"stdio" | "http">("stdio");
  const [mcp, setMcp] = useState<McpInfo | null>(null);
  // Signed-in account (multi-user mode only): who you are, the admin page for admins, and Sign out.
  const multi = isMulti();
  const [me, setMe] = useState<Me["user"] | null>(null);
  useEffect(() => { if (open && multi) authApi.me().then((r) => { if (!isError(r)) setMe(r.user); }); }, [open, multi]);
  const signOut = async () => { await authApi.logout(); nav.to("/"); };
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (open && showInternals) api.mcp().then((r) => { if (!isError(r)) setMcp(r); }); }, [open, showInternals]);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener("mousedown", close); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  return (
    <div className="pop" ref={root}>
      <button ref={trigger} className="tbtn icon" type="button" aria-label="Settings" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(!open)}>⚙</button>
      {open && (
        <div className="menu">
          {multi && me && (<>
            <h4>{AUTH.account}</h4>
            <p className="hint">{me.email}</p>
            {me.role === "admin" && <a className="tbtn" href="/admin">{AUTH.admin}</a>}
            <button className="tbtn" type="button" onClick={signOut}>{AUTH.signOut}</button>
          </>)}
          {showInternals && (<>
            <h4>Connect an MCP client</h4>
            <div className="tabs" role="tablist">
              <button type="button" role="tab" aria-selected={tab === "stdio"} onClick={() => setTab("stdio")}>Claude Code</button>
              <button type="button" role="tab" aria-selected={tab === "http"} onClick={() => setTab("http")}>HTTP</button>
            </div>
            {tab === "stdio" && mcp && (<>
              <p className="hint">Run once in a terminal. Claude Code starts LORE itself when it needs it.</p>
              <Snip text={mcp.stdio_command} />
            </>)}
            {tab === "http" && mcp && (<>
              <p className="hint">For clients that connect over HTTP. Start the server, then point the client at the address. Loopback only.</p>
              <Snip text={mcp.http_command} /><Snip text={mcp.http_url} />
              <span className={`status${mcp.http_running ? " on" : ""}`}><i />{mcp.http_running ? UI.mcpRunning : UI.mcpNotRunning}</span>
            </>)}
          </>)}
        </div>
      )}
    </div>
  );
}
