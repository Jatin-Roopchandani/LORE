import { useCallback, useEffect, useState } from "react";
import { adminApi, isError } from "../api";
import { ADMIN } from "../messages";
import type { AdminUser, NewInvite, WaitlistEntry } from "../types";
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

export function Admin() {
  const [entries, setEntries] = useState<WaitlistEntry[] | null>(null);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "forbidden" | "failed">("loading");
  const [notice, setNotice] = useState("");
  const [days, setDays] = useState(14);
  const [invite, setInvite] = useState<NewInvite | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
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
