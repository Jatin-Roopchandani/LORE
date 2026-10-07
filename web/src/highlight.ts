export interface Span { text: string; mark: boolean }

// One-for-one character folds, so indexes in the folded text are indexes in the original.
const fold = (s: string) => s.toLowerCase().replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/[–—]/g, "-");
const LETTER = /\p{L}/u;
const WORD = /[\p{L}\p{N}_]/u;

// Mirrors the backend's _normalise: drop every " and every ' that is not between two letters
// (apostrophes inside words stay), collapse whitespace, trim. `map[i]` is the index in the
// folded text of normalised character i, so matches can be mapped back to the original.
function normalise(raw: string): { text: string; map: number[] } {
  const f = fold(raw);
  const out: string[] = [];
  const map: number[] = [];
  for (let i = 0; i < f.length; i++) {
    const c = f[i];
    if (c === '"') continue;
    if (c === "'" && !(i > 0 && LETTER.test(f[i - 1]) && i + 1 < f.length && LETTER.test(f[i + 1]))) continue;
    if (/\s/.test(c)) {
      if (out.length && out[out.length - 1] !== " ") { out.push(" "); map.push(i); }
      continue;
    }
    out.push(c); map.push(i);
  }
  if (out[out.length - 1] === " ") { out.pop(); map.pop(); }
  return { text: out.join(""), map };
}

// Whole words at both ends, like the backend's _find.
function findWhole(hay: string, needle: string, from: number): [number, number] | null {
  let at = hay.indexOf(needle, from);
  while (at !== -1) {
    const end = at + needle.length;
    if (!(at > 0 && WORD.test(hay[at - 1])) && !(end < hay.length && WORD.test(hay[end]))) return [at, end];
    at = hay.indexOf(needle, at + 1);
  }
  return null;
}

export function highlightQuote(text: string, quote: string): Span[] {
  const norm = normalise(text);
  const segments = quote.split(/\.\.\.|…/).map((s) => normalise(s).text)
    .filter((s) => s.split(" ").filter((w) => WORD.test(w)).length >= 3);
  const ranges: [number, number][] = [];
  let from = 0;
  for (const seg of segments) {
    const r = findWhole(norm.text, seg, from);
    if (r) { ranges.push([norm.map[r[0]], norm.map[r[1] - 1] + 1]); from = r[1]; }
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
