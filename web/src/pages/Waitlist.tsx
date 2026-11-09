import { FormEvent, useEffect, useRef, useState } from "react";
import { authApi } from "../api";
import { AUTH } from "../messages";
import { AgreeLine, AuthShell, PrimaryButton } from "./AuthShell";
import { CheckIcon } from "./authIcons";

// Also where a sign-in with no account and no invite lands, so it explains itself.
export function Waitlist() {
  const [email, setEmail] = useState("");
  const [reading, setReading] = useState("");
  const [honeypot, setHoneypot] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "done">("idle");
  const doneHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { if (state === "done") doneHeading.current?.focus(); }, [state]);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setState("sending");
    await authApi.joinWaitlist(email.trim(), reading.trim(), true, honeypot);
    setState("done"); // the server answers 202 whatever happened, and so does this page
  };
  if (state === "done") {
    return (
      <AuthShell mark={false}>
        <div className="au-badge is-ok"><CheckIcon /></div>
        <div className="au-head">
          <h1 ref={doneHeading} tabIndex={-1}>{AUTH.waitlistDone}</h1>
          <p className="au-sub" role="status">{AUTH.thanks}</p>
          <p className="au-fine">{AUTH.spamHint}</p>
        </div>
        <p className="au-switch">{AUTH.haveAccount} <a href="/signin">{AUTH.signIn}</a></p>
      </AuthShell>
    );
  }
  return (
    <AuthShell>
      <div className="au-head">
        <h1>{AUTH.waitlistTitle}</h1>
        <p className="au-sub">{AUTH.waitlistLead}</p>
      </div>
      <form className="au-form" onSubmit={submit}>
        <div className="au-field">
          <label htmlFor="waitlist-email">{AUTH.emailLabel}</label>
          <input id="waitlist-email" className="au-input" type="email" name="email" autoComplete="email" required
            placeholder={AUTH.emailPlaceholder} value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="au-field">
          <label htmlFor="waitlist-reading">{AUTH.readingLabel}</label>
          <textarea id="waitlist-reading" className="au-input" name="reading" rows={2} maxLength={500} value={reading}
            onChange={(e) => setReading(e.target.value)} />
        </div>
        {/* The honeypot: invisible to people, tempting to bots. */}
        <div className="hp" aria-hidden="true">
          <input type="text" name="website" tabIndex={-1} autoComplete="off" value={honeypot} onChange={(e) => setHoneypot(e.target.value)} />
        </div>
        <PrimaryButton busy={state === "sending"} busyLabel={AUTH.submitting}>{AUTH.submit}</PrimaryButton>
        <p className="au-fine">{AUTH.consentLine}</p>
      </form>
      <AgreeLine />
      <p className="au-switch">{AUTH.haveAccount} <a href="/signin">{AUTH.signIn}</a></p>
    </AuthShell>
  );
}
