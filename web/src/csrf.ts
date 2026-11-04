// The per-session CSRF token the server sets in a readable cookie. Every state-changing request echoes it in a
// header (a page on another site can send the cookie but cannot read it). Local mode has no such cookie, so nothing
// is sent there.
export const CSRF_COOKIE = "lore_csrf";
export function csrfHeaders(): Record<string, string> {
  try {
    const hit = document.cookie.split(";").map((c) => c.trim()).find((c) => c.startsWith(`${CSRF_COOKIE}=`));
    const token = hit ? decodeURIComponent(hit.slice(CSRF_COOKIE.length + 1)) : "";
    return token ? { "X-CSRF-Token": token } : {};
  } catch {
    return {};
  }
}
