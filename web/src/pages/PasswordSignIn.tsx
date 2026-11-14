import { FormEvent, useState } from "react";
import { isError, nav, selfhostApi } from "../api";
import { SELFHOST } from "../messages";
import { Alert, AuthShell, PrimaryButton } from "./AuthShell";

// Self-host mode's sign-in page: email and password (lore/auth/selfhost.py). The answer to a wrong password and to
// an unknown address is the same, on purpose.
export function PasswordSignIn() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setProblem("");
    if (!email.trim() || !password) { setProblem(SELFHOST.invalid); return; }
    setBusy(true);
    const r = await selfhostApi.signIn(email.trim(), password);
    setBusy(false);
    if (!isError(r)) { nav.to(r.redirect || "/app"); return; }
    setPassword("");
    setProblem(r.error.code === "rate_limited" ? SELFHOST.rateLimited
      : r.error.code === "invalid_credentials" || r.error.code === "http_401" ? SELFHOST.invalid : SELFHOST.failed);
  };

  return (
    <AuthShell>
      <div className="au-head">
        <h1>{SELFHOST.signInTitle}</h1>
        <p className="au-sub">{SELFHOST.signInLead}</p>
      </div>
      <form className="au-form" onSubmit={submit} noValidate>
        <div className="au-field">
          <label htmlFor="signin-email">{SELFHOST.emailLabel}</label>
          <input id="signin-email" className="au-input" type="email" name="email" autoComplete="username" required
            value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="au-field">
          <label htmlFor="signin-password">{SELFHOST.passwordLabel}</label>
          <input id="signin-password" className="au-input" type="password" name="password" autoComplete="current-password"
            required value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {problem && <Alert>{problem}</Alert>}
        <PrimaryButton busy={busy} busyLabel={SELFHOST.signingIn}>{SELFHOST.signInButton}</PrimaryButton>
      </form>
      <p className="au-fine">{SELFHOST.forgot}</p>
    </AuthShell>
  );
}
