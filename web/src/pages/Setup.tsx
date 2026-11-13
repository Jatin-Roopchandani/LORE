import { FormEvent, useRef, useState } from "react";
import { isError, nav, selfhostApi } from "../api";
import { SELFHOST } from "../messages";
import { Alert, AuthShell, PrimaryButton } from "./AuthShell";

const LOOKS_LIKE_EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export const MIN_PASSWORD = 8;

// The two password fields every self-host form uses: says what is wrong before anything is sent.
export function passwordProblem(password: string, confirm: string): string {
  if (password.length < MIN_PASSWORD) return SELFHOST.tooShort;
  if (password !== confirm) return SELFHOST.mismatch;
  return "";
}

// Self-host mode's first run (lore/auth/firstrun.py): until the admin exists, every address comes here. Creating it
// signs the admin in and opens the app; a second attempt (another browser got there first) says so.
export function Setup() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [done, setDone] = useState(false);
  const first = useRef<HTMLInputElement>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setProblem("");
    const address = email.trim();
    if (!LOOKS_LIKE_EMAIL.test(address)) { setProblem(SELFHOST.emailMissing); return; }
    const bad = passwordProblem(password, confirm);
    if (bad) { setProblem(bad); return; }
    setBusy(true);
    const r = await selfhostApi.setup({ email: address, password, confirm, ...(name.trim() ? { name: name.trim() } : {}) });
    setBusy(false);
    if (!isError(r)) { nav.to(r.redirect || "/app"); return; }
    if (r.error.code === "setup_done") { setDone(true); return; }
    setProblem(r.error.message || SELFHOST.failed);
  };

  if (done) {
    return (
      <AuthShell>
        <div className="au-head"><h1>{SELFHOST.setupDone}</h1></div>
        <a className="au-primary" href="/signin">{SELFHOST.goSignIn}</a>
      </AuthShell>
    );
  }
  return (
    <AuthShell>
      <div className="au-head">
        <h1>{SELFHOST.setupTitle}</h1>
        <p className="au-sub">{SELFHOST.setupLead}</p>
      </div>
      <form className="au-form" onSubmit={submit} noValidate>
        <div className="au-field">
          <label htmlFor="setup-name">{SELFHOST.nameLabel}</label>
          <input id="setup-name" ref={first} className="au-input" type="text" autoComplete="name" value={name}
            onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="au-field">
          <label htmlFor="setup-email">{SELFHOST.emailLabel}</label>
          <input id="setup-email" className="au-input" type="email" autoComplete="email" required value={email}
            onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="au-field">
          <label htmlFor="setup-password">{SELFHOST.passwordLabel}</label>
          <input id="setup-password" className="au-input" type="password" autoComplete="new-password" required
            value={password} aria-describedby="setup-password-hint" onChange={(e) => setPassword(e.target.value)} />
          <p id="setup-password-hint" className="au-fine">{SELFHOST.passwordHint}</p>
        </div>
        <div className="au-field">
          <label htmlFor="setup-confirm">{SELFHOST.confirmLabel}</label>
          <input id="setup-confirm" className="au-input" type="password" autoComplete="new-password" required
            value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
        {problem && <Alert>{problem}</Alert>}
        <PrimaryButton busy={busy} busyLabel={SELFHOST.creating}>{SELFHOST.createAdmin}</PrimaryButton>
      </form>
    </AuthShell>
  );
}
