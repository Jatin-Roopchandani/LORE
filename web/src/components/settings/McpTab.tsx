import { useEffect, useState, type FormEvent } from "react";
import { api, isError } from "../../api";
import { hostedSnippets } from "../../mcpSnippets";
import { SETTINGS, UI } from "../../messages";
import { ago } from "../../settings";
import type { McpCheck, McpHosted, McpInfo, McpToken, McpTokenMade } from "../../types";
import { Snip } from "../Snip";

// MCP connector. Local mode: what it does, the exact snippets for Claude Desktop, Claude Code and HTTP (built by the
// server from the real `lore` path and this library), and Check, which builds the server and lists its tools.
// Multi mode: the hosted server's address, the user's access tokens (create one, shown once; revoke), the setup
// snippets built from the server's real address, and Check.
export function McpTab({ multi, mcp }: { multi: boolean; mcp: McpInfo | null }) {
  if (multi) return <Hosted />;
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

function Hosted() {
  const [info, setInfo] = useState<McpHosted | null>(null);
  const [tokens, setTokens] = useState<McpToken[] | null>(null);
  const [max, setMax] = useState(10);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [made, setMade] = useState<McpTokenMade | null>(null);
  const [problem, setProblem] = useState("");
  const load = () => api.mcpTokens().then((r) => {
    if (isError(r) || !Array.isArray(r.tokens)) { setProblem(SETTINGS.mcpTokensFailed); return; }
    setTokens(r.tokens); setMax(r.max_tokens);
  });
  useEffect(() => {
    api.mcpHosted().then((r) => { if (!isError(r) && r.hosted) setInfo(r); });
    load();
  }, []);
  const create = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) { setProblem(SETTINGS.mcpTokenNameNeeded); return; }
    setBusy(true); setProblem("");
    const r = await api.createMcpToken(name.trim());
    setBusy(false);
    if (isError(r)) { setProblem(r.error.code === "too_many_tokens" ? SETTINGS.mcpTokenMax(max) : SETTINGS.mcpTokenFailed); return; }
    setMade(r); setName("");
    load();
  };
  const revoke = async (id: string) => {
    setProblem("");
    const r = await api.revokeMcpToken(id);
    if (isError(r)) setProblem(SETTINGS.mcpRevokeFailed);
    if (made?.id === id) setMade(null);
    load();
  };
  const full = tokens !== null && tokens.length >= max;
  const snips = info ? hostedSnippets(info.mcp_url, made?.token ?? SETTINGS.mcpTokenPlaceholder) : null;
  return (<>
    <p className="set-lead-text">{SETTINGS.mcpWhatHosted}</p>
    {!info ? <p className="hint">{SETTINGS.mcpLoading}</p> : (
      <section className="set-section">
        <h4>{SETTINGS.mcpAddress}</h4>
        <Snip text={info.mcp_url} label={SETTINGS.mcpAddressLabel} />
      </section>
    )}
    <section className="set-section" aria-labelledby="set-mcp-tokens">
      <h4 id="set-mcp-tokens">{SETTINGS.mcpTokens}</h4>
      <p className="hint">{SETTINGS.mcpTokensHint}</p>
      <form className="mcp-new" onSubmit={create}>
        <label className="set-label" htmlFor="mcp-token-name">{SETTINGS.mcpTokenName}</label>
        <div className="key-actions">
          <input id="mcp-token-name" type="text" autoComplete="off" maxLength={100} value={name} disabled={full}
            placeholder={SETTINGS.mcpTokenNamePlaceholder} onChange={(e) => setName(e.target.value)} />
          <button className="tbtn primary" type="submit" disabled={busy || full}>{busy ? SETTINGS.mcpCreating : SETTINGS.mcpCreate}</button>
        </div>
      </form>
      {full && <p className="hint">{SETTINGS.mcpTokenMax(max)}</p>}
      {made && (
        <div className="notice" role="status">
          <strong>{SETTINGS.mcpTokenOnce}</strong>
          <Snip text={made.token} label={SETTINGS.mcpTokenLabel} />
        </div>
      )}
      {tokens && tokens.length === 0 && <p className="hint">{SETTINGS.mcpTokenNone}</p>}
      {tokens && tokens.length > 0 && (
        <ul className="set-list mcp-tokens">
          {tokens.map((t) => (
            <li key={t.id}>
              <span className="set-row-main">{t.name}</span>
              <code className="mcp-prefix">{SETTINGS.mcpTokenPrefix(t.prefix)}</code>
              <span className="hint">{SETTINGS.mcpTokenWhen(ago(t.created_at), t.last_used_at ? ago(t.last_used_at) : null)}</span>
              <button className="tbtn" type="button" aria-label={SETTINGS.mcpTokenRevokeLabel(t.name)} onClick={() => revoke(t.id)}>
                {SETTINGS.mcpTokenRevoke}</button>
            </li>
          ))}
        </ul>
      )}
      {problem && <div className="notice error" role="alert">{problem}</div>}
    </section>
    {snips && (
      <section className="set-section">
        <h4>{SETTINGS.mcpSetup}</h4>
        <p className="hint">{SETTINGS.mcpSetupHint}</p>
        <p className="set-label">{SETTINGS.mcpSetupCode}</p>
        <p className="hint">{SETTINGS.mcpSetupCodeHint}</p>
        <Snip text={snips.claudeCode} label={SETTINGS.mcpSetupCode} />
        <p className="set-label">{SETTINGS.mcpSetupJson}</p>
        <p className="hint">{SETTINGS.mcpSetupJsonHint}</p>
        <Snip text={snips.json} label={SETTINGS.mcpSetupJson} />
        <p className="set-label">{SETTINGS.mcpSetupCursor}</p>
        <p className="hint">{SETTINGS.mcpSetupCursorHint}</p>
        <Snip text={snips.cursor} label={SETTINGS.mcpSetupCursor} />
      </section>
    )}
    <Check hosted />
  </>);
}

function Check({ hosted = false }: { hosted?: boolean }) {
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
        {result && <span className={`status${result.ok ? " on" : " off"}`} role="status"><i />{result.ok ? (hosted ? SETTINGS.mcpCheckHosted : SETTINGS.mcpOk) : SETTINGS.mcpFailed}</span>}
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
