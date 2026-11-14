// Which page to draw for an address. In local mode there is one page, the app, at "/" as ever. In multi-user mode the
// server marks the page with <meta name="lore-multi" content="1"> and the address decides.
export type Page = "app" | "home" | "signin" | "verify" | "waitlist" | "admin" | "privacy" | "terms" | "consent" | "notfound"
  | "blog" | "post" | "faq" | "setup" | "setpassword";

export const isMulti = (doc: Document = document) =>
  doc.querySelector('meta[name="lore-multi"]')?.getAttribute("content") === "1";

// Self-host mode (LORE_SELFHOST) adds <meta name="lore-selfhost" content="1">: password sign-in and first-run setup.
// (The pre-render runs without a document: never self-hosted there.)
export const isSelfhost = (doc: Document | undefined = typeof document === "undefined" ? undefined : document) =>
  doc?.querySelector('meta[name="lore-selfhost"]')?.getAttribute("content") === "1";

const PAGES: Record<string, Page> = {
  "/setup": "setup", "/set-password": "setpassword",
  "/": "home", "/app": "app", "/signin": "signin", "/waitlist": "waitlist", "/admin": "admin", "/privacy": "privacy", "/terms": "terms",
  "/blog": "blog", "/faq": "faq",
  "/auth/email/verify": "verify",  // the mailed sign-in link (release step 6)
  "/oauth/consent": "consent",     // an MCP client asking to read the library (hosted MCP)
};

export function pageFor(pathname: string, multi: boolean): Page {
  if (!multi) return "app";
  const clean = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  if (/^\/blog\/[a-z0-9-]+$/.test(clean)) return "post";
  return PAGES[clean] ?? "notfound";
}
