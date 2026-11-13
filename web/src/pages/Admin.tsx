import { FormEvent, useCallback, useEffect, useState } from "react";
import { adminApi, isError, selfhostApi } from "../api";
import { ServerProviderSettings } from "../components/ServerProviderSettings";
import { ADMIN, ADMIN_USERS, SELFHOST } from "../messages";
import { isSelfhost } from "../routing";
import type { AdminEvent, AdminReport, AdminUser, NewInvite, PasswordLink, TraceRecord, WaitlistEntry } from "../types";
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

// The set-password link as this browser reaches the server (the server's own LORE_BASE_URL may say localhost).
const linkHere = (link: PasswordLink) => `${window.location.origin}/set-password?code=${encodeURIComponent(link.code)}`;

// Self-host mode: add a person, with a password or a set-password link to send them by hand (there is no mail).
export function AddPerson({ onAdded }: { onAdded: () => void }) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [done, setDone] = useState<{ email: string; link?: PasswordLink } | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setProblem(""); setDone(null);
    if (password && password.length < 8) { setProblem(SELFHOST.tooShort); return; }
    setBusy(true);
    const r = await selfhostApi.addPerson({ email: email.trim(), ...(name.trim() ? { name: name.trim() } : {}),
      ...(password ? { password } : {}) });
    setBusy(false);
    if (isError(r)) { setProblem(r.error.code === "exists" ? SELFHOST.exists : r.error.message || ADMIN.actionFailed); return; }
    setDone({ email: r.user.email, link: r.link });
    setEmail(""); setName(""); setPassword("");
    onAdded();
  };
  return (
    <section aria-labelledby="add-person-title">
      <h3 id="add-person-title">{SELFHOST.addPerson}</h3>
      <form className="key-form add-person" onSubmit={submit}>
        <label>{SELFHOST.personEmail}
          <input type="email" name="person-email" autoComplete="off" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>{SELFHOST.personName}
          <input type="text" name="person-name" autoComplete="off" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label>{SELFHOST.personPassword}
          <input type="password" name="person-password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        <p className="hint">{SELFHOST.personPasswordHint}</p>
        {problem && <div className="notice error" role="alert">{problem}</div>}
        <div className="key-actions"><button className="tbtn primary" type="submit" disabled={busy || !email.trim()}>{SELFHOST.add}</button></div>
      </form>
      {done && (done.link
        ? <div className="notice" role="status"><p>{SELFHOST.addedWithLink(done.email)}</p><CopyLine label={ADMIN.inviteLink} value={linkHere(done.link)} /></div>
        : <div className="notice" role="status">{SELFHOST.added(done.email)}</div>)}
    </section>
  );
}

// Self-host mode: a person's password, reset by the admin (a new one typed here, or a link to send them).
function ResetPassword({ user, onDone }: { user: AdminUser; onDone: (note: string, link?: PasswordLink) => void }) {
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const run = async (withPassword: boolean) => {
    if (withPassword && password.length < 8) { onDone(SELFHOST.tooShort); return; }
    const r = await selfhostApi.resetPassword(user.id, withPassword ? password : undefined);
    setOpen(false); setPassword("");
    if (isError(r)) { onDone(ADMIN.actionFailed); return; }
    onDone(r.link ? SELFHOST.resetLinkFor(user.email) : SELFHOST.resetDone(user.email), r.link);
  };
  if (!open) return <button className="tbtn" type="button" onClick={() => setOpen(true)}>{SELFHOST.resetPassword}</button>;
  return (<>
    <input type="password" aria-label={SELFHOST.newPasswordFor(user.email)} autoComplete="new-password" value={password}
      onChange={(e) => setPassword(e.target.value)} style={{ width: 160 }} />
    <button className="tbtn primary" type="button" disabled={!password} onClick={() => void run(true)}>{SELFHOST.set}</button>
    <button className="tbtn" type="button" onClick={() => void run(false)}>{SELFHOST.resetLink}</button>
    <button className="tbtn" type="button" onClick={() => { setOpen(false); setPassword(""); }}>{SELFHOST.cancel}</button>
  </>);
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

export function Admin({ selfhost = isSelfhost() }: { selfhost?: boolean } = {}) {
  const [resetLink, setResetLink] = useState<PasswordLink | null>(null);
  const [entries, setEntries] = useState<WaitlistEntry[] | null>(null);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "forbidden" | "failed">("loading");
  const [notice, setNotice] = useState("");
  const [days, setDays] = useState(14);
  const [invite, setInvite] = useState<NewInvite | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [reports, setReports] = useState<AdminReport[]>([]);
  const [trace, setTrace] = useState<TraceRecord | null>(null);
  const [events, setEvents] = useState<AdminEvent[]>([]);
  const [removing, setRemoving] = useState<string | null>(null);
  const loadEvents = useCallback(async () => {
    const r = await adminApi.events();
    setEvents(!isError(r) && Array.isArray(r.events) ? r.events : []);
  }, []);
  useEffect(() => { void loadEvents(); }, [loadEvents]);
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
  // Disable (signs them out, refuses sign-in, pauses their jobs), enable, sign out everywhere, or delete.
  const control = async (u: AdminUser, kind: "disable" | "enable" | "revoke" | "remove") => {
    setNotice(""); setRemoving(null);
    const r = await (kind === "disable" ? adminApi.disable(u.id) : kind === "enable" ? adminApi.enable(u.id)
      : kind === "revoke" ? adminApi.revokeSessions(u.id) : adminApi.deleteUser(u.id));
    if (isError(r)) { setNotice(ADMIN.actionFailed); return; }
    setNotice(kind === "disable" ? ADMIN_USERS.disabled(u.email) : kind === "enable" ? ADMIN_USERS.enabled(u.email)
      : kind === "revoke" ? ADMIN_USERS.revoked(u.email) : ADMIN_USERS.removed(u.email));
    void load(); void loadEvents();
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
      {selfhost && resetLink && <div className="notice"><CopyLine label={ADMIN.inviteLink} value={linkHere(resetLink)} /></div>}
      {selfhost && <ServerProviderSettings />}
      {selfhost && (<><h2>{SELFHOST.people}</h2><p>{SELFHOST.peopleLead}</p><AddPerson onAdded={() => { void load(); void loadEvents(); }} /></>)}
      {!selfhost && (<>
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
      </>)}
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
          <tr key={u.id}><td>{u.email}{selfhost && u.has_password === false && <span className="hint"> ({SELFHOST.noPassword})</span>}</td>
            <td>{u.name}</td><td>{u.role}</td><td>{u.status}</td><td>{day(u.created_at)}</td>
            <td>{u.has_key ? ADMIN.hasKey : ADMIN.noKey}</td><td>{u.requests_today ?? 0}</td>
            <td className="actions">
              {removing === u.id ? (<>
                <span>{ADMIN_USERS.confirmRemove(u.email)}</span>
                <button className="tbtn primary" type="button" onClick={() => void control(u, "remove")}>{ADMIN_USERS.yes}</button>
                <button className="tbtn" type="button" onClick={() => setRemoving(null)}>{ADMIN_USERS.cancel}</button>
              </>) : (<>
                {u.status === "active" && <button className="tbtn" type="button" onClick={() => void control(u, "disable")}>{ADMIN_USERS.disable}</button>}
                {u.status === "disabled" && <button className="tbtn" type="button" onClick={() => void control(u, "enable")}>{ADMIN_USERS.enable}</button>}
                <button className="tbtn" type="button" onClick={() => void control(u, "revoke")}>{ADMIN_USERS.revoke}</button>
                {selfhost && <ResetPassword user={u} onDone={(note, link) => { setNotice(note); setResetLink(link ?? null); void load(); void loadEvents(); }} />}
                <button className="tbtn" type="button" onClick={() => setRemoving(u.id)}>{ADMIN_USERS.remove}</button>
              </>)}
            </td></tr>))}</tbody>
      </table></div>
      <h2>{ADMIN_USERS.events}</h2>
      {events.length === 0 ? <p>{ADMIN_USERS.noEvents}</p> : (
        <div className="admin-scroll"><table className="admin-table" aria-label={ADMIN_USERS.events}>
          <thead><tr>{Object.values(ADMIN_USERS.eventColumns).map((c) => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>{events.map((e) => (
            <tr key={e.id}><td>{e.at.slice(0, 16).replace("T", " ")}</td><td>{e.admin ?? ""}</td><td>{e.action}</td><td>{e.target_user_id ?? ""}</td></tr>))}</tbody>
        </table></div>
      )}
    </PageShell>
  );
}
