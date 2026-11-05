import { FormEvent, useRef, useState } from "react";
import { authApi, isError } from "../../api";
import { HOME } from "../../messages";
import { NARROW, useMedia, usePinProgress } from "./scroll";

const SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// The server answers 202 whatever happened (stored, a duplicate, the honeypot...), so any success is "on the list".
// What can go wrong is the address (checked here first), a rate limit, or the network.
function problemFor(code: string): string {
  if (code === "rate_limited" || code === "http_429") return HOME.rateLimited;
  if (code === "unreachable") return HOME.offline;
  if (code === "invalid_email" || code === "http_422") return HOME.invalidEmail;
  return HOME.failed;
}

export function WaitlistForm({ ready }: { ready: boolean }) {
  const [email, setEmail] = useState("");
  const [reading, setReading] = useState("");
  const [consent, setConsent] = useState(false);
  const [honeypot, setHoneypot] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "done">("idle");
  const [problem, setProblem] = useState<{ text: string; field?: "email" | "consent" } | null>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (state !== "idle") return;
    const address = email.trim();
    if (!SHAPE.test(address)) { setProblem({ text: HOME.invalidEmail, field: "email" }); emailRef.current?.focus(); return; }
    if (!consent) { setProblem({ text: HOME.needConsent, field: "consent" }); return; }
    setProblem(null);
    setState("sending");
    const r = await authApi.joinWaitlist(address, reading.trim(), consent, honeypot);
    if (!isError(r)) { setState("done"); return; }
    setState("idle");
    setProblem({ text: problemFor(r.error.code) });
  };
  const done = state === "done";
  return (
    <form className={`h-card h-waitlist${ready ? " is-ready" : ""}`} id="waitlist" noValidate onSubmit={submit} aria-labelledby="h-form-title">
      <h3 className="h-form-title" id="h-form-title">{HOME.formTitle}</h3>
      <label htmlFor="h-email">{HOME.emailLabel}</label>
      <input id="h-email" ref={emailRef} type="email" name="email" required autoComplete="email" placeholder={HOME.emailPlaceholder}
        value={email} onChange={(e) => setEmail(e.target.value)} readOnly={done}
        aria-invalid={problem?.field === "email" ? true : undefined} aria-describedby={problem ? "h-problem" : undefined} />
      <label htmlFor="h-reading">{HOME.readingLabel} <span className="h-opt">{HOME.optional}</span></label>
      <input id="h-reading" type="text" name="reading" maxLength={500} placeholder={HOME.readingPlaceholder}
        value={reading} onChange={(e) => setReading(e.target.value)} readOnly={done} />
      {/* The honeypot: off-screen and out of the tab order for people, tempting to bots. */}
      <input className="h-hp" type="text" name="website" tabIndex={-1} autoComplete="off" aria-hidden="true"
        value={honeypot} onChange={(e) => setHoneypot(e.target.value)} />
      <label className="h-consent">
        <input type="checkbox" name="consent" required checked={consent} onChange={(e) => setConsent(e.target.checked)} disabled={done}
          aria-invalid={problem?.field === "consent" ? true : undefined} />
        {HOME.consent}
      </label>
      {problem && <p className="h-problem" id="h-problem" role="alert">{problem.text}</p>}
      <button className="h-btn is-primary" type="submit" disabled={state !== "idle"}>
        {done ? HOME.done : state === "sending" ? HOME.sending : HOME.submit}
      </button>
      <p className="h-sr" role="status">{done ? HOME.done : ""}</p>
    </form>
  );
}

// "How the beta works": the steps light up in turn beside the form, which is highlighted once the last one is
// reached. On a phone the scene isn't pinned (home.css) and every step shows at once.
export function Beta() {
  const scene = useRef<HTMLElement>(null);
  const progress = usePinProgress(scene);
  const narrow = useMedia(NARROW);
  const total = HOME.betaSteps.length;
  const lit = narrow ? total : Math.min(total, 1 + Math.floor(progress * (total + 0.6)));
  const ready = !narrow && lit >= total && progress > 0.75;
  return (
    <section className="h-scene h-beta-scene" id="beta" aria-labelledby="beta-title" ref={scene}>
      <div className="h-scene-view h-band">
        <div className="h-beta">
          <div className="h-beta-text">
            <h2 className="h-section-title" id="beta-title">{HOME.betaTitle}</h2>
            <p className="h-section-sub">{HOME.betaSub}</p>
            <ol className="h-beta-steps">
              {HOME.betaSteps.map((s, i) => (
                <li key={s.b} data-step={i + 1} className={i < lit ? "is-on" : ""}><div><b>{s.b}</b><span>{s.span}</span></div></li>
              ))}
            </ol>
          </div>
          <WaitlistForm ready={ready} />
        </div>
      </div>
    </section>
  );
}
