import { useEffect, useState } from "react";
import { authApi, isError, nav } from "../../api";
import { AUTH, SETTINGS } from "../../messages";
import { ago } from "../../settings";
import type { DeviceSession, Me } from "../../types";

const methodName = (p: string) => SETTINGS.methodNames[p] ?? p;

export async function signOut() {
  await authApi.logout();
  nav.to("/");
}

// Account (multi-user mode): name and email, the linked sign-in methods with "Connect" for the others, the signed-in
// devices (sign one out, or all the others), and Sign out of this one.
export function AccountTab({ me }: { me: Me }) {
  const [oauth, setOauth] = useState<string[]>([]);
  useEffect(() => { authApi.providers().then((p) => { if (!isError(p) && Array.isArray(p.providers)) setOauth(p.providers); }); }, []);
  const linked = me.identities ?? [];
  const unlinked = oauth.filter((p) => !linked.some((i) => i.provider === p));
  return (
    <>
      <section className="set-section">
        <dl className="set-facts">
          <div><dt>{SETTINGS.name}</dt><dd>{me.user.name || <span className="hint">{SETTINGS.noName}</span>}</dd></div>
          <div><dt>{SETTINGS.email}</dt><dd>{me.user.email}</dd></div>
        </dl>
      </section>
      <section className="set-section" aria-labelledby="set-methods">
        <h4 id="set-methods">{SETTINGS.methods}</h4>
        <ul className="set-list">
          {linked.map((i) => (
            <li key={i.provider}>
              <span className="set-row-main">{methodName(i.provider)}</span>
              <span className="hint">{SETTINGS.linkedSince(ago(i.created_at))}</span>
            </li>
          ))}
        </ul>
        <p className="hint">{SETTINGS.emailAlways(me.user.email)}</p>
        {unlinked.length > 0 && (<>
          <div className="key-actions">
            {unlinked.map((p) => <a key={p} className="tbtn" href={`/auth/${encodeURIComponent(p)}/start`}>{SETTINGS.connect(methodName(p))}</a>)}
          </div>
          <p className="hint">{SETTINGS.connectHint(me.user.email)}</p>
        </>)}
      </section>
      <Devices />
      <section className="set-section">
        <div className="key-actions"><button className="tbtn" type="button" onClick={signOut}>{AUTH.signOut}</button></div>
      </section>
    </>
  );
}

function Devices() {
  const [list, setList] = useState<DeviceSession[] | null>(null);
  const [problem, setProblem] = useState("");
  const [note, setNote] = useState("");
  const load = () => authApi.sessions().then((r) => {
    if (isError(r) || !Array.isArray(r.sessions)) { setProblem(SETTINGS.devicesFailed); return; }
    setList(r.sessions);
  });
  useEffect(() => { load(); }, []);
  const revoke = async (id: string) => {
    setProblem(""); setNote("");
    const r = await authApi.revokeSession(id);
    if (isError(r)) setProblem(SETTINGS.revokeFailed);
    load();
  };
  const others = async () => {
    setProblem(""); setNote("");
    const r = await authApi.signOutOthers();
    if (isError(r)) { setProblem(SETTINGS.revokeFailed); return; }
    setNote(SETTINGS.revokedOthers(r.revoked));
    load();
  };
  const otherCount = list?.filter((s) => !s.current).length ?? 0;
  return (
    <section className="set-section" aria-labelledby="set-devices">
      <h4 id="set-devices">{SETTINGS.devices}</h4>
      {list && (
        <ul className="set-list devices">
          {list.map((s) => (
            <li key={s.id}>
              <span className="set-row-main">{s.device}{s.current && <span className="badge">{SETTINGS.thisDevice}</span>}</span>
              <span className="hint">{SETTINGS.lastActive(ago(s.last_active))}</span>
              {!s.current && (
                <button className="tbtn" type="button" aria-label={SETTINGS.revokeLabel(s.device)} onClick={() => revoke(s.id)}>
                  {SETTINGS.revoke}</button>
              )}
            </li>
          ))}
        </ul>
      )}
      {list && otherCount === 0 && <p className="hint">{SETTINGS.onlyThisDevice}</p>}
      {otherCount > 0 && (
        <div className="key-actions"><button className="tbtn" type="button" onClick={others}>{SETTINGS.revokeOthers}</button></div>
      )}
      {note && <p className="hint" role="status">{note}</p>}
      {problem && <div className="notice error" role="alert">{problem}</div>}
    </section>
  );
}
