import type { MouseEvent } from "react";
import { HOME } from "../../messages";
import { useReducedMotion, useScrolled } from "./scroll";
import { goToForm } from "./session";
import { Logo } from "../Logo";
import { GithubMark } from "../authIcons";

export { Logo };

// LORE's public repository: an icon link in the nav (named by its label), a word in the footer.
export function GithubLink({ icon = false }: { icon?: boolean }) {
  return (
    <a className={icon ? "h-gh" : undefined} href={HOME.githubUrl} target="_blank" rel="noopener noreferrer"
      aria-label={icon ? HOME.githubLabel : undefined} title={icon ? HOME.githubLabel : undefined}>
      {icon ? <GithubMark size={20} /> : HOME.github}
    </a>
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
      <GithubLink icon />
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
      <a href="/privacy">{HOME.privacy}</a><a href="/terms">{HOME.terms}</a><GithubLink />
      {signedIn ? <a href="/app">{HOME.openLore}</a> : <a href="/signin">{HOME.signIn}</a>}
    </footer>
  );
}
