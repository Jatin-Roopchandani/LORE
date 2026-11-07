import { useEffect, useState } from "react";
import { SIGNIN_LINK } from "../messages";
import { AgreeLine, Alert, AuthShell, PrimaryButton } from "./AuthShell";
import { KeyIcon } from "./authIcons";

// GET /auth/email/verify?token=… (the mailed link) draws this page and spends nothing, so a mail scanner that
// prefetches the link can't burn it. The button is a plain form POST: the server spends the token, sets the session
// and sends the browser on to /app. The token is taken out of the address bar at once (it stays in the form only).
export function ContinueSignIn({ search = window.location.search }: { search?: string }) {
  const token = new URLSearchParams(search).get("token") ?? "";
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    try { if (window.location.search.includes("token=")) window.history.replaceState(null, "", window.location.pathname); } catch { /* not a browser */ }
  }, []);
  // Coming back to this page from the browser's history: the button works again.
  useEffect(() => {
    const again = () => setBusy(false);
    window.addEventListener("pageshow", again);
    return () => window.removeEventListener("pageshow", again);
  }, []);
  return (
    <AuthShell mark={false}>
      <div className="au-badge"><KeyIcon /></div>
      <div className="au-head">
        <h1>{SIGNIN_LINK.title}</h1>
        {token && <p className="au-sub">{SIGNIN_LINK.lead}</p>}
      </div>
      {token ? (
        <>
          <form className="au-form" method="post" action="/auth/email/verify" onSubmit={() => setBusy(true)}>
            <input type="hidden" name="token" value={token} />
            <PrimaryButton big busy={busy} busyLabel={SIGNIN_LINK.button}>{SIGNIN_LINK.button}</PrimaryButton>
          </form>
          <p className="au-fine">{SIGNIN_LINK.why}</p>
          <AgreeLine />
        </>
      ) : (
        <>
          <Alert>{SIGNIN_LINK.missing}</Alert>
          <a className="au-secondary" href="/signin">{SIGNIN_LINK.again}</a>
        </>
      )}
    </AuthShell>
  );
}
