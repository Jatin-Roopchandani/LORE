import { AUTH } from "../messages";
import { PageShell } from "./PageShell";

// A placeholder front door; the real homepage is built in a later step.
export function Home() {
  return (
    <PageShell>
      <h1>{AUTH.homeTitle}</h1>
      <p className="lead">{AUTH.homeLead}</p>
      <p>{AUTH.homeBeta}</p>
      <div className="row">
        <a className="btn-new" href="/signin" style={{ textDecoration: "none" }}>{AUTH.signIn}</a>
        <a className="btn-provider" href="/waitlist">{AUTH.requestInvite}</a>
      </div>
    </PageShell>
  );
}
