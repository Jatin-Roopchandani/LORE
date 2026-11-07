import "./auth.css";
import type { ReactNode } from "react";
import { AUTH } from "../messages";
import { AlertIcon, Spinner } from "./authIcons";
import { AuthGraph } from "./AuthGraph";
import { Logo } from "./Logo";

// The frame of the sign-in, waitlist and continue-signing-in pages: the homepage's night sky on the left (logo, the
// promise and a mirror-symmetric chapters 1-2 constellation, all on one centre line), the form on the right. On a phone the
// sky shrinks to a strip with the logo and the first line.
// `mark` false drops the small logo above the heading, for a page that leads with its own icon instead.
export function AuthShell({ children, mark = true }: { children: ReactNode; mark?: boolean }) {
  return (
    <div className="au">
      <div className="au-brand">
        <a className="au-wordmark" href="/" aria-label={AUTH.homeLabel}>
          <Logo size={32} />
          <b>{AUTH.brand}</b><small>{AUTH.brandTag}</small>
        </a>
        <p className="au-line">{AUTH.brandLine} <span>{AUTH.brandLineGlow}</span></p>
        {/* A picture of the product, not content: hidden from assistive technology so the form comes first. */}
        <div className="au-art" aria-hidden="true"><AuthGraph /></div>
        <p className="au-caption">{AUTH.brandCaption}</p>
      </div>
      <main className="au-main">
        <div className="au-col">
          {mark && <a className="au-mark" href="/" aria-label={AUTH.homeLabel}><Logo size={28} /></a>}
          {children}
        </div>
      </main>
    </div>
  );
}

export function Alert({ children }: { children: ReactNode }) {
  return <div className="au-alert" role="alert"><AlertIcon /><div>{children}</div></div>;
}

// The page's main action: full width, with a spinner and a busy label while it works.
export function PrimaryButton({ busy = false, busyLabel, children, disabled = false, big = false }:
  { busy?: boolean; busyLabel?: string; children: ReactNode; disabled?: boolean; big?: boolean }) {
  return (
    <button className={`au-primary${big ? " is-big" : ""}`} type="submit" disabled={busy || disabled} aria-busy={busy || undefined}>
      {busy ? <><Spinner />{busyLabel}</> : children}
    </button>
  );
}

export function AgreeLine() {
  return (
    <p className="au-fine">
      {AUTH.agreeBefore}<a href="/terms">{AUTH.termsLink}</a>{AUTH.agreeAnd}<a href="/privacy">{AUTH.privacyLink}</a>{AUTH.agreeAfter}
    </p>
  );
}
