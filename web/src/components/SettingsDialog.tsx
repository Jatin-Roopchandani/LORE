import { useCallback, useEffect, useRef, useState } from "react";
import { api, authApi, isError } from "../api";
import { SETTINGS } from "../messages";
import { isMulti } from "../routing";
import {
  closeSettings, lastTab, openSettings, parseHash, rememberTab, SETTINGS_EVENT, type SettingsTab,
} from "../settings";
import type { Depth, McpInfo, Me, World } from "../types";
import { LocalModelSettings } from "./LocalModelSettings";
import { ProviderSettings } from "./ProviderSettings";
import { AccountTab } from "./settings/AccountTab";
import { DataTab } from "./settings/DataTab";
import { McpTab } from "./settings/McpTab";
import { AppearanceTab, ReadingTab, UsageTab } from "./settings/PrefsTabs";

export const SETTINGS_BUTTON_ID = "settings-button";
const POSITION_BUTTON = "header .pos-btn";

/** The tabs that apply: account and admin are multi-user only (admin for admins). In local mode the providers tab
 * offers a local model (LocalModelSettings) instead of the user's own keys. */
export function tabsFor(multi: boolean, role?: string): SettingsTab[] {
  if (!multi) return ["usage", "providers", "appearance", "reading", "mcp", "data"];
  return ["account", "providers", "usage", "appearance", "reading", "mcp", "data", ...(role === "admin" ? ["admin" as const] : [])];
}

/** Follows the address: the tab `#settings/<tab>` asks for, "" for `#settings`, null when the popup is closed. */
function useSettingsRoute() {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const sync = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", sync);
    window.addEventListener(SETTINGS_EVENT, sync);
    return () => { window.removeEventListener("hashchange", sync); window.removeEventListener(SETTINGS_EVENT, sync); };
  }, []);
  return route;
}

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface SettingsDialogProps {
  worlds?: World[]; world?: string; onWorldChange?: (w: string) => void;
  depth?: Depth; onDepth?: (d: Depth) => void;
  showInternals?: boolean; onShowInternals?: (on: boolean) => void;
}

// The Settings popup: a modal dialog over the app (never a page or a route), opened by the address (#settings/<tab>).
// A tab list on the left (across the top on phones); Esc, the close button or a click outside close it; Tab stays
// inside while it is open, and focus goes back to whatever opened it.
export function SettingsDialog({ worlds = [], world = "all", onWorldChange = () => {}, depth = "auto", onDepth = () => {},
  showInternals = false, onShowInternals = () => {} }: SettingsDialogProps) {
  const route = useSettingsRoute();
  const open = route !== null;
  const multi = isMulti();
  const [me, setMe] = useState<Me | null>(null);
  const [mcp, setMcp] = useState<McpInfo | null>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const returnTo = useRef<HTMLElement | null>(null);

  useEffect(() => { if (open && multi && !me) authApi.me().then((r) => { if (!isError(r) && r.user) setMe(r); }); }, [open, multi, me]);
  useEffect(() => { if (open && !multi && !mcp) api.mcp().then((r) => { if (!isError(r)) setMcp(r); }); }, [open, multi, mcp]);

  const tabs = tabsFor(multi, me?.user.role);
  const remembered = lastTab();
  const tab: SettingsTab = route && tabs.includes(route) ? route
    : remembered && tabs.includes(remembered) ? remembered : tabs[0];
  useEffect(() => { if (open) rememberTab(tab); }, [open, tab]);

  const close = useCallback((focusTarget?: HTMLElement | null) => {
    returnTo.current = focusTarget ?? null;
    closeSettings();
  }, []);

  // Opening: remember who had focus, and focus the current tab. Closing: give focus back.
  useEffect(() => {
    if (!open) return;
    const active = document.activeElement as HTMLElement | null;
    opener.current = active && !dialog.current?.contains(active) && active !== document.body ? active : null;
    dialog.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus();
    return () => {
      const target = returnTo.current ?? (opener.current?.isConnected ? opener.current : document.getElementById(SETTINGS_BUTTON_ID));
      returnTo.current = null;
      target?.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); close(); } };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [open, close]);

  if (!open) return null;

  const trap = (e: React.KeyboardEvent) => {
    if (e.key !== "Tab" || !dialog.current) return;
    const items = Array.from(dialog.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && (document.activeElement === first || !dialog.current.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  const onTabKey = (e: React.KeyboardEvent) => {
    const i = tabs.indexOf(tab);
    const go = (n: number) => {
      e.preventDefault();
      const next = tabs[(n + tabs.length) % tabs.length];
      openSettings(next);
      setTimeout(() => dialog.current?.querySelector<HTMLElement>(`#settings-tab-${next}`)?.focus(), 0);
    };
    if (e.key === "ArrowDown" || e.key === "ArrowRight") go(i + 1);
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") go(i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(tabs.length - 1);
  };
  const openPosition = () => {
    const button = document.querySelector<HTMLElement>(POSITION_BUTTON);
    close(button);
    setTimeout(() => button?.click(), 0);
  };

  return (
    <div className="overlay settings-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div className="dialog settings" role="dialog" aria-modal="true" aria-labelledby="settings-title" ref={dialog} onKeyDown={trap}>
        <header className="settings-head">
          <h2 id="settings-title">{SETTINGS.title}</h2>
          <button className="tbtn icon settings-close" type="button" aria-label={SETTINGS.close} onClick={() => close()}>✕</button>
        </header>
        <div className="settings-body">
          <div className="settings-tabs" role="tablist" aria-label={SETTINGS.tabsLabel} aria-orientation="vertical" onKeyDown={onTabKey}>
            {tabs.map((t) => (
              <button key={t} id={`settings-tab-${t}`} type="button" role="tab" aria-selected={t === tab} aria-controls="settings-panel"
                tabIndex={t === tab ? 0 : -1} onClick={() => openSettings(t)}>
                {SETTINGS.tabs[t]}
              </button>
            ))}
          </div>
          <div className="settings-panel" id="settings-panel" role="tabpanel" aria-labelledby={`settings-tab-${tab}`} tabIndex={-1}>
            <h3>{SETTINGS.tabs[tab]}</h3>
            <p className="set-lead">{SETTINGS.leads[!multi && (tab === "data" || tab === "providers") ? `${tab}Local` : tab]}</p>
            {tab === "account" && (me ? <AccountTab me={me} /> : null)}
            {tab === "providers" && (multi ? <ProviderSettings heading={false} /> : <LocalModelSettings />)}
            {tab === "usage" && <UsageTab multi={multi} showInternals={showInternals} />}
            {tab === "appearance" && <AppearanceTab />}
            {tab === "reading" && (
              <ReadingTab multi={multi} worlds={worlds} world={world} onWorldChange={onWorldChange} depth={depth} onDepth={onDepth}
                showInternals={showInternals} onShowInternals={onShowInternals} onOpenPosition={openPosition} />
            )}
            {tab === "mcp" && <McpTab multi={multi} mcp={mcp} />}
            {tab === "data" && <DataTab multi={multi} email={me?.user.email} mcp={mcp} />}
            {tab === "admin" && (
              <><div className="key-actions"><a className="tbtn primary" href="/admin">{SETTINGS.adminLink}</a></div>
                <p className="hint">{SETTINGS.adminHint}</p></>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
