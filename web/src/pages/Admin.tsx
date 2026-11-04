import { useCallback, useEffect, useState } from "react";
import { adminApi, isError } from "../api";
import { ADMIN } from "../messages";
import type { AdminReport, AdminUser, NewInvite, TraceRecord, WaitlistEntry } from "../types";
import { PageShell } from "./PageShell";

const day = (iso: string) => iso.slice(0, 10);

function CopyLine({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => { try { navigator.clipboard.writeText(value).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1400); }, () => {}); } catch { /* no clipboard */ } };
  return (
    <div className="row"><strong>{label}</strong><code style={{ wordBreak: "break-all" }}>{value}</code>
      <button className="tbtn" type="button" onClick={copy}>{copied ? ADMIN.copied : ADMIN.copy}</button></div>
  );
}

// A trace rendered readably: the question, the decomposition, each model call collapsible, then the answer.
export function TraceView({ trace, onClose }: { trace: TraceRecord; onClose: () => void }) {
  const calls = Array.isArray(trace.calls) ? trace.calls : [];
  return (
    <section className="trace-view" aria-label={ADMIN.traceTitle}>
      <div className="row"><h2>{ADMIN.traceTitle} <code>{trace.request_id}</code></h2>
        <button className="tbtn" type="button" onClick={onClose}>{ADMIN.traceClose}</button></div>
      <p><strong>{ADMIN.traceQuestion}:</strong> {trace.question}</p>
      {trace.decomposition && (<><h3>{ADMIN.traceDecomposition}</h3><pre>{JSON.stringify(trace.decomposition, null, 2)}</pre></>)}
      <h3>{ADMIN.traceCalls(calls.length)}</h3>
      {calls.map((c, i) => (
        <details key={i}>
          <summary className={c.ok ? undefined : "failed"}>{`${i + 1}. ${c.stage} · ${c.model ?? "?"} · ${c.latency_ms ?? "?"} ms`}
            {c.ok ? "" : ` · ${c.error ?? ""}`}</summary>
          {(c.attempts ?? []).length > 1 && <ul>{c.attempts!.map((a, j) => <li key={j}>{a.model}: {a.ok ? "ok" : a.error}</li>)}</ul>}
          <strong>{ADMIN.tracePrompt}</strong><pre>{c.prompt}</pre>
          <strong>{ADMIN.traceReply}</strong><pre>{c.reply ?? c.error}</pre>
        </details>
      ))}
      {trace.error && <p className="notice error"><strong>{ADMIN.traceError}:</strong> {trace.error}</p>}
      {trace.answer && (<><h3>{ADMIN.traceAnswer}</h3><pre>{typeof trace.answer.answer === "string" ? trace.answer.answer : JSON.stringify(trace.answer, null, 2)}</pre></>)}
    </section>
  );
}

export function Admin() {
  const [entries, setEntries] = useState<WaitlistEntry[] | null>(null);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "forbidden" | "failed">("loading");
  const [notice, setNotice] = useState("");
  const [days, setDays] = useState(14);
  const [invite, setInvite] = useState<NewInvite | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [reports, setReports] = useState<AdminReport[]>([]);
  const [trace, setTrace] = useState<TraceRecord | null>(null);
  const loadReports = useCallback(async () => {
    const r = await adminApi.reports();
    setReports(!isError(r) && Array.isArray(r.reports) ? r.reports : []);
  }, []);
  useEffect(() => { void loadReports(); }, [loadReports]);
  const openTrace = async (rep: AdminReport) => {
    setNotice("");
    const t = await adminApi.trace(rep.request_id);
    // A trace has an `error` field of its own (null when it answered), so isError cannot tell them apart.
    if (!("request_id" in t)) { setNotice(ADMIN.traceGone); return; }
    setTrace(t as TraceRecord);
  };
  const resolve = async (rep: AdminReport) => {
    const r = await adminApi.resolveReport(rep.id);
    if (isError(r)) { setNotice(ADMIN.actionFailed); return; }
    void loadReports();
  };
  const load = useCallback(async () => {
    const [w, u] = await Promise.all([adminApi.waitlist(), adminApi.users()]);
    if (isError(w) || isError(u)) {
      const code = (isError(w) ? w : (u as { error: { code: string } })).error.code;
      setStatus(code === "http_403" ? "forbidden" : "failed");
      return;
    }
    setEntries(w.entries); setUsers(u.users); setStatus("ready");
  }, []);
  useEffect(() => { load(); }, [load]);
  const act = async (entry: WaitlistEntry, kind: "approve" | "decline") => {
    setBusy(entry.id); setNotice("");
    const r = await (kind === "approve" ? adminApi.approve(entry.id) : adminApi.decline(entry.id));
    setBusy(null);
    if (isError(r)) { setNotice(ADMIN.actionFailed); return; }
    setNotice(kind === "approve" ? ADMIN.approved(entry.email) : ADMIN.declined(entry.email));
    load();
  };
  const create = async () => {
    setNotice("");
    const r = await adminApi.createInvite(days);
    if (isError(r)) { setNotice(ADMIN.actionFailed); return; }
    setInvite(r);
  };
  if (status === "forbidden") return <PageShell><h1>{ADMIN.title}</h1><div className="notice error" role="alert">{ADMIN.forbidden}</div></PageShell>;
  if (status === "failed") return <PageShell><h1>{ADMIN.title}</h1><div className="notice error" role="alert">{ADMIN.loadFailed}</div></PageShell>;
  return (
    <PageShell wide>
      <h1>{ADMIN.title}</h1>
      {notice && <div className="notice" role="status">{notice}</div>}
      <h2>{ADMIN.waitlist}</h2>
      {entries && entries.length === 0 ? <p>{ADMIN.noRequests}</p> : (
        <div className="admin-scroll"><table className="admin-table" aria-label={ADMIN.waitlist}>
          <thead><tr>{Object.values(ADMIN.columns).map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
          <tbody>{(entries ?? []).map((e) => (
            <tr key={e.id}>
              <td>{e.email}</td><td>{e.reading}</td><td>{day(e.created_at)}</td><td>{ADMIN.status[e.status] ?? e.status}</td>
              <td className="actions">
                {e.status !== "invited" && <button className="tbtn primary" type="button" disabled={busy === e.id} onClick={() => act(e, "approve")}>{ADMIN.approve}</button>}
                {e.status === "pending" && <button className="tbtn" type="button" disabled={busy === e.id} onClick={() => act(e, "decline")}>{ADMIN.decline}</button>}
              </td>
            </tr>))}</tbody>
        </table></div>
      )}
      <h2>{ADMIN.invites}</h2>
      <p>{ADMIN.invitesLead}</p>
      <div className="row">
        <label style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>{ADMIN.days}
          <input type="number" min={1} max={90} value={days} style={{ width: 80 }} onChange={(e) => setDays(Math.max(1, Math.min(90, Number(e.target.value) || 1)))} />
        </label>
        <button className="tbtn primary" type="button" onClick={create}>{ADMIN.createInvite}</button>
      </div>
      {invite && <div className="notice"><CopyLine label={ADMIN.inviteCode} value={invite.code} /><CopyLine label={ADMIN.inviteLink} value={invite.link} /></div>}
      <h2>{ADMIN.reports}</h2>
      {reports.length === 0 ? <p>{ADMIN.noReports}</p> : (
        <div className="admin-scroll"><table className="admin-table" aria-label={ADMIN.reports}>
          <thead><tr>{Object.values(ADMIN.reportColumns).map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
          <tbody>{reports.map((r) => (
            <tr key={r.id}>
              <td>{day(r.created_at)}</td><td>{r.email}</td><td>{r.question}</td><td>{r.note}</td><td>{ADMIN.reportStatus[r.status] ?? r.status}</td>
              <td className="actions">
                {r.trace && <button className="tbtn" type="button" onClick={() => void openTrace(r)}>{ADMIN.openTrace}</button>}
                {r.status === "open" && <button className="tbtn" type="button" onClick={() => void resolve(r)}>{ADMIN.resolve}</button>}
              </td>
            </tr>))}</tbody>
        </table></div>
      )}
      {trace && <TraceView trace={trace} onClose={() => setTrace(null)} />}
      <h2>{ADMIN.users}</h2>
      <div className="admin-scroll"><table className="admin-table" aria-label={ADMIN.users}>
        <thead><tr>{Object.values(ADMIN.userColumns).map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>{(users ?? []).map((u) => (
          <tr key={u.id}><td>{u.email}</td><td>{u.name}</td><td>{u.role}</td><td>{u.status}</td><td>{day(u.created_at)}</td>
            <td>{u.has_key ? ADMIN.hasKey : ADMIN.noKey}</td><td>{u.requests_today ?? 0}</td></tr>))}</tbody>
      </table></div>
    </PageShell>
  );
}
