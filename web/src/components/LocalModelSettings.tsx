import { FormEvent, useEffect, useState } from "react";
import { isError, localModelApi } from "../api";
import { LOCAL_MODEL, PROVIDERS } from "../messages";
import type { LocalModelState, LocalModelTest } from "../types";

// Settings → AI providers in local mode. What runs the model steps now, the cloud providers (set in lore.toml, shown
// for orientation), and the Local model option: an OpenAI-compatible server's address, the model and an optional key,
// with Test connection (one tiny structured call) and Save, which makes it the active provider for every step.
// Multi-user mode never shows this (the server has no such route there).
export function LocalModelSettings() {
  const [state, setState] = useState<LocalModelState | null>(null);
  const [address, setAddress] = useState("");
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [testing, setTesting] = useState(false);
  const [tested, setTested] = useState<LocalModelTest | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [problem, setProblem] = useState("");

  const show = (next: LocalModelState) => {
    setState(next);
    const fromFile = next.file.provider === "openai_compatible" ? next.file : null;
    setAddress(next.saved?.base_url || fromFile?.base_url || next.default_base_url);
    setModel(next.saved?.model || fromFile?.model || "");
  };
  useEffect(() => { localModelApi.get().then((r) => { if (!isError(r) && r.active) show(r); }); }, []);
  if (!state) return null;

  const body = () => ({ base_url: address.trim(), model: model.trim(), ...(key.trim() ? { key: key.trim() } : {}) });
  const test = async () => {
    setTesting(true); setTested(null); setProblem(""); setNotice("");
    const r = await localModelApi.test(body());
    setTesting(false);
    if (isError(r)) { setTested({ ok: false, message: r.error.message || LOCAL_MODEL.errors[r.error.code] || LOCAL_MODEL.testFailed }); return; }
    setTested(r);
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setProblem(""); setNotice("");
    const r = await localModelApi.save(body());
    setBusy(false);
    if (isError(r)) { setProblem(r.error.message || LOCAL_MODEL.errors[r.error.code] || LOCAL_MODEL.saveFailed); return; }
    setKey("");
    show(r);
    setNotice(LOCAL_MODEL.saved);
  };
  const clearKey = async () => {
    const r = await localModelApi.save({ base_url: address.trim(), model: model.trim(), clear_key: true });
    if (!isError(r)) show(r);
  };
  const remove = async () => {
    setProblem(""); setTested(null);
    const r = await localModelApi.remove();
    if (isError(r)) { setProblem(LOCAL_MODEL.saveFailed); return; }
    show(r);
    setNotice(LOCAL_MODEL.removed);
  };
  const active = state.active;
  const ready = address.trim() !== "" && model.trim() !== "";
  return (
    <div className="provider-settings local-model">
      <p className="hint" data-testid="local-active">
        <b>{LOCAL_MODEL.nowUsing}: </b>
        {LOCAL_MODEL.activeLine(PROVIDERS.names[active.provider] ?? active.provider, active.model,
          active.provider === "openai_compatible" ? active.base_url : null)}
        {" "}({LOCAL_MODEL.source[active.source]})
      </p>

      <h4>{LOCAL_MODEL.title}</h4>
      <p className="hint">{LOCAL_MODEL.lead}</p>
      <form className="key-form" onSubmit={save}>
        <label>{LOCAL_MODEL.addressLabel}
          <input type="url" name="local-address" autoComplete="off" spellCheck={false} required value={address}
            placeholder={state.default_base_url} onChange={(e) => { setAddress(e.target.value); setTested(null); }} />
        </label>
        <label>{LOCAL_MODEL.modelLabel}
          <input type="text" name="local-model" autoComplete="off" spellCheck={false} required value={model}
            onChange={(e) => { setModel(e.target.value); setTested(null); }} />
        </label>
        <p className="hint">{LOCAL_MODEL.modelHint}</p>
        <label>{LOCAL_MODEL.keyLabel}
          <input type="password" name="local-key" autoComplete="off" spellCheck={false} value={key}
            placeholder={LOCAL_MODEL.keyPlaceholder} onChange={(e) => setKey(e.target.value)} />
        </label>
        {state.saved?.has_key && (
          <p className="hint">{LOCAL_MODEL.keyKept}{" "}
            <button className="link" type="button" onClick={clearKey}>{LOCAL_MODEL.clearKey}</button></p>
        )}
        {tested && (tested.ok
          ? <div className="notice" role="status">{LOCAL_MODEL.testOk(tested.model, tested.seconds)}</div>
          : <div className="notice error" role="alert"><b>{LOCAL_MODEL.testFailed}</b> {tested.message}</div>)}
        {problem && <div className="notice error" role="alert">{problem}</div>}
        {notice && <div className="notice" role="status">{notice}</div>}
        <p className="hint">{LOCAL_MODEL.overrides} {LOCAL_MODEL.sizeNote}</p>
        <div className="key-actions">
          <button className="tbtn" type="button" onClick={test} disabled={testing || !ready}>
            {testing ? LOCAL_MODEL.testing : LOCAL_MODEL.test}</button>
          <button className="btn-new" type="submit" disabled={busy || !ready}>{busy ? LOCAL_MODEL.saving : LOCAL_MODEL.save}</button>
          {state.saved && <button className="tbtn" type="button" onClick={remove}>{LOCAL_MODEL.remove}</button>}
        </div>
      </form>

      <h4>{LOCAL_MODEL.cloudTitle}</h4>
      <p className="hint">{LOCAL_MODEL.cloudHint}</p>
    </div>
  );
}
