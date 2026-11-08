import { useRef, useState } from "react";
import { SETTINGS, THEME } from "../messages";
import { applyTheme, loadTheme } from "../theme";
import { THEMES, themeById, type Theme } from "../themes";

// The theme picker's pieces. It used to be a dropdown in the top bar; it now lives in Settings, Appearance, as a grid.

export const SYSTEM_SWATCH: Theme = { id: "system", name: THEME.system, group: "light", tokens: {} };
const BASICS = ["system", "light", "dark"];

// A miniature of the app in that theme: its background, a panel strip, a line of text in its ink, and accent and glow
// dots. System shows Light and Dark side by side.
export function ThemePreview({ t, large = false }: { t: Theme; large?: boolean }) {
  const card = (tk: Record<string, string>) => (
    <span className="theme-card" style={{ background: tk["--bg"], borderColor: tk["--line"] }}>
      <span className="tc-panel" style={{ background: tk["--panel"] }} />
      <span className="tc-ink" style={{ background: tk["--ink"] }} />
      {large && <span className="tc-ink short" style={{ background: tk["--muted"] }} />}
      <span className="tc-dots">
        <span style={{ background: tk["--accent"] }} />
        <span style={{ background: tk["--glow"] }} />
      </span>
    </span>
  );
  const cls = `theme-swatch${large ? " large" : ""}`;
  if (t.id !== "system") return <span className={cls} aria-hidden="true">{card(t.tokens)}</span>;
  return (
    <span className={`${cls} split`} aria-hidden="true">
      {card(themeById("light")!.tokens)}
      {card(themeById("dark")!.tokens)}
    </span>
  );
}

const byId = (id: string) => (id === "system" ? SYSTEM_SWATCH : themeById(id)!);

// Every theme as a card in a grid: System, Light and Dark first, then the other dark and light themes. One radio group:
// arrow keys move between cards (and choose, as radios do), Home and End jump to the ends.
export function ThemeGrid({ onChange }: { onChange?: (id: string) => void }) {
  const [current, setCurrent] = useState(loadTheme());
  const root = useRef<HTMLDivElement>(null);
  const pick = (id: string) => { applyTheme(id); setCurrent(id); onChange?.(id); };
  const sections: { key: string; label: string; themes: Theme[] }[] = [
    { key: "basics", label: SETTINGS.themeBasics, themes: BASICS.map(byId) },
    ...(["dark", "light"] as const).map((g) => ({
      key: g, label: SETTINGS.themeGroups[g], themes: THEMES.filter((t) => t.group === g && !BASICS.includes(t.id)),
    })),
  ];
  const order = sections.flatMap((s) => s.themes.map((t) => t.id));
  const onKey = (e: React.KeyboardEvent) => {
    const i = order.indexOf(current);
    const go = (n: number) => {
      e.preventDefault();
      const id = order[(n + order.length) % order.length];
      pick(id);
      root.current?.querySelector<HTMLElement>(`[data-id="${id}"]`)?.focus();
    };
    if (e.key === "ArrowDown" || e.key === "ArrowRight") go(i + 1);
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") go(i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(order.length - 1);
  };
  return (
    <div className="theme-grid" role="radiogroup" aria-label={SETTINGS.theme} ref={root} onKeyDown={onKey}>
      {sections.map((s) => (
        <div key={s.key} role="group" aria-label={s.label} className="theme-section">
          <h5 aria-hidden="true">{s.label}</h5>
          <div className="theme-cards">
            {s.themes.map((t) => (
              <button key={t.id} type="button" role="radio" aria-checked={current === t.id} data-id={t.id}
                tabIndex={current === t.id || (!order.includes(current) && t.id === "system") ? 0 : -1}
                className="theme-option" onClick={() => pick(t.id)}>
                <ThemePreview t={t} large />
                <span className="theme-name">{t.name}</span>
                {t.id === "system" && <small>{THEME.systemHint}</small>}
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
