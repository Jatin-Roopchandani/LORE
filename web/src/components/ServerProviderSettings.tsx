import { FormEvent, useEffect, useState } from "react";
import { isError, selfhostApi } from "../api";
import { SELFHOST } from "../messages";
import type { LocalModelTest, ServerProvider, ServerProviderId } from "../types";

const CHOICES: ServerProviderId[] = ["openai_compatible", "gemini", "anthropic", "openai"];
export const DOCKER_HOST_URL = "http://host.docker.internal:8081/v1";

// The admin page in self-host mode: the AI provider for everyone who has no key of their own. A cloud provider
// with its key (never shown back), or a local model on an OpenAI-compatible server (address, model, optional key,
// Test connection). Only the admin can reach this (the server checks); nobody else can set an address.
export function ServerProviderSettings() {
  const [saved, setSaved] = useState<ServerProvider | null | undefined>(undefined);
  const [provider, setProvider] = useState<ServerProviderId>("openai_compatible");
  const [address, setAddress] = useState(DOCKER_HOST_URL);
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [testing, setTesting] = useState(false);
  const [tested, setTested] = useState<LocalModelTest | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [problem, setProblem] = useState("");

  const show = (p: ServerProvider | null) => {
    setSaved(p);
    if (!p) return;
    setProvider(p.provider);
    setModel(p.model ?? "");
    if (p.base_url) setAddress(p.base_url);
  };
  useEffect(() => { selfhostApi.provider().then((r) => show(isError(r) ? null : r.provider)); }, []);

  const local = provider === "openai_compatible";
  const sameAsSaved = saved?.provider === provider;
  const ready = local ? address.trim() !== "" && model.trim() !== "" : key.trim() !== "" || (sameAsSaved && !!saved?.has_key);

  const test = async () => {
    setTesting(true); setTested(null); setProblem(""); setNotice("");
    const r = await selfhostApi.testProvider({ base_url: address.trim(), model: model.trim(), ...(key.trim() ? { key: key.trim() } : {}) });
    setTesting(false);
    setTested(isError(r) ? { ok: false, message: r.error.message || SELFHOST.testFailed } : r);
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setProblem(""); setNotice("");
    const r = await selfhostApi.saveProvider({
      provider, ...(key.trim() ? { key: key.trim() } : {}), ...(model.trim() ? { model: model.trim() } : {}),
      ...(local ? { base_url: address.trim() } : {}),
    });
    setBusy(false);
    if (isError(r)) { setProblem(r.error.message || SELFHOST.failed); return; }
    setKey("");
    show(r.provider);
    setNotice(SELFHOST.saved);
  };
  const remove = async () => {
    setProblem(""); setNotice("");
    const r = await selfhostApi.removeProvider();
    if (isError(r)) { setProblem(SELFHOST.failed); return; }
    setSaved(null); setTested(null);
    setNotice(SELFHOST.removed);
  };

  if (saved === undefined) return null;
  return (
    <section className="server-provider" aria-labelledby="server-provider-title">
      <h2 id="server-provider-title">{SELFHOST.providerTitle}</h2>
      <p>{SELFHOST.providerLead}</p>
      <p className="hint" data-testid="server-provider-now">
        {saved ? SELFHOST.providerNow(SELFHOST.choices[saved.provider] ?? saved.provider, saved.model) : SELFHOST.providerNone}
      </p>
      <form className="key-form" onSubmit={save}>
        <label>{SELFHOST.providerLabel}
          <select value={provider} onChange={(e) => { setProvider(e.target.value as ServerProviderId); setTested(null); setKey(""); }}>
            {CHOICES.map((c) => <option key={c} value={c}>{SELFHOST.choices[c]}</option>)}
          </select>
        </label>
        {local && (
          <label>{SELFHOST.addressLabel}
            <input type="url" name="server-address" autoComplete="off" spellCheck={false} value={address}
              onChange={(e) => { setAddress(e.target.value); setTested(null); }} />
          </label>
        )}
        {local && <p className="hint">{SELFHOST.addressHint}</p>}
        <label>{SELFHOST.modelLabel}
          <input type="text" name="server-model" autoComplete="off" spellCheck={false} value={model}
            onChange={(e) => { setModel(e.target.value); setTested(null); }} />
        </label>
        <p className="hint">{local ? SELFHOST.localModelHint : SELFHOST.modelHintCloud}</p>
        <label>{local ? SELFHOST.localKeyLabel : SELFHOST.keyLabel}
          <input type="password" name="server-key" autoComplete="off" spellCheck={false} value={key}
            onChange={(e) => setKey(e.target.value)} />
        </label>
        {sameAsSaved && saved?.has_key && saved.last4 && <p className="hint">{SELFHOST.keyKept(saved.last4)}</p>}
        {tested && (tested.ok
          ? <div className="notice" role="status">{SELFHOST.testOk(tested.model, tested.seconds)}</div>
          : <div className="notice error" role="alert"><b>{SELFHOST.testFailed}</b> {tested.message}</div>)}
        {problem && <div className="notice error" role="alert">{problem}</div>}
        {notice && <div className="notice" role="status">{notice}</div>}
        <div className="key-actions">
          {local && (
            <button className="tbtn" type="button" onClick={test} disabled={testing || !ready}>
              {testing ? SELFHOST.testing : SELFHOST.test}</button>
          )}
          <button className="tbtn primary" type="submit" disabled={busy || !ready}>{busy ? SELFHOST.saving : SELFHOST.save}</button>
          {saved && <button className="tbtn" type="button" onClick={remove}>{SELFHOST.remove}</button>}
        </div>
      </form>
    </section>
  );
}
