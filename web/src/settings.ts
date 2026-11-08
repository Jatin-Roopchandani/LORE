// The Settings popup's address and the small preferences it owns.
//
// The popup is a dialog over the app, never a page: it opens when the address ends in `#settings` or
// `#settings/<tab>`, so any part of the UI can open a tab with a plain link (`<a href="#settings/providers">`), and it
// closes by dropping the hash. The last tab is remembered for the session.

import { SETTINGS } from "./messages";

export const SETTINGS_TABS = ["account", "providers", "usage", "appearance", "reading", "mcp", "data", "admin"] as const;
export type SettingsTab = (typeof SETTINGS_TABS)[number];

export const TAB_KEY = "lore-settings-tab";
export const MOTION_KEY = "lore-motion-v1";
export const INTERNALS_KEY = "lore-internals-v1";
export const SETTINGS_EVENT = "lore-settings";

const isTab = (v: unknown): v is SettingsTab => typeof v === "string" && (SETTINGS_TABS as readonly string[]).includes(v);

/** The tab a hash asks for: a tab id, "" for `#settings` alone (the last or first tab), or null when it isn't ours. */
export function parseHash(hash: string): SettingsTab | "" | null {
  const m = /^#settings(?:\/([\w-]*))?$/.exec(hash);
  if (!m) return null;
  return isTab(m[1]) ? m[1] : "";
}

export const hrefFor = (tab?: SettingsTab) => (tab ? `#settings/${tab}` : "#settings");

function session(): Storage | null { try { return window.sessionStorage; } catch { return null; } }
function local(): Storage | null { try { return window.localStorage; } catch { return null; } }

export function lastTab(): SettingsTab | null {
  try { const v = session()?.getItem(TAB_KEY); return isTab(v) ? v : null; } catch { return null; }
}
export function rememberTab(tab: SettingsTab): void {
  try { session()?.setItem(TAB_KEY, tab); } catch { /* not remembered */ }
}

/** Opens the popup (at `tab`, else the last tab). Replaces the hash rather than adding a history entry. */
export function openSettings(tab?: SettingsTab): void {
  const target = hrefFor(tab ?? lastTab() ?? undefined);
  if (window.location.hash === target) { window.dispatchEvent(new Event(SETTINGS_EVENT)); return; }
  history.replaceState(history.state, "", window.location.pathname + window.location.search + target);
  window.dispatchEvent(new Event(SETTINGS_EVENT));
}

export function closeSettings(): void {
  if (parseHash(window.location.hash) === null) return;
  history.replaceState(history.state, "", window.location.pathname + window.location.search);
  window.dispatchEvent(new Event(SETTINGS_EVENT));
}

// ---- Reduced motion: follow the device (the default), or always reduce. ----
export type Motion = "system" | "reduce";
export function loadMotion(): Motion {
  try { return local()?.getItem(MOTION_KEY) === "reduce" ? "reduce" : "system"; } catch { return "system"; }
}
export function applyMotion(m: Motion): void {
  if (m === "reduce") document.documentElement.setAttribute("data-motion", "reduce");
  else document.documentElement.removeAttribute("data-motion");
  try { local()?.setItem(MOTION_KEY, m); } catch { /* lasts this visit */ }
}
/** Whether animations should be skipped: the override, or the device's own setting. */
export const prefersReducedMotion = () =>
  document.documentElement.getAttribute("data-motion") === "reduce"
  || (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false);

// ---- Show internals (local mode only): this browser's override of lore.toml's [web] show_internals. ----
export function loadInternals(): boolean | null {
  try { const v = local()?.getItem(INTERNALS_KEY); return v === "on" ? true : v === "off" ? false : null; } catch { return null; }
}
export function saveInternals(on: boolean): void {
  try { local()?.setItem(INTERNALS_KEY, on ? "on" : "off"); } catch { /* lasts this visit */ }
}

/** "5 minutes ago", "yesterday", "3 days ago" (English, as every string here); a date after a month. */
export function ago(iso: string, now: Date = new Date()): string {
  const then = new Date(iso);
  const secs = Math.round((then.getTime() - now.getTime()) / 1000);
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const abs = Math.abs(secs);
  if (abs < 60) return SETTINGS.justNow;
  if (abs < 3600) return rtf.format(Math.round(secs / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(secs / 3600), "hour");
  if (abs < 30 * 86400) return rtf.format(Math.round(secs / 86400), "day");
  return SETTINGS.onDate(then.toLocaleDateString("en", { day: "numeric", month: "short", year: "numeric" }));
}
