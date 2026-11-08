import { useEffect, useRef, useState } from "react";
import { AUTH, SETTINGS } from "../messages";
import { openSettings } from "../settings";
import type { Me } from "../types";
import { signOut } from "./settings/AccountTab";

// The account menu (multi-user mode): who is signed in, and two quick links, Settings and Sign out. Everything else
// about the account lives in the Settings popup.
// `me`: the signed-in user, as the app already asked for it (null while unknown).
export function AccountMenu({ me }: { me: Me["user"] | null }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    root.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener("mousedown", close); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  const onKey = (e: React.KeyboardEvent) => {
    const items = Array.from(root.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    const i = items.indexOf(document.activeElement as HTMLElement);
    if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
    else if (e.key === "Tab") setOpen(false);
  };
  const initial = (me?.name || me?.email || "?").trim().charAt(0).toUpperCase();
  return (
    <div className="pop" ref={root} onKeyDown={open ? onKey : undefined}>
      <button ref={trigger} className="tbtn icon avatar" type="button" aria-label={SETTINGS.accountMenu} aria-haspopup="menu"
        aria-expanded={open} onClick={() => setOpen(!open)}>{initial}</button>
      {open && (
        <div className="menu account-menu" role="menu" aria-label={SETTINGS.accountMenu}>
          {me && (
            <div className="account-who" role="presentation">
              {me.name && <strong>{me.name}</strong>}
              <span className="hint">{me.email}</span>
            </div>
          )}
          <button type="button" role="menuitem" className="menu-item" onClick={() => { setOpen(false); openSettings("account"); }}>{SETTINGS.open}</button>
          <button type="button" role="menuitem" className="menu-item" onClick={signOut}>{AUTH.signOut}</button>
        </div>
      )}
    </div>
  );
}
