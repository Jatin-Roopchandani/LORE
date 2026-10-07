import { useEffect, useRef, useState } from "react";
import { api, isError } from "../api";
import { UI } from "../messages";
import { applyTheme, loadTheme, type ThemeMode } from "../theme";
import type { McpInfo } from "../types";

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
  const [mode, setMode] = useState<ThemeMode>(loadTheme());
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => { if (open && showInternals) api.mcp().then((r) => { if (!isError(r)) setMcp(r); }); }, [open, showInternals]);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  const choose = (m: ThemeMode) => { applyTheme(m); setMode(m); };
  return (
    <div className="pop" ref={root}>
      <button className="tbtn icon" type="button" aria-label="Settings" aria-haspopup="true" aria-expanded={open} onClick={() => setOpen(!open)}>⚙</button>
      {open && (
        <div className="menu">
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
              <span className={`status${mcp.http_running ? " on" : ""}`}><i />{mcp.http_running ? "HTTP server running" : UI.mcpNotRunning}</span>
            </>)}
          </>)}
          <h4>Appearance</h4>
          <div className="seg" role="group" aria-label="Theme">
            {(["system", "light", "dark"] as ThemeMode[]).map((m) => (
              <button key={m} type="button" aria-pressed={mode === m} onClick={() => choose(m)}>{m[0].toUpperCase() + m.slice(1)}</button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
