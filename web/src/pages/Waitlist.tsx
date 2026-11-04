import { FormEvent, useState } from "react";
import { authApi } from "../api";
import { AUTH } from "../messages";
import { PageShell } from "./PageShell";

// Also where a sign-in with no account and no invite lands, so it explains itself.
export function Waitlist() {
  const [email, setEmail] = useState("");
  const [reading, setReading] = useState("");
  const [consent, setConsent] = useState(false);
  const [honeypot, setHoneypot] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "done">("idle");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setState("sending");
    await authApi.joinWaitlist(email.trim(), reading.trim(), consent, honeypot);
    setState("done"); // the server answers 202 whatever happened, and so does this page
  };
  return (
    <PageShell>
      <h1>{AUTH.waitlistTitle}</h1>
      <p>{AUTH.waitlistLead}</p>
      {state === "done" ? <div className="notice" role="status">{AUTH.thanks}</div> : (
        <form onSubmit={submit}>
          <label>{AUTH.emailLabel}
            <input type="email" name="email" autoComplete="email" required placeholder={AUTH.emailPlaceholder} value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <label>{AUTH.readingLabel}
            <textarea name="reading" rows={2} maxLength={500} value={reading} onChange={(e) => setReading(e.target.value)} />
          </label>
          {/* The honeypot: invisible to people, tempting to bots. */}
          <div className="hp" aria-hidden="true">
            <input type="text" name="website" tabIndex={-1} autoComplete="off" value={honeypot} onChange={(e) => setHoneypot(e.target.value)} />
          </div>
          <label className="check">
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} required />
            <span>{AUTH.consentLabel}</span>
          </label>
          <button className="btn-new" type="submit" disabled={state === "sending" || !consent}>{AUTH.submit}</button>
        </form>
      )}
      <a href="/signin">{AUTH.signIn}</a>
    </PageShell>
  );
}
