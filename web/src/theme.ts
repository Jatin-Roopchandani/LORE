export type ThemeMode = "system" | "light" | "dark";
const KEY = "lore-theme";

export function applyTheme(mode: ThemeMode): void {
  if (mode === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
  try { localStorage.setItem(KEY, mode); } catch { /* storage blocked: the choice lasts this visit */ }
}

export function loadTheme(): ThemeMode {
  try { const v = localStorage.getItem(KEY); if (v === "light" || v === "dark" || v === "system") return v; } catch { /* blocked */ }
  return "system";
}

export function isDark(): boolean {
  const t = document.documentElement.getAttribute("data-theme");
  return t ? t === "dark" : window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}
