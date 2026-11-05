import type { MouseEvent } from "react";
import { HOME } from "../../messages";
import { useReducedMotion, useScrolled } from "./scroll";
import { goToForm } from "./session";

// The LORE mark: an open book with a small constellation over it (the same drawing as favicon.svg).
export function Logo() {
  return (
    <svg width="34" height="34" viewBox="0 0 64 64" aria-hidden="true">
      <path d="M32 56 C24 51 14 50 6 52 V30 C14 28 24 29 32 34 C40 29 50 28 58 30 V52 C50 50 40 51 32 56 Z" fill="none" stroke="#78AEEA" strokeWidth="3.4" strokeLinejoin="round" />
      <path d="M32 34 V56" stroke="#78AEEA" strokeWidth="3.4" />
      <path d="M32 30 L16 14 M32 30 L32 8 M32 30 L48 14 M16 14 L32 8 L48 14" fill="none" stroke="#78AEEA" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="32" cy="30" r="3.2" fill="#78AEEA" /><circle cx="16" cy="14" r="3.4" fill="#78AEEA" />
      <circle cx="32" cy="8" r="3.8" fill="#EBAA3C" /><circle cx="48" cy="14" r="3.4" fill="#78AEEA" />
    </svg>
  );
}

// On the homepage the nav sits see-through over the night hero and turns solid once the page scrolls; on privacy
// and terms (`onHome` false) it is solid from the start and its links lead back to the homepage's sections.
export function Nav({ onHome, signedIn }: { onHome: boolean; signedIn: boolean }) {
  const scrolled = useScrolled();
  const reduced = useReducedMotion();
  const base = onHome ? "" : "/";
  const invite = (e: MouseEvent) => {
    if (!onHome) return; // a plain link to /#beta
    e.preventDefault();
    goToForm(reduced);
  };
  return (
    <nav className={`h-nav${scrolled || !onHome ? " is-solid" : ""}`} aria-label={HOME.navLabel}>
      <a className="h-brand" href={onHome ? "#top" : "/"} aria-label={HOME.brandLabel}>
        <Logo />
        <b>{HOME.brand}</b><small>{HOME.brandTag}</small>
      </a>
      <span className="h-sp" />
      <a className="h-link" href={`${base}#how`}>{HOME.navHow}</a>
      <a className="h-link" href={`${base}#beta`}>{HOME.navBeta}</a>
      {signedIn ? <a className="h-btn is-primary" href="/app">{HOME.openLore}</a> : (
        <>
          <a className="h-btn" href="/signin">{HOME.signIn}</a>
          <a className="h-btn is-primary h-invite" href={`${base}#beta`} onClick={invite}>{HOME.requestInvite}</a>
        </>
      )}
    </nav>
  );
}

export function Footer({ signedIn }: { signedIn: boolean }) {
  return (
    <footer className="h-band">
      <span>{HOME.copyright}</span><span className="h-sp" />
      <a href="/privacy">{HOME.privacy}</a><a href="/terms">{HOME.terms}</a>
      {signedIn ? <a href="/app">{HOME.openLore}</a> : <a href="/signin">{HOME.signIn}</a>}
    </footer>
  );
}
