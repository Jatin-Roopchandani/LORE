import { WORLDS } from "../messages";
import type { World } from "../types";
import { ALL_WORLDS } from "../worlds";

// A native select: keyboard and screen-reader behaviour for free. The value is a world id as a string, or "all".
export function WorldSelector({ worlds, world, onChange }: { worlds: World[]; world: string; onChange: (world: string) => void }) {
  return (
    <label className="wsel">
      <span className="visually-hidden">{WORLDS.selectLabel}</span>
      <select aria-label={WORLDS.selectLabel} value={world} onChange={(e) => onChange(e.target.value)}>
        <option value={ALL_WORLDS}>{WORLDS.all}</option>
        {worlds.map((w) => <option key={w.id} value={String(w.id)}>{w.name}</option>)}
      </select>
    </label>
  );
}
