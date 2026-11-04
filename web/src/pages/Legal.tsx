import { AUTH } from "../messages";
import { PageShell } from "./PageShell";

// Placeholders: Google's consent screen needs these addresses to exist.
export function Legal({ kind }: { kind: "privacy" | "terms" }) {
  return (
    <PageShell>
      <h1>{kind === "privacy" ? AUTH.privacyTitle : AUTH.termsTitle}</h1>
      <p>{kind === "privacy" ? AUTH.privacyBody : AUTH.termsBody}</p>
      <a href="/">{AUTH.back}</a>
    </PageShell>
  );
}
