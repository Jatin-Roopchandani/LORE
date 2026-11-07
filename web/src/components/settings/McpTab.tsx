import { useState } from "react";
import { api, isError } from "../../api";
import { SETTINGS, UI } from "../../messages";
import type { McpCheck, McpInfo } from "../../types";
import { Snip } from "../Snip";

// MCP connector. Local mode: what it does, the exact snippets for Claude Desktop, Claude Code and HTTP (built by the
// server from the real `lore` path and this library), and Check, which builds the server and lists its tools.
// Multi mode: coming soon for hosted accounts; everything else is shown disabled.
export function McpTab({ multi, mcp }: { multi: boolean; mcp: McpInfo | null }) {
  if (multi) {
    return (<>
      <div className="notice" role="status"><strong>{SETTINGS.mcpSoon}</strong> {SETTINGS.mcpSoonHint}</div>
      <p className="hint">{SETTINGS.mcpWhatHosted}</p>
      <fieldset className="set-disabled" disabled aria-disabled="true">
        <div className="key-actions"><button className="tbtn" type="button">{SETTINGS.mcpCheck}</button></div>
      </fieldset>
    </>);
  }
  return (<>
    <p className="set-lead-text">{SETTINGS.mcpWhat}</p>
    {!mcp ? <p className="hint">{SETTINGS.mcpLoading}</p> : (<>
      {mcp.claude_desktop && (
        <section className="set-section">
          <h4>{SETTINGS.mcpDesktop}</h4>
          <p className="hint">{SETTINGS.mcpDesktopHint}</p>
          <Snip text={mcp.claude_desktop} label={SETTINGS.mcpDesktop} />
        </section>
      )}
      <section className="set-section">
        <h4>{SETTINGS.mcpCode}</h4>
        <p className="hint">{SETTINGS.mcpCodeHint}</p>
        <Snip text={mcp.claude_code ?? mcp.stdio_command} label={SETTINGS.mcpCode} />
      </section>
      <section className="set-section">
        <h4>{SETTINGS.mcpHttp}</h4>
        <p className="hint">{SETTINGS.mcpHttpHint}</p>
        <Snip text={mcp.http_command} label={SETTINGS.mcpHttpCommand} />
        <Snip text={mcp.http_url} label={SETTINGS.mcpHttpAddress} />
        {mcp.claude_code_http && (<><p className="hint">{SETTINGS.mcpHttpClaude}</p><Snip text={mcp.claude_code_http} label={SETTINGS.mcpCodeHttp} /></>)}
        <span className={`status${mcp.http_running ? " on" : ""}`}><i />{mcp.http_running ? UI.mcpRunning : UI.mcpNotRunning}</span>
      </section>
    </>)}
    <Check />
  </>);
}

function Check() {
  const [state, setState] = useState<"idle" | "busy" | "failed">("idle");
  const [result, setResult] = useState<McpCheck | null>(null);
  const run = async () => {
    setState("busy");
    const r = await api.mcpCheck();
    if (isError(r)) { setResult(null); setState("failed"); return; }
    setResult(r); setState("idle");
  };
  return (
    <section className="set-section">
      <div className="key-actions">
        <button className="tbtn primary" type="button" disabled={state === "busy"} onClick={run}>
          {state === "busy" ? SETTINGS.mcpChecking : SETTINGS.mcpCheck}</button>
        {result && <span className={`status${result.ok ? " on" : " off"}`} role="status"><i />{result.ok ? SETTINGS.mcpOk : SETTINGS.mcpFailed}</span>}
      </div>
      {state === "failed" && <div className="notice error" role="alert">{SETTINGS.mcpCheckFailed}</div>}
      {result?.problem && <div className="notice error" role="alert">{result.problem}</div>}
      {result && result.library_found === false && <p className="hint">{SETTINGS.mcpNoLibrary}</p>}
      {result && result.tools.length > 0 && (<>
        <h4>{SETTINGS.mcpTools(result.tools.length)}</h4>
        <ul className="set-list tools">
          {result.tools.map((t) => <li key={t.name}><code>{t.name}</code><span className="hint">{t.summary}</span></li>)}
        </ul>
      </>)}
    </section>
  );
}
