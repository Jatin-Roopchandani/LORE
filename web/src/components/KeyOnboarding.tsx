import "./onboarding.css";
import { FormEvent, KeyboardEvent, ReactNode, RefObject, useEffect, useRef, useState } from "react";
import { isError, keyApi } from "../api";
import { AUTH, ONBOARD, PROVIDERS } from "../messages";
import { AlertIcon, CheckIcon, Spinner } from "../pages/authIcons";
import { Logo } from "../pages/Logo";
import type { KeySaved, ProviderId, ProvidersState } from "../types";
import { checkKey, ClaudePrices, GeminiPaidSwitch, ModelPicker, PROVIDER_IDS } from "./ProviderSettings";

// Shown inside /app, in place of the reading app, while the signed-in user has no working key: on first sign-in
// (`missing`), or after their provider refused the saved one (`invalid`). Two steps: choose one provider, then paste
// its key. The key check, model picker and Gemini paid-plan switch are the same pieces the Settings popup uses;
// Settings stays the place for managing several providers.
export function KeyOnboarding({ reason, onDone }: { reason: "missing" | "invalid"; onDone: () => void }) {
  const [step, setStep] = useState<1 | 2>(1);
  const [provider, setProvider] = useState<ProviderId>("gemini");
  const heading = useRef<HTMLHeadingElement>(null);
  const moved = useRef(false);
  // Focus follows the step change (not on first paint), so a screen reader hears the new heading.
  useEffect(() => { if (moved.current) heading.current?.focus(); }, [step]);
  // Replacing a refused key: start on the provider that refused it.
  useEffect(() => {
    if (reason !== "invalid") return;
    keyApi.providers().then((r) => {
      if (isError(r) || !Array.isArray(r.providers)) return;
      const p = r.providers.find((x) => x.last_error === "rejected") ?? r.providers.find((x) => x.active);
      if (p && PROVIDER_IDS.includes(p.provider)) setProvider(p.provider);
    });
  }, [reason]);
  const go = (next: 1 | 2) => { moved.current = true; setStep(next); };
  return (
    <div className="ob">
      <header className="ob-top">
        <a className="ob-brand" href="/" aria-label={AUTH.homeLabel}><span className="ob-logo"><Logo size={22} /></span><b>{AUTH.brand}</b></a>
        <ol className="ob-steps" aria-label={ONBOARD.progressLabel}>
          {ONBOARD.steps.map((label, i) => {
            const n = i + 1;
            const state = n < step ? " is-done" : n === step ? " is-now" : "";
            return (
              <li key={label} className={`ob-step${state}`} aria-current={n === step ? "step" : undefined}>
                <span className="ob-step-dot" aria-hidden="true">{n < step ? "✓" : n}</span><span className="ob-step-label">{label}</span>
              </li>
            );
          })}
        </ol>
      </header>
      <main className="ob-main">
        <div className={`ob-col${step === 1 ? " is-wide" : ""}`}>
          {step === 1
            ? <ChooseStep reason={reason} provider={provider} onPick={setProvider} onNext={() => go(2)} heading={heading} />
            : <KeyStep provider={provider} onBack={() => go(1)} onDone={onDone} heading={heading} />}
        </div>
      </main>
      <footer className="ob-foot">
        <p>{ONBOARD.footer}</p>
        <p className="ob-foot-links"><a href="/privacy">{AUTH.privacy}</a><a href="/terms">{AUTH.terms}</a></p>
      </footer>
    </div>
  );
}

const name = (p: ProviderId) => PROVIDERS.names[p] ?? p;

function Head({ n, title, sub, heading }: { n: number; title: string; sub: string; heading: RefObject<HTMLHeadingElement> }) {
  return (
    <div className="ob-head">
      <p className="ob-eyebrow">{ONBOARD.stepOf(n, 2)}</p>
      <h1 ref={heading} tabIndex={-1}>{title}</h1>
      <p className="ob-sub">{sub}</p>
    </div>
  );
}

function Alert({ id, children }: { id?: string; children: ReactNode }) {
  return <div className="ob-alert" role="alert" id={id}><AlertIcon /><div>{children}</div></div>;
}

// Step 1: three cards, one native radio each (so the arrow keys and screen readers treat them as one choice).
function ChooseStep({ reason, provider, onPick, onNext, heading }: { reason: "missing" | "invalid"; provider: ProviderId;
  onPick: (p: ProviderId) => void; onNext: () => void; heading: RefObject<HTMLHeadingElement> }) {
  const submit = (e: FormEvent) => { e.preventDefault(); onNext(); };
  // Enter on a card goes on, as it would in any one-field form (and the same in every browser).
  const enter = (e: KeyboardEvent) => { if (e.key === "Enter") { e.preventDefault(); onNext(); } };
  return (
    <form className="ob-form" onSubmit={submit}>
      <Head n={1} title={reason === "invalid" ? PROVIDERS.replaceTitle : PROVIDERS.connectTitle} sub={ONBOARD.chooseLead} heading={heading} />
      {reason === "invalid" && <Alert>{PROVIDERS.rejected}</Alert>}
      <div className="ob-cards" role="radiogroup" aria-label={ONBOARD.providersLabel}>
        {PROVIDER_IDS.map((id) => (
          <label key={id} className={`ob-card${id === provider ? " is-on" : ""}`}>
            <input className="ob-radio" type="radio" name="ob-provider" value={id} checked={id === provider}
              onChange={() => onPick(id)} onKeyDown={enter} aria-labelledby={`ob-${id}-name`} aria-describedby={`ob-${id}-pitch`} />
            <span className="ob-card-top">
              <span className="ob-glyph" aria-hidden="true">{ONBOARD.marks[id]}</span>
              {id === "gemini" && <span className="ob-badge">{ONBOARD.freeBadge}</span>}
              <span className="ob-tick" aria-hidden="true" />
            </span>
            <span className="ob-card-text">
              <span className="ob-card-name" id={`ob-${id}-name`}>{name(id)}</span>
              <span className="ob-card-pitch" id={`ob-${id}-pitch`}>{ONBOARD.pitch[id]}</span>
            </span>
          </label>
        ))}
      </div>
      <div className="ob-actions"><button className="ob-primary" type="submit">{ONBOARD.continue}</button></div>
    </form>
  );
}

function EyeIcon({ off }: { off: boolean }) {
  const line = { fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" } as const;
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" {...line} />
      <circle cx="12" cy="12" r="3" {...line} />
      {off && <path d="M4 4l16 16" {...line} />}
    </svg>
  );
}

function ExternalIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false">
      <path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" fill="none" stroke="currentColor"
        strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// What the chosen provider costs, folded away until asked for.
function Prices({ provider }: { provider: ProviderId }) {
  return (
    <details className="ob-prices">
      <summary>{ONBOARD.seePrices}</summary>
      <div className="ob-prices-body">
        {provider === "anthropic" && (<><ClaudePrices /><p className="hint">{PROVIDERS.novelEstimate}</p></>)}
        {provider === "gemini" && <p className="hint">{ONBOARD.geminiPrices}</p>}
        {provider === "openai" && <p className="hint">{ONBOARD.openaiPrices}</p>}
      </div>
    </details>
  );
}

// Step 2: the chosen provider's key. Once the server accepts it: the model picker (Claude, OpenAI) or the paid-plan
// switch (Gemini), then Start reading.
function KeyStep({ provider, onBack, onDone, heading }: { provider: ProviderId; onBack: () => void; onDone: () => void;
  heading: RefObject<HTMLHeadingElement> }) {
  const [key, setKey] = useState("");
  const [shown, setShown] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [saved, setSaved] = useState<KeySaved | null>(null);
  const [state, setState] = useState<ProvidersState | null>(null);
  const field = useRef<HTMLInputElement>(null);
  const loadState = async () => {
    const r = await keyApi.providers();
    if (!isError(r) && Array.isArray(r?.providers)) setState(r);
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setProblem("");
    const r = await checkKey(key, provider);
    setBusy(false);
    if ("problem" in r) {
      setProblem(r.problem);
      if (!r.keep) setKey("");
      field.current?.focus();
      return;
    }
    setKey("");
    setSaved(r.saved);
    await loadState();
  };
  const info = state?.providers.find((p) => p.provider === provider) ?? null;
  const changed = (next?: ProvidersState) => { if (next && Array.isArray(next.providers)) setState(next); else loadState(); };
  return (
    <div className="ob-form">
      <button className="ob-back" type="button" onClick={onBack}><span aria-hidden="true">←</span>{ONBOARD.changeProvider}</button>
      <div className="ob-panel">
      <Head n={2} title={ONBOARD.keyTitle(name(provider))} sub={saved ? ONBOARD.chooseHint : ONBOARD.keyLead[provider]} heading={heading} />
      {saved ? (<>
        <div className={`ob-done${saved.warning ? " is-warn" : ""}`} role="status">
          {saved.warning ? <AlertIcon /> : <CheckIcon size={20} />}
          <div><b>{saved.warning ? ONBOARD.savedUnchecked : ONBOARD.connected(name(provider))}</b>{saved.warning ? ` ${saved.warning}` : ""}</div>
        </div>
        {info && provider !== "gemini" && (
          <div className="ob-models">
            <ModelPicker info={info} compact onChosen={changed} />
          </div>
        )}
        {info && provider === "gemini" && (
          <div className="ob-models"><GeminiPaidSwitch paid={!!info.paid_tier} hint={!!state?.gemini_paid_hint} onChange={changed} /></div>
        )}
        <button className="ob-primary" type="button" onClick={onDone}>{PROVIDERS.startReading}</button>
      </>) : (
        <form className="ob-form ob-key" onSubmit={submit}>
          <div className="ob-field">
            <div className="ob-label-row">
              <label htmlFor="ob-key">{PROVIDERS.fieldLabel(name(provider))}</label>
              <a className="ob-getkey" href={PROVIDERS.getKeyUrl[provider]} target="_blank" rel="noopener noreferrer">
                {ONBOARD.getKeyAt[provider]}<ExternalIcon /><span className="ob-sr"> {ONBOARD.newTab}</span>
              </a>
            </div>
            <div className="ob-input-wrap">
              <input ref={field} id="ob-key" className="ob-input" type={shown ? "text" : "password"} name={`${provider}-key`}
                autoComplete="off" spellCheck={false} autoCapitalize="off" autoCorrect="off" required
                placeholder={PROVIDERS.placeholder} value={key} onChange={(e) => setKey(e.target.value)}
                aria-invalid={problem ? true : undefined} aria-describedby={problem ? "ob-key-problem" : undefined} />
              <button className="ob-reveal" type="button" onClick={() => setShown((s) => !s)}
                aria-label={shown ? ONBOARD.hideKey : ONBOARD.showKey} title={shown ? ONBOARD.hideKey : ONBOARD.showKey}>
                <EyeIcon off={shown} />
              </button>
            </div>
          </div>
          {problem && <Alert id="ob-key-problem">{problem}</Alert>}
          <button className="ob-primary" type="submit" disabled={busy || !key.trim()} aria-busy={busy || undefined}>
            {busy ? <><Spinner />{PROVIDERS.checking}</> : ONBOARD.check}
          </button>
        </form>
      )}
      <Prices provider={provider} />
      </div>
    </div>
  );
}
