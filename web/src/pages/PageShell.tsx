import type { ReactNode } from "react";
import { AUTH } from "../messages";

// The frame shared by the sign-in, waitlist, legal and admin pages.
export function PageShell({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <main className="page">
      <a className="mark" href="/" style={{ textDecoration: "none", color: "inherit" }}>{AUTH.brand}</a>
      <div className={`page-card${wide ? " wide" : ""}`}>{children}</div>
      <footer><a href="/privacy">{AUTH.privacy}</a><a href="/terms">{AUTH.terms}</a></footer>
    </main>
  );
}
