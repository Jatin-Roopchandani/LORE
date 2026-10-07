export interface Span { text: string; mark: boolean }

// One-for-one character folds, so indexes in the folded text are indexes in the original.
const fold = (s: string) => s.toLowerCase().replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/[–—]/g, "-");
// Normalise a quote the way the backend's verify does: no quote marks, single spaces.
const normQuote = (s: string) => fold(s).replace(/["]/g, "").replace(/\s+/g, " ").trim();

function findLoose(text: string, needle: string, from: number): [number, number] | null {
  // Match needle in text ignoring quote marks and collapsing whitespace, returning original indexes.
  const f = fold(text);
  const words = needle.split(" ").filter(Boolean);
  if (!words.length) return null;
  const pattern = words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(`["\\s]+`);
  const re = new RegExp(`["]?${pattern}`, "g");
  re.lastIndex = from;
  const m = re.exec(f);
  return m ? [m.index + (m[0].startsWith('"') ? 1 : 0), m.index + m[0].length] : null;
}

export function highlightQuote(text: string, quote: string): Span[] {
  const segments = quote.split(/\.\.\.|…/).map(normQuote).filter((s) => s.split(" ").length >= 3);
  const ranges: [number, number][] = [];
  let from = 0;
  for (const seg of segments) {
    const r = findLoose(text, seg, from);
    if (r) { ranges.push(r); from = r[1]; }
  }
  if (!ranges.length) return [{ text, mark: false }];
  const spans: Span[] = [];
  let at = 0;
  for (const [a, b] of ranges) {
    if (a > at) spans.push({ text: text.slice(at, a), mark: false });
    spans.push({ text: text.slice(a, b), mark: true });
    at = b;
  }
  if (at < text.length) spans.push({ text: text.slice(at), mark: false });
  return spans;
}
