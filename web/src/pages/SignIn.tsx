import { FormEvent, useEffect, useState } from "react";
import { authApi, isError } from "../api";
import { AUTH } from "../messages";
import type { AuthProviders } from "../types";
import { PageShell } from "./PageShell";

// `?invite=CODE` is carried to the provider (and into the emailed link); `?error=` is a fixed code from the server.
export function SignIn({ search = window.location.search }: { search?: string }) {
  const params = new URLSearchParams(search);
  const invite = params.get("invite") ?? "";
  const error = params.get("error") ?? "";
  const [providers, setProviders] = useState<AuthProviders | null>(null);
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const [problem, setProblem] = useState("");
  useEffect(() => { authApi.providers().then((p) => { if (!isError(p)) setProviders(p); }); }, []);
  const start = (name: string) => `/auth/${name}/start${invite ? `?invite=${encodeURIComponent(invite)}` : ""}`;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setProblem(""); setState("sending");
    const r = await authApi.requestLink(email.trim(), invite || undefined);
    if (!isError(r)) { setState("sent"); return; }
    setState("idle");
    setProblem(r.error.code === "invalid_email" ? AUTH.invalidEmail : r.error.code === "rate_limited" ? AUTH.rateLimited : AUTH.sendFailed);
  };
  const oauth = [["google", AUTH.withGoogle], ["github", AUTH.withGithub]].filter(([n]) => providers?.providers.includes(n));
  return (
    <PageShell>
      <h1>{AUTH.signInTitle}</h1>
      <p>{AUTH.signInLead}</p>
      {error && <div className="notice error" role="alert">{AUTH.errors[error] ?? AUTH.errors.oauth_failed}</div>}
      {invite && <div className="notice">{AUTH.invited}</div>}
      {oauth.map(([name, label]) => <a key={name} className="btn-provider" href={start(name)}>{label}</a>)}
      {oauth.length > 0 && <div className="rule">{AUTH.or}</div>}
      {state === "sent" ? <div className="notice" role="status">{AUTH.linkSent}</div> : (
        <form onSubmit={submit}>
          <label>{AUTH.emailLabel}
            <input type="email" name="email" autoComplete="email" required placeholder={AUTH.emailPlaceholder} value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          {problem && <div className="notice error" role="alert">{problem}</div>}
          <button className="btn-new" type="submit" disabled={state === "sending"}>{state === "sending" ? AUTH.sending : AUTH.sendLink}</button>
        </form>
      )}
      <p>{AUTH.newHere} <a href="/waitlist">{AUTH.requestInvite}</a></p>
    </PageShell>
  );
}
