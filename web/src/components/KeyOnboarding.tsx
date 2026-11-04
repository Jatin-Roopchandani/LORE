import { FormEvent, useState } from "react";
import { isError, keyApi } from "../api";
import { KEYS } from "../messages";
import { PageShell } from "../pages/PageShell";
import type { KeySaved } from "../types";

// The paste field and "Check and save" button, shared by the onboarding screen and the account menu's Replace.
// The key lives only in this field's state until the server answers; it is cleared either way.
export function KeyForm({ onSaved, onCancel }: { onSaved: (saved: KeySaved) => void; onCancel?: () => void }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setProblem("");
    const r = await keyApi.save(key.trim());
    setBusy(false);
    if (isError(r)) {
      setProblem(KEYS.errors[r.error.code] ?? KEYS.saveFailed);
      if (r.error.code !== "rate_limited") setKey("");
      return;
    }
    setKey("");
    onSaved(r);
  };
  return (
    <form className="key-form" onSubmit={submit}>
      <label>{KEYS.fieldLabel}
        <input type="password" name="gemini-key" autoComplete="off" spellCheck={false} required placeholder={KEYS.placeholder}
          value={key} onChange={(e) => setKey(e.target.value)} />
      </label>
      {problem && <div className="notice error" role="alert">{problem}</div>}
      <div className="key-actions">
        <button className="btn-new" type="submit" disabled={busy || !key.trim()}>{busy ? KEYS.checking : KEYS.save}</button>
        {onCancel && <button className="tbtn" type="button" onClick={onCancel}>{KEYS.cancel}</button>}
      </div>
    </form>
  );
}

// Shown inside /app, in place of the reading app, while the signed-in user has no working key: on first sign-in
// (`missing`), or after Gemini refused the saved one (`invalid`).
export function KeyOnboarding({ reason, onDone }: { reason: "missing" | "invalid"; onDone: () => void }) {
  const [warning, setWarning] = useState<string | null>(null);
  const saved = (r: KeySaved) => { if (r.warning) setWarning(r.warning); else onDone(); };
  return (
    <PageShell>
      <h1>{reason === "invalid" ? KEYS.replaceTitle : KEYS.onboardingTitle}</h1>
      {reason === "invalid" && <div className="notice error" role="alert">{KEYS.rejected}</div>}
      <p className="lead">{KEYS.why}</p>
      <p><a href={KEYS.getKeyUrl} target="_blank" rel="noopener noreferrer">{KEYS.getKey}</a></p>
      <p>{KEYS.freeTier}</p>
      {warning ? (<>
        <div className="notice" role="status">{KEYS.saved} {warning}</div>
        <button className="btn-new" type="button" onClick={onDone}>{KEYS.continue}</button>
      </>) : <KeyForm onSaved={saved} />}
      <p className="hint">{KEYS.privacy}</p>
    </PageShell>
  );
}
