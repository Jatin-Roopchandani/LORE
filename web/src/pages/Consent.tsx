import { useEffect, useState } from "react";
import { api, isError, nav } from "../api";
import { CONSENT } from "../messages";
import type { ConsentInfo } from "../types";
import { Alert, AuthShell, PrimaryButton } from "./AuthShell";
import { KeyIcon } from "./authIcons";

// GET /oauth/consent?req=… (multi mode): an MCP client such as claude.ai asks to read the reader's library. The server
// only draws this page for a signed-in reader (signed out, it sends them to sign in and back here). Allow or Cancel
// posts the answer and goes where the server says: the client's registered address, with a code or with
// access_denied. A script navigation, not a form post: the CSP's form-action allows only this origin.
export function Consent({ search = window.location.search }: { search?: string }) {
  const req = new URLSearchParams(search).get("req") ?? "";
  const [info, setInfo] = useState<ConsentInfo | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "busy" | "expired" | "failed">(req ? "loading" : "expired");
  useEffect(() => {
    if (!req) return;
    api.consent(req).then((r) => {
      if (isError(r)) { setState("expired"); return; }
      setInfo(r); setState("ready");
    });
  }, [req]);
  const decide = async (approve: boolean) => {
    setState("busy");
    const r = await api.decideConsent(req, approve);
    if (isError(r)) { setState(r.error.code === "invalid_request" ? "expired" : "failed"); return; }
    nav.to(r.redirect);
  };
  const title = info?.client_name ? CONSENT.title(info.client_name) : CONSENT.titleUnnamed;
  return (
    <AuthShell mark={false}>
      <div className="au-badge"><KeyIcon /></div>
      <div className="au-head"><h1>{state === "expired" || state === "loading" ? CONSENT.titleUnnamed : title}</h1></div>
      {state === "loading" && <p className="au-sub" role="status">{CONSENT.loading}</p>}
      {state === "expired" && <Alert>{CONSENT.expired}</Alert>}
      {info && state !== "expired" && (
        <form className="au-form" onSubmit={(e) => { e.preventDefault(); decide(true); }}>
          <p className="au-sub">{CONSENT.lead}</p>
          <ul className="consent-can">{CONSENT.can.map((line) => <li key={line}>{line}</li>)}</ul>
          <p className="au-fine">{CONSENT.cannot}</p>
          {info.redirect_host && <p className="au-fine">{CONSENT.back(info.redirect_host)}</p>}
          {state === "failed" && <Alert>{CONSENT.failed}</Alert>}
          <PrimaryButton big busy={state === "busy"} busyLabel={CONSENT.allowing}>{CONSENT.allow}</PrimaryButton>
          <button className="au-secondary" type="button" disabled={state === "busy"} onClick={() => decide(false)}>{CONSENT.cancel}</button>
          <p className="au-fine">{CONSENT.later}</p>
        </form>
      )}
    </AuthShell>
  );
}
