/** Quote a string for safe pasting into a POSIX shell: single quotes, embedded ' as '\''. */
export function shellQuote(s: string): string {
  return `'${s.replace(/'/g, `'\\''`)}'`;
}
