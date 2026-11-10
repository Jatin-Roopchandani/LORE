// "Did you mean …@gmail.com?": a gentle hint for a mistyped email domain, so an invite or sign-in link
// isn't sent to a domain that doesn't exist (an invite to "@gmai.com" was lost this way). It only suggests;
// the reader decides, and an unusual address that is really theirs is never blocked.

// Common mail domains. A typed domain close to one of these (but not equal) gets a suggestion.
const DOMAINS = [
  "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.co.uk", "hotmail.com", "outlook.com",
  "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
  "gmx.com", "gmx.de", "yandex.com", "mail.com", "zoho.com", "rediffmail.com", "fastmail.com", "hey.com",
];

// Optimal string alignment distance (Damerau-Levenshtein without repeated edits): one swap of two
// neighbouring letters counts as one edit, so "gmial" is one edit from "gmail".
export function editDistance(a: string, b: string): number {
  const d: number[][] = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
  for (let j = 1; j <= b.length; j++) d[0][j] = j;
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1);
    }
  }
  return d[a.length][b.length];
}

/** The corrected address when the domain looks like a slip of a common one, else null. */
export function suggestEmail(raw: string): string | null {
  const email = raw.trim();
  const at = email.lastIndexOf("@");
  if (at < 1 || at === email.length - 1) return null;
  const local = email.slice(0, at);
  const domain = email.slice(at + 1).toLowerCase();
  if (DOMAINS.includes(domain)) return null;
  let best: string | null = null;
  let bestDistance = Infinity;
  for (const candidate of DOMAINS) {
    const distance = editDistance(domain, candidate);
    // One slip for short domains, two for longer ones; never a different domain that merely looks alike.
    const allowed = candidate.length >= 9 ? 2 : 1;
    if (distance > 0 && distance <= allowed && distance < bestDistance) {
      best = candidate;
      bestDistance = distance;
    }
  }
  return best ? `${local}@${best}` : null;
}
