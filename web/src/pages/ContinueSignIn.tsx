import { useEffect } from "react";
import { SIGNIN_LINK } from "../messages";
import { PageShell } from "./PageShell";

// GET /auth/email/verify?token=… (the mailed link) draws this page and spends nothing, so a mail scanner that
// prefetches the link can't burn it. The button is a plain form POST: the server spends the token, sets the session
// and sends the browser on to /app. The token is taken out of the address bar at once (it stays in the form only).
export function ContinueSignIn({ search = window.location.search }: { search?: string }) {
  const token = new URLSearchParams(search).get("token") ?? "";
  useEffect(() => {
    try { if (window.location.search.includes("token=")) window.history.replaceState(null, "", window.location.pathname); } catch { /* not a browser */ }
  }, []);
  return (
    <PageShell>
      <h1>{SIGNIN_LINK.title}</h1>
      {token ? (
        <form method="post" action="/auth/email/verify">
          <p>{SIGNIN_LINK.lead}</p>
          <input type="hidden" name="token" value={token} />
          <button className="btn-new" type="submit">{SIGNIN_LINK.button}</button>
        </form>
      ) : (
        <>
          <div className="notice error" role="alert">{SIGNIN_LINK.missing}</div>
          <a href="/signin">{SIGNIN_LINK.again}</a>
        </>
      )}
    </PageShell>
  );
}
