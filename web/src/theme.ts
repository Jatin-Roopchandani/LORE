import { themeById } from "./themes";

// A theme id from themes.ts, or "system" (follow the OS). The old values "light" | "dark" | "system" are all valid ids.
export type ThemeMode = string;
const KEY = "lore-theme-v2";
const OLD_KEY = "lore-theme";

const known = (v: string | null): v is string => v === "system" || (!!v && !!themeById(v));

export function applyTheme(mode: ThemeMode): void {
  const id = known(mode) ? mode : "system";
  if (id === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", id);
  try { localStorage.setItem(KEY, id); } catch { /* storage blocked: the choice lasts this visit */ }
}

export function loadTheme(): ThemeMode {
  try {
    const v = localStorage.getItem(KEY);
    if (v !== null) return known(v) ? v : "system";
    const old = localStorage.getItem(OLD_KEY);          // pre-v2: light | dark | system
    if (known(old)) { localStorage.setItem(KEY, old); return old; }
  } catch { /* blocked */ }
  return "system";
}
