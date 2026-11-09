import { useEffect, useState } from "react";
import { api, isError } from "../../api";
import { PROVIDERS, SETTINGS } from "../../messages";
import { applyMotion, loadMotion, type Motion } from "../../settings";
import { loadOpen, saveOpen, STEPS_KEY } from "../../stepsPref";
import type { Depth, Quota, World } from "../../types";
import { ALL_WORLDS } from "../../worlds";
import { DEPTHS } from "../../depth";
import { ThemeGrid } from "../ThemeMenu";
import { UsagePanel } from "../UsagePanel";

// A labelled group of radio buttons, drawn as a segmented control.
function Segmented<T extends string>({ name, legend, hint, value, options, onChange }:
  { name: string; legend: string; hint?: string; value: T; options: { id: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <fieldset className="set-field">
      <legend>{legend}</legend>
      <div className="segmented">
        {options.map((o) => (
          <label key={o.id} className={value === o.id ? "on" : undefined}>
            <input type="radio" name={name} value={o.id} checked={value === o.id} onChange={() => onChange(o.id)} />
            {o.label}
          </label>
        ))}
      </div>
      {hint && <p className="hint">{hint}</p>}
    </fieldset>
  );
}

function Toggle({ label, hint, checked, onChange }: { label: string; hint?: string; checked: boolean; onChange: (on: boolean) => void }) {
  return (
    <div className="set-field">
      <label className="toggle">
        <input type="checkbox" role="switch" checked={checked} onChange={(e) => onChange(e.target.checked)} />
        <span className="toggle-track" aria-hidden="true"><span /></span>
        <span>{label}</span>
      </label>
      {hint && <p className="hint">{hint}</p>}
    </div>
  );
}

export function AppearanceTab() {
  const [motion, setMotion] = useState<Motion>(loadMotion());
  return (<>
    <section className="set-section"><ThemeGrid /></section>
    <section className="set-section">
      <Segmented name="motion" legend={SETTINGS.motion} hint={SETTINGS.motionHint} value={motion}
        options={(["system", "reduce"] as Motion[]).map((id) => ({ id, label: SETTINGS.motionOptions[id] }))}
        onChange={(m) => { applyMotion(m); setMotion(m); }} />
    </section>
  </>);
}

export interface ReadingProps {
  multi: boolean;
  worlds: World[]; world: string; onWorldChange: (w: string) => void;
  depth: Depth; onDepth: (d: Depth) => void;
  showInternals: boolean; onShowInternals: (on: boolean) => void;
  onOpenPosition: () => void;
}

export function ReadingTab({ multi, worlds, world, onWorldChange, depth, onDepth, showInternals, onShowInternals, onOpenPosition }: ReadingProps) {
  const [steps, setSteps] = useState(() => loadOpen(STEPS_KEY, false));
  return (<>
    <section className="set-section">
      <div className="set-field">
        <label className="set-label" htmlFor="set-world">{SETTINGS.world}</label>
        <select id="set-world" value={world} onChange={(e) => onWorldChange(e.target.value)}>
          <option value={ALL_WORLDS}>{SETTINGS.allWorlds}</option>
          {worlds.map((w) => <option key={w.id} value={String(w.id)}>{w.name}</option>)}
        </select>
        <p className="hint">{SETTINGS.worldHint}</p>
      </div>
    </section>
    <section className="set-section">
      <div className="set-field">
        <span className="set-label">{SETTINGS.position}</span>
        <p className="hint">{SETTINGS.positionHint}</p>
        <div className="key-actions">
          <button className="tbtn" type="button" onClick={onOpenPosition}>{SETTINGS.positionOpen}</button>
        </div>
      </div>
    </section>
    <section className="set-section">
      <Segmented name="depth" legend={SETTINGS.depth} hint={SETTINGS.depthHint} value={depth}
        options={DEPTHS.map((id) => ({ id, label: SETTINGS.depthOptions[id] }))} onChange={onDepth} />
      <Toggle label={SETTINGS.steps} hint={SETTINGS.stepsHint} checked={steps}
        onChange={(on) => { setSteps(on); saveOpen(STEPS_KEY, on); }} />
      {!multi && <Toggle label={SETTINGS.internals} hint={SETTINGS.internalsHint} checked={showInternals} onChange={onShowInternals} />}
    </section>
  </>);
}

// Usage. Multi mode: the provider usage panel (requests, tokens, cost, the token cap; Gemini's requests when Gemini is
// in use). Local mode: the Gemini daily budget, per model, and when it resets.
export function UsageTab({ multi, showInternals }: { multi: boolean; showInternals: boolean }) {
  if (multi) return <UsagePanel heading={false} />;
  return <LocalBudget showInternals={showInternals} />;
}

function LocalBudget({ showInternals }: { showInternals: boolean }) {
  const [quota, setQuota] = useState<Quota | null | undefined>(undefined);
  useEffect(() => { api.quota().then((q) => setQuota(isError(q) ? null : q)); }, []);
  if (quota === undefined) return null;
  if (quota === null) return <p className="hint">{SETTINGS.usageUnavailable}</p>;
  if (quota.provider === "billed") return <p className="hint">{SETTINGS.billed(PROVIDERS.names[quota.billed_by ?? "gemini"] ?? "your provider")}</p>;
  if (quota.provider !== "gemini") return <p className="hint">{SETTINGS.localModels}</p>;
  const time = new Date(quota.resets_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (!quota.models.length) {
    return (
      <section className="set-section">
        <p className="set-big">{showInternals ? SETTINGS.geminiNoneYet : SETTINGS.noneYet}</p>
        <p className="hint">{SETTINGS.resets(time)}</p>
      </section>
    );
  }
  const left = quota.models.reduce((a, m) => a + m.remaining, 0);
  const limit = quota.models.reduce((a, m) => a + m.limit, 0);
  const used = limit ? (limit - left) / limit : 0;
  return (
    <section className="set-section">
      <p className="set-big">{showInternals ? SETTINGS.geminiToday(left, limit) : SETTINGS.requestsToday(left, limit)}</p>
      <div className="lib-bar" role="progressbar" aria-label={SETTINGS.tabs.usage} aria-valuemin={0} aria-valuemax={100}
        aria-valuenow={Math.round(used * 100)}><span style={{ width: `${Math.round(used * 100)}%` }} /></div>
      {showInternals && (
        <ul className="set-list">
          {quota.models.map((m) => <li key={m.model}><span className="hint">{SETTINGS.perModel(m.model, m.count, m.limit)}</span></li>)}
        </ul>
      )}
      <p className="hint">{SETTINGS.resets(time)}</p>
    </section>
  );
}
