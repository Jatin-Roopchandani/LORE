import { PROVIDERS } from "../messages";
import { PageShell } from "../pages/PageShell";
import { PROVIDER_IDS, ProviderCard } from "./ProviderSettings";

// Shown inside /app, in place of the reading app, while the signed-in user has no working key: on first sign-in
// (`missing`), or after their provider refused the saved one (`invalid`). Three cards, one per provider, built from the
// same components the Settings popup uses; connecting any one of them is enough.
export function KeyOnboarding({ reason, onDone }: { reason: "missing" | "invalid"; onDone: () => void }) {
  return (
    <PageShell>
      <h1>{reason === "invalid" ? PROVIDERS.replaceTitle : PROVIDERS.connectTitle}</h1>
      {reason === "invalid" && <div className="notice error" role="alert">{PROVIDERS.rejected}</div>}
      <p className="lead">{PROVIDERS.lead}</p>
      <div className="provider-cards">
        {PROVIDER_IDS.map((id) => <ProviderCard key={id} provider={id} onConnected={onDone} />)}
      </div>
      <p className="hint">{PROVIDERS.privacy}</p>
    </PageShell>
  );
}
