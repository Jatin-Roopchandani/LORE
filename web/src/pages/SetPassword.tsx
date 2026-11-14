import { FormEvent, useEffect, useState } from "react";
import { isError, nav, selfhostApi } from "../api";
import { SELFHOST } from "../messages";
import { Alert, AuthShell, PrimaryButton } from "./AuthShell";
import { passwordProblem } from "./Setup";

// The page an admin's set-password link opens (/set-password?code=...): a new person chooses their password, or
// someone whose password the admin reset chooses a new one. The link works once.
export function SetPassword({ search = window.location.search }: { search?: string }) {
  const code = new URLSearchParams(search).get("code") ?? "";
  const [email, setEmail] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  useEffect(() => {
    if (!code) { setInvalid(true); return; }
    selfhostApi.linkEmail(code).then((r) => { if (isError(r)) setInvalid(true); else setEmail(r.email); });
  }, [code]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const bad = passwordProblem(password, confirm);
    if (bad) { setProblem(bad); return; }
    setBusy(true); setProblem("");
    const r = await selfhostApi.useLink(code, password);
    setBusy(false);
    if (!isError(r)) { nav.to(r.redirect || "/app"); return; }
    if (r.error.code === "link_invalid") { setInvalid(true); return; }
    setProblem(r.error.message || SELFHOST.failed);
  };

  if (invalid) {
    return <AuthShell><div className="au-head"><h1>{SELFHOST.setTitle}</h1></div><Alert>{SELFHOST.linkInvalid}</Alert></AuthShell>;
  }
  if (email === null) return <AuthShell><div className="au-head"><h1>{SELFHOST.setTitle}</h1></div></AuthShell>;
  return (
    <AuthShell>
      <div className="au-head">
        <h1>{SELFHOST.setTitle}</h1>
        <p className="au-sub">{SELFHOST.setLead(email)}</p>
      </div>
      <form className="au-form" onSubmit={submit} noValidate>
        {/* For password managers: the account the new password belongs to. */}
        <input type="email" name="username" autoComplete="username" value={email} readOnly hidden />
        <div className="au-field">
          <label htmlFor="set-password">{SELFHOST.newPasswordLabel}</label>
          <input id="set-password" className="au-input" type="password" autoComplete="new-password" required
            value={password} onChange={(e) => setPassword(e.target.value)} />
          <p className="au-fine">{SELFHOST.passwordHint}</p>
        </div>
        <div className="au-field">
          <label htmlFor="set-confirm">{SELFHOST.confirmLabel}</label>
          <input id="set-confirm" className="au-input" type="password" autoComplete="new-password" required
            value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
        {problem && <Alert>{problem}</Alert>}
        <PrimaryButton busy={busy} busyLabel={SELFHOST.saving}>{SELFHOST.setButton}</PrimaryButton>
      </form>
    </AuthShell>
  );
}
