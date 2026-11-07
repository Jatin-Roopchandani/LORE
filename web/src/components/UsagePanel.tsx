import { FormEvent, useEffect, useState } from "react";
import { isError, keyApi } from "../api";
import { USAGE } from "../messages";
import type { TokenTotals, UsageToday } from "../types";

// Self-contained (no page or route). Gemini on the free tier: today's request count and when it resets. Claude, OpenAI
// and a paid Gemini plan: tokens today and this month, an estimated cost where the model has a known price, and the
// user's own optional daily token cap.
function Totals({ label, totals }: { label: string; totals: TokenTotals }) {
  const tokens = totals.input_tokens + totals.output_tokens;
  return (
    <p className="hint">{USAGE.tokens(label, tokens)}
      {totals.estimated_cost_usd != null && <>{" · "}{USAGE.cost(totals.estimated_cost_usd)}</>}</p>
  );
}

// `heading`: false inside the Settings popup, whose tab already names the section.
export function UsagePanel({ heading = true }: { heading?: boolean } = {}) {
  const [usage, setUsage] = useState<UsageToday | null>(null);
  const [cap, setCap] = useState("");
  const [problem, setProblem] = useState("");
  const load = () => {
    keyApi.usage().then((u) => {
      if (isError(u) || typeof u.requests !== "number") return;
      setUsage(u);
      setCap(u.tokens?.daily_token_cap ? String(u.tokens.daily_token_cap) : "");
    });
  };
  useEffect(load, []);
  if (!usage) return null;
  const tokens = usage.tokens;
  const freeGemini = (usage.provider ?? "gemini") === "gemini" && !usage.gemini_paid;
  const save = async (value: number | null) => {
    setProblem("");
    const r = await keyApi.setTokenCap(value);
    if (isError(r)) { setProblem(USAGE.capInvalid); return; }
    load();
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const n = Number(cap.replace(/[,\s_]/g, ""));
    if (!cap.trim() || !Number.isInteger(n) || n < 0) { setProblem(USAGE.capInvalid); return; }
    save(n || null);
  };
  return (
    <div className="usage-panel">
      {heading && <h4>{USAGE.section}</h4>}
      {freeGemini && (<>
        <p className="hint">{USAGE.requestsToday(usage.requests, usage.limit)}</p>
        <p className="hint">{USAGE.resets}</p>
      </>)}
      {tokens && !freeGemini && (<>
        <Totals label={USAGE.today} totals={tokens.today} />
        <Totals label={USAGE.month} totals={tokens.month_total} />
        {tokens.cap_reached && <div className="notice error" role="alert">{USAGE.capReached}</div>}
        <form className="key-form" onSubmit={submit}>
          <label>{USAGE.capLabel}
            <input type="text" inputMode="numeric" placeholder={USAGE.capOff} value={cap} onChange={(e) => setCap(e.target.value)} />
          </label>
          <p className="hint">{USAGE.capHint}</p>
          {problem && <div className="notice error" role="alert">{problem}</div>}
          <div className="key-actions">
            <button className="tbtn" type="submit">{USAGE.capSave}</button>
            {tokens.daily_token_cap != null && <button className="tbtn" type="button" onClick={() => save(null)}>{USAGE.capClear}</button>}
          </div>
        </form>
      </>)}
    </div>
  );
}
