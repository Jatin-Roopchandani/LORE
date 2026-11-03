import { useEffect, useRef, useState } from "react";
import { THEME } from "../messages";
import { applyTheme, loadTheme } from "../theme";
import { THEMES, themeById, type Theme } from "../themes";

const SYSTEM_SWATCH: Theme = { id: "system", name: THEME.system, group: "light", tokens: {} };
// A miniature of the app in that theme: its background, a panel strip, a line of
// text in its ink, and accent and glow dots. System shows Light and Dark side by side.
function Preview({ t }: { t: Theme }) {
  const card = (tk: Record<string, string>) => (
    <span className="theme-card" style={{ background: tk["--bg"], borderColor: tk["--line"] }}>
      <span className="tc-panel" style={{ background: tk["--panel"] }} />
      <span className="tc-ink" style={{ background: tk["--ink"] }} />
      <span className="tc-dots">
        <span style={{ background: tk["--accent"] }} />
        <span style={{ background: tk["--glow"] }} />
      </span>
    </span>
  );
  if (t.id !== "system") return <span className="theme-swatch" aria-hidden="true">{card(t.tokens)}</span>;
  return (
    <span className="theme-swatch split" aria-hidden="true">
      {card(themeById("light")!.tokens)}
      {card(themeById("dark")!.tokens)}
    </span>
  );
}

// A button that opens a menu of themes: System, then the Dark group, then the Light group. Arrow keys move,
// Enter picks, Escape closes and returns focus to the button.
export function ThemeMenu() {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState(loadTheme());
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const items = () => Array.from(root.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]') ?? []);
  useEffect(() => {
    if (!open) return;
    const now = loadTheme();
    setCurrent(now);
    const list = items();
    (list.find((e) => e.dataset.id === now) ?? list[0])?.focus();
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  const pick = (id: string) => { applyTheme(id); setCurrent(id); setOpen(false); trigger.current?.focus(); };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setOpen(false); trigger.current?.focus(); return; }
    const list = items(), i = list.indexOf(document.activeElement as HTMLElement);
    const go = (n: number) => { e.preventDefault(); list[(n + list.length) % list.length]?.focus(); };
    if (e.key === "ArrowDown") go(i + 1); else if (e.key === "ArrowUp") go(i - 1);
    else if (e.key === "Home") go(0); else if (e.key === "End") go(list.length - 1);
    else if (e.key === "Tab") setOpen(false);
  };
  const entry = (t: Theme, hint?: string) => (
    <button key={t.id} type="button" role="menuitemradio" aria-checked={current === t.id} data-id={t.id} className="theme-item" onClick={() => pick(t.id)}>
      <Preview t={t} />
      <span>{t.name}</span>
      {hint && <small>{hint}</small>}
    </button>
  );
  const group = (g: "dark" | "light") => (
    <div role="group" aria-label={THEME.groups[g]} key={g}>
      <h4 aria-hidden="true">{THEME.groups[g]}</h4>
      {THEMES.filter((t) => t.group === g).map((t) => entry(t))}
    </div>
  );
  return (
    <div className="pop" ref={root} onKeyDown={open ? onKey : undefined}>
      <button ref={trigger} className="tbtn icon" type="button" aria-label={THEME.button} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>◐</button>
      {open && (
        <div className="menu theme-menu" role="menu" aria-label={THEME.menu}>
          {entry(SYSTEM_SWATCH, THEME.systemHint)}
          {group("dark")}
          {group("light")}
        </div>
      )}
    </div>
  );
}
