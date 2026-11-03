/** `text` with its first letter in upper case and the rest as it was (names and acronyms keep their case). */
export function cap(text: string): string {
  const at = text.search(/\p{L}/u);
  return at < 0 ? text : text.slice(0, at) + text[at].toUpperCase() + text.slice(at + 1);
}
