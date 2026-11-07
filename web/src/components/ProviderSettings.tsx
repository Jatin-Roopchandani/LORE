import { FormEvent, useEffect, useState } from "react";
import { isError, keyApi, reportKeyProblem } from "../api";
import { PROVIDERS } from "../messages";
import type { KeySaved, ProviderId, ProviderInfo, ProvidersState } from "../types";

// Self-contained provider pieces (multi-user mode): no page or route of their own, so they render inside any container
// (the onboarding screen, the Settings popup). Keys are pasted, checked by the server, and never shown back.

export const PROVIDER_IDS: ProviderId[] = ["gemini", "anthropic", "openai"];
const name = (p: ProviderId) => PROVIDERS.names[p] ?? p;

// The paste field and "Check and save" for one provider. The key lives only in this field until the server answers.
export function ProviderKeyForm({ provider, onSaved, onCancel }:
  { provider: ProviderId; onSaved: (saved: KeySaved) => void; onCancel?: () => void }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setProblem("");
    const r = await keyApi.save(key.trim(), provider);
    setBusy(false);
    if (isError(r)) {
      setProblem(PROVIDERS.errors[r.error.code] ?? PROVIDERS.saveFailed);
      if (r.error.code !== "rate_limited") setKey("");
      return;
    }
    setKey("");
    onSaved(r);
  };
  return (
    <form className="key-form" onSubmit={submit}>
      <label>{PROVIDERS.fieldLabel(name(provider))}
        <input type="password" name={`${provider}-key`} autoComplete="off" spellCheck={false} required
          placeholder={PROVIDERS.placeholder} value={key} onChange={(e) => setKey(e.target.value)} />
      </label>
      {problem && <div className="notice error" role="alert">{problem}</div>}
      <div className="key-actions">
        <button className="btn-new" type="submit" disabled={busy || !key.trim()}>{busy ? PROVIDERS.checking : PROVIDERS.save}</button>
        {onCancel && <button className="tbtn" type="button" onClick={onCancel}>{PROVIDERS.cancel}</button>}
      </div>
    </form>
  );
}

// Claude's three models and their prices (the cost-honesty table).
export function ClaudePrices() {
  return (
    <>
      <table className="price-table">
        <caption className="hint">{PROVIDERS.priceHeading}</caption>
        <tbody>
          {PROVIDERS.claudeModels.map((m) => <tr key={m.id}><th scope="row">{m.name}</th><td>{m.price}</td></tr>)}
        </tbody>
      </table>
      <p className="hint">{PROVIDERS.priceExplain}</p>
    </>
  );
}

// The model picker for a connected Claude or OpenAI key (Gemini runs its own measured cascade: nothing to pick).
export function ModelPicker({ info, onChosen }: { info: ProviderInfo; onChosen: (state: ProvidersState) => void }) {
  const [problem, setProblem] = useState("");
  if (info.provider === "gemini") return null;
  const options = info.provider === "anthropic"
    ? PROVIDERS.claudeModels.map((m) => ({ id: m.id, label: `${m.name} (${m.price})` }))
    : info.models.map((m) => ({ id: m, label: m }));
  const choose = async (model: string | null, fast?: string | null) => {
    setProblem("");
    const r = await keyApi.choose(info.provider, model, fast);
    if (isError(r)) { setProblem(PROVIDERS.errors[r.error.code] ?? PROVIDERS.saveFailed); return; }
    onChosen(r);
  };
  return (
    <div className="model-picker">
      <label>{PROVIDERS.modelLabel}
        <select value={info.model ?? ""} onChange={(e) => choose(e.target.value)}>
          {!info.model && <option value="" disabled>{"—"}</option>}
          {options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
      </label>
      {info.provider === "openai" && <p className="hint">{PROVIDERS.openaiSuggested}</p>}
      <label>{PROVIDERS.fastModelLabel}
        <select value={info.fast_model ?? ""} onChange={(e) => choose(null, e.target.value || null)}>
          <option value="">{PROVIDERS.fastModelSame}</option>
          {options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
        </select>
      </label>
      <p className="hint">{PROVIDERS.fastModelHint}</p>
      {info.provider === "anthropic" && <p className="hint">{PROVIDERS.priceExplain}</p>}
      {problem && <div className="notice error" role="alert">{problem}</div>}
    </div>
  );
}

// One provider to connect: what it costs, where its key comes from, the paste field; once saved, its model picker.
// `onConnected` runs when the user is done (straight away for Gemini without a warning; after Continue otherwise).
export function ProviderCard({ provider, onConnected }: { provider: ProviderId; onConnected: () => void }) {
  const [saved, setSaved] = useState<KeySaved | null>(null);
  const [info, setInfo] = useState<ProviderInfo | null>(null);
  const afterSave = async (r: KeySaved) => {
    if (provider === "gemini" && !r.warning) { onConnected(); return; }
    setSaved(r);
    const state = await keyApi.providers();
    if (!isError(state)) setInfo(state.providers.find((p) => p.provider === provider) ?? null);
  };
  return (
    <section className="provider-card" aria-labelledby={`provider-${provider}`}>
      <h2 id={`provider-${provider}`}>{name(provider)}</h2>
      <p className="hint">{PROVIDERS.cost[provider]}</p>
      {provider === "anthropic" && (<><ClaudePrices /><p className="hint">{PROVIDERS.novelEstimate}</p></>)}
      <p><a href={PROVIDERS.getKeyUrl[provider]} target="_blank" rel="noopener noreferrer">{PROVIDERS.getKey[provider]}</a></p>
      {saved ? (<>
        <div className="notice" role="status">{PROVIDERS.saved}{saved.warning ? ` ${saved.warning}` : ""}</div>
        {info && <ModelPicker info={info} onChosen={(s) => setInfo(s.providers.find((p) => p.provider === provider) ?? null)} />}
        <button className="btn-new" type="button" onClick={onConnected}>{PROVIDERS.startReading}</button>
      </>) : <ProviderKeyForm provider={provider} onSaved={afterSave} />}
    </section>
  );
}

// The hint's dismissal, per browser and day (it comes back on another day if the condition holds again).
const HINT_KEY = "lore-gemini-paid-hint-dismissed-v1";
const today = () => new Date().toISOString().slice(0, 10);
function hintDismissed(): boolean {
  try { return localStorage.getItem(HINT_KEY) === today(); } catch { return false; }
}
function dismissHint(): void {
  try { localStorage.setItem(HINT_KEY, today()); } catch { /* storage blocked: dismissed for this view only */ }
}

// Gemini only: the user's own "I'm on a paid Gemini plan" switch, and the hint that their key seems to be on one.
// LORE never turns the switch on by itself.
export function GeminiPaidSwitch({ paid, hint, onChange }:
  { paid: boolean; hint: boolean; onChange: (state: ProvidersState) => void }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [dismissed, setDismissed] = useState(hintDismissed);
  const toggle = async () => {
    setBusy(true); setProblem("");
    const r = await keyApi.setGeminiPaid(!paid);
    setBusy(false);
    if (isError(r)) { setProblem(PROVIDERS.errors[r.error.code] ?? PROVIDERS.saveFailed); return; }
    onChange(r);
  };
  return (
    <div className="gemini-paid">
      {hint && !paid && !dismissed && (
        <div className="notice" role="status">
          {PROVIDERS.geminiPaidHint}{" "}
          <button className="tbtn" type="button" onClick={() => { dismissHint(); setDismissed(true); }}>{PROVIDERS.dismiss}</button>
        </div>
      )}
      <label className="switch-row">
        <input type="checkbox" role="switch" checked={paid} aria-checked={paid} disabled={busy} onChange={toggle} />
        {PROVIDERS.geminiPaid}
      </label>
      <p className="hint">{PROVIDERS.geminiPaidHelp}</p>
      {problem && <div className="notice error" role="alert">{problem}</div>}
    </div>
  );
}

// One connected provider in Settings: in use or "Use it", its key's last four, its model, Replace and Remove.
// Gemini also has the paid-plan switch (and its hint, from `state`).
function ConnectedProvider({ info, state, onChange }:
  { info: ProviderInfo; state: ProvidersState; onChange: (state?: ProvidersState) => void }) {
  const [mode, setMode] = useState<"view" | "replace" | "remove">("view");
  const use = async () => {
    const r = await keyApi.choose(info.provider);
    if (!isError(r)) onChange(r);
  };
  const remove = async () => {
    const r = await keyApi.remove(info.provider);
    if (isError(r)) return;
    setMode("view");
    const state = await keyApi.providers();
    if (isError(state)) { onChange(); return; }
    onChange(state);
    if (!state.providers.some((p) => p.connected)) reportKeyProblem("key_required");
  };
  return (
    <div className="provider-row">
      <h5>{name(info.provider)} {info.active
        ? <span className="badge">{PROVIDERS.active}</span>
        : <button className="tbtn" type="button" onClick={use}>{PROVIDERS.use(name(info.provider))}</button>}</h5>
      {info.last4 && <p className="hint">{PROVIDERS.endingIn(info.last4)}</p>}
      {info.last_error === "rejected" && <div className="notice error" role="alert">{PROVIDERS.needsReplacing}</div>}
      {info.last_error === "unchecked" && <p className="hint">{PROVIDERS.unchecked}</p>}
      <p className="hint">{info.provider === "gemini" && info.paid_tier ? PROVIDERS.geminiPaidCost : PROVIDERS.cost[info.provider]}</p>
      {info.provider === "gemini" && <GeminiPaidSwitch paid={!!info.paid_tier} hint={!!state.gemini_paid_hint} onChange={onChange} />}
      <ModelPicker info={info} onChosen={onChange} />
      {mode === "view" && (
        <div className="key-actions">
          <button className="tbtn" type="button" onClick={() => setMode("replace")}>{PROVIDERS.replace}</button>
          <button className="tbtn" type="button" onClick={() => setMode("remove")}>{PROVIDERS.remove}</button>
        </div>
      )}
      {mode === "replace" && <ProviderKeyForm provider={info.provider} onSaved={() => { setMode("view"); onChange(); }}
        onCancel={() => setMode("view")} />}
      {mode === "remove" && (<>
        <p className="hint">{PROVIDERS.removeConfirm(name(info.provider))}</p>
        <div className="key-actions">
          <button className="tbtn" type="button" onClick={remove}>{PROVIDERS.removeYes}</button>
          <button className="tbtn" type="button" onClick={() => setMode("view")}>{PROVIDERS.cancel}</button>
        </div>
      </>)}
    </div>
  );
}

// Settings: every connected provider (switch the active one or its model, replace, remove), and adding another.
// `heading`: false inside the Settings popup, whose tab already names the section.
export function ProviderSettings({ heading = true }: { heading?: boolean } = {}) {
  const [state, setState] = useState<ProvidersState | null>(null);
  const [adding, setAdding] = useState<ProviderId | null>(null);
  const load = () => { keyApi.providers().then((r) => { if (!isError(r) && Array.isArray(r.providers)) setState(r); }); };
  useEffect(load, []);
  if (!state) return null;
  const connected = state.providers.filter((p) => p.connected);
  const others = PROVIDER_IDS.filter((id) => !connected.some((p) => p.provider === id));
  const changed = (next?: ProvidersState) => { if (next) setState(next); else load(); };
  return (
    <div className="provider-settings">
      {heading && <h4>{PROVIDERS.section}</h4>}
      {connected.map((info) => <ConnectedProvider key={info.provider} info={info} state={state} onChange={changed} />)}
      {others.length > 0 && (<>
        <h5>{PROVIDERS.addAnother}</h5>
        {adding ? <ProviderCard provider={adding} onConnected={() => { setAdding(null); load(); }} />
          : <div className="key-actions">{others.map((id) =>
            <button key={id} className="tbtn" type="button" onClick={() => setAdding(id)}>{name(id)}</button>)}</div>}
      </>)}
    </div>
  );
}
