import type { ReactNode } from "react";
import { AUTH } from "../messages";
import { Logo } from "./Logo";

// The plain frame of the admin, key-onboarding and not-found pages. (Sign-in, waitlist and continue-signing-in use
// the split AuthShell.)
export function PageShell({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <main className="page">
      <a className="mark page-mark" href="/" aria-label={AUTH.homeLabel}><Logo size={28} /><span>{AUTH.brand}</span></a>
      <div className={`page-card${wide ? " wide" : ""}`}>{children}</div>
      <footer><a href="/privacy">{AUTH.privacy}</a><a href="/terms">{AUTH.terms}</a></footer>
    </main>
  );
}
