import { EXAMPLES } from "./messages";
import type { SubgraphData } from "./types";

const GROUP_KINDS = new Set(["Faction", "Group"]);

/** Up to four example questions built from the library overview's most-connected entities (overview order = degree order). */
export function exampleQuestions(overview: SubgraphData | null | undefined): string[] {
  const nodes = overview?.nodes ?? [];
  const names = (match: (kind: string) => boolean) => nodes.filter((n) => match(n.kind) && n.name).map((n) => n.name);
  const characters = names((k) => k === "Character");
  const place = names((k) => k === "Place")[0];
  const group = names((k) => GROUP_KINDS.has(k))[0];
  const out: string[] = [];
  if (characters.length >= 2) out.push(EXAMPLES.connected(characters[0], characters[1]));
  if (characters.length >= 1) out.push(EXAMPLES.about(characters[0]));
  if (place) out.push(EXAMPLES.place(place));
  if (group) out.push(EXAMPLES.group(group));
  return out;
}
