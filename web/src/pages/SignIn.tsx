import { FormEvent, useEffect, useRef, useState } from "react";
import { authApi, isError } from "../api";
import { AUTH } from "../messages";
import type { AuthProviders } from "../types";
import { AgreeLine, Alert, AuthShell, PrimaryButton } from "./AuthShell";
import { GithubMark, GoogleMark, InviteIcon, MailIcon } from "./authIcons";

export const RESEND_AFTER = 30; // seconds before "Resend link" is offered
const LOOKS_LIKE_EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const PROVIDERS = [
  { name: "google", label: AUTH.withGoogle, Mark: GoogleMark },
  { name: "github", label: AUTH.withGithub, Mark: GithubMark },
];

// `?invite=CODE` is carried to the provider (and into the emailed link); `?error=` is a fixed code from the server.
export function SignIn({ search = window.location.search }: { search?: string }) {
  const params = new URLSearchParams(search);
  const invite = params.get("invite") ?? "";
  const error = params.get("error") ?? "";
  const [providers, setProviders] = useState<AuthProviders | null>(null);
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const [fieldError, setFieldError] = useState("");
  const [problem, setProblem] = useState("");
  const [until, setUntil] = useState(0); // when "Resend link" is offered (ms since the epoch)
  const [now, setNow] = useState(() => Date.now());
  const [resending, setResending] = useState(false);
  const [resent, setResent] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const sentHeading = useRef<HTMLHeadingElement>(null);
  const backToForm = useRef(false);
  useEffect(() => { authApi.providers().then((p) => { if (!isError(p)) setProviders(p); }); }, []);
  // The countdown to "Resend link", from a deadline (a throttled background tab still shows the right number).
  const wait = Math.max(0, Math.ceil((until - now) / 1000));
  const counting = wait > 0;
  useEffect(() => {
    if (!counting) return;
    const t = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(t);
  }, [counting]);
  const startCountdown = () => { const t = Date.now(); setNow(t); setUntil(t + RESEND_AFTER * 1000); };
  // Focus follows the swap: to the "Check your inbox" heading, and back to the field for a different email.
  useEffect(() => {
    if (state === "sent") sentHeading.current?.focus();
    if (state === "idle" && backToForm.current) { backToForm.current = false; input.current?.focus(); }
  }, [state]);

  const start = (name: string) => `/auth/${name}/start${invite ? `?invite=${encodeURIComponent(invite)}` : ""}`;
  // Asks for a link; says which message to show, if any (none when the link went).
  const request = async (address: string): Promise<{ field?: string; alert?: string }> => {
    const r = await authApi.requestLink(address, invite || undefined);
    if (!isError(r)) return {};
    if (r.error.code === "invalid_email") return { field: AUTH.invalidEmail };
    return { alert: r.error.code === "rate_limited" ? AUTH.rateLimited : AUTH.sendFailed };
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const address = email.trim();
    setProblem("");
    if (!LOOKS_LIKE_EMAIL.test(address)) {
      setFieldError(address ? AUTH.invalidEmail : AUTH.emailMissing);
      input.current?.focus();
      return;
    }
    setFieldError(""); setState("sending");
    const r = await request(address);
    if (!r.field && !r.alert) { setResent(false); startCountdown(); setState("sent"); return; }
    setState("idle");
    setFieldError(r.field ?? ""); setProblem(r.alert ?? "");
    if (r.field) input.current?.focus();
  };
  const resend = async () => {
    setProblem(""); setResent(false); setResending(true);
    const r = await request(email.trim());
    setResending(false);
    if (!r.field && !r.alert) { setResent(true); startCountdown(); return; }
    setProblem(r.field ?? r.alert ?? "");
  };
  const differentEmail = () => {
    backToForm.current = true;
    setEmail(""); setProblem(""); setResent(false); setUntil(0); setState("idle");
  };

  if (state === "sent") {
    return (
      <AuthShell mark={false}>
        <div className="au-badge"><MailIcon /></div>
        <div className="au-head">
          <h1 ref={sentHeading} tabIndex={-1}>{AUTH.checkInbox}</h1>
          <p className="au-sub" role="status">{AUTH.sentBefore}<b className="au-email">{email.trim()}</b>{AUTH.sentAfter}</p>
        </div>
        <p className="au-fine">{AUTH.sentHint}</p>
        {problem && <Alert>{problem}</Alert>}
        {resent && <p className="au-ok" role="status">{AUTH.resent}</p>}
        <div className="au-resend">
          <span>{AUTH.didntGet}</span>
          {wait > 0
            ? <span className="au-wait">{AUTH.resendIn(wait)}</span>
            : <button type="button" className="au-link-btn" onClick={resend} disabled={resending}>{AUTH.resend}</button>}
        </div>
        <button type="button" className="au-secondary" onClick={differentEmail}>{AUTH.differentEmail}</button>
      </AuthShell>
    );
  }

  const oauth = PROVIDERS.filter((p) => providers?.providers.includes(p.name));
  return (
    <AuthShell>
      <div className="au-head">
        <h1>{AUTH.signInTitle}</h1>
        <p className="au-sub">{AUTH.signInLead}</p>
      </div>
      {invite && (
        <div className="au-banner"><InviteIcon /><div><b>{AUTH.invitedTitle}</b> <span>{AUTH.invited}</span></div></div>
      )}
      {error && <Alert>{AUTH.errors[error] ?? AUTH.errors.oauth_failed}</Alert>}
      {oauth.length > 0 && (
        <>
          <div className="au-providers">
            {oauth.map(({ name, label, Mark }) => (
              <a key={name} className={`au-provider is-${name}`} href={start(name)}><Mark /><span>{label}</span></a>
            ))}
          </div>
          <div className="au-rule">{AUTH.or}</div>
        </>
      )}
      <form className="au-form" onSubmit={submit} noValidate>
        <div className="au-field">
          <label htmlFor="signin-email">{AUTH.emailLabel}</label>
          <input id="signin-email" ref={input} className="au-input" type="email" name="email" autoComplete="email"
            required placeholder={AUTH.emailPlaceholder} value={email} aria-invalid={fieldError ? true : undefined}
            aria-describedby={fieldError ? "signin-email-error" : undefined}
            onChange={(e) => { setEmail(e.target.value); if (fieldError) setFieldError(""); }} />
          {fieldError && <p id="signin-email-error" className="au-field-error" role="alert">{fieldError}</p>}
        </div>
        {problem && <Alert>{problem}</Alert>}
        <PrimaryButton busy={state === "sending"} busyLabel={AUTH.sending}>{AUTH.sendLink}</PrimaryButton>
      </form>
      <AgreeLine />
      <p className="au-switch">{AUTH.newHere} <a href="/waitlist">{AUTH.requestInvite}</a></p>
    </AuthShell>
  );
}
