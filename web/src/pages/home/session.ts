// The server marks the public pages for a signed-in visitor (lore/web/public_pages.py), so the page offers
// "Open LORE" from its first frame without asking /api/me (which a signed-out visitor would answer with a 401).
// Signed out when pre-rendered (web/scripts/prerender.tsx: no document).
export const isSignedIn = (doc: Document | undefined = typeof document === "undefined" ? undefined : document) =>
  doc?.querySelector('meta[name="lore-signed-in"]')?.getAttribute("content") === "1";

// Scroll to the waitlist form and put the cursor in its email field.
export function goToForm(reduced: boolean) {
  const beta = document.getElementById("beta");
  const email = document.getElementById("h-email") as HTMLInputElement | null;
  beta?.scrollIntoView?.({ behavior: reduced ? "instant" : "smooth", block: "start" });
  email?.focus({ preventScroll: true });
}
