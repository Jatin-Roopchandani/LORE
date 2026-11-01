// A recorded-shaped deep answer to an interpretive question with two parts and two lenses each, as the server
// streams it: the graph events per part, the plan (one section per lens, grouped by part), the short answer first,
// then the sections (one fails, one finds nothing) and the close. Invented names throughout.
import type { AskEvent, Claim, Edge, Passage, ProseSentence, Seed } from "../types";

const step = (t: number, name: any, state: any, part: number | null = null, detail: string | null = null): AskEvent =>
  ({ type: "step", t, name, state, part, detail });
const seed = (id: string, name: string): Seed => ({ id, name, kind: "Character", books: ["b1"], score: 0 });
const edge = (s: string, o: string, chunk: string): Edge => ({ subject: s.toUpperCase(), predicate: "KNOWS", predicate_raw: "knows",
  object: o.toUpperCase(), evidence: "", confidence: 1, book_id: "b1", chapter_seq: 1, chunk_id: chunk, subject_id: s, object_id: o });
const pass = (n: number, text: string): Passage => ({ n, chunk_id: `c${n}`, book_id: "b1", chapter_seq: n, text });

export const PART_A = "In the first book, who is boldest?";
export const PART_B = "In the second book, who is boldest?";
export const LENSES = ["What did Rin risk?", "What did Tam refuse?", "Who stood by Rin?", "Who stood by Tam?"];
export const claims: Claim[] = [
  { text: "Rin crossed the pass in winter.", passage: 1, quote: "crossed the pass in winter" },
  { text: "Tam refused the offer.", passage: 2, quote: "refused the offer" },
  { text: "Mara stood by Rin.", passage: 3, quote: "stood by Rin" },
  { text: "Joss stood by Tam.", passage: 4, quote: "stood by Tam" },
];
const passages = [pass(1, "Rin crossed the pass in winter."), pass(2, "Tam refused the offer."),
  pass(3, "Mara stood by Rin."), pass(4, "Joss stood by Tam.")];
const sent = (text: string, cl: number[], pa: number[], kind?: ProseSentence["kind"], para = false): ProseSentence =>
  ({ text, claims: cl, passages: pa, new_paragraph: para, ...(kind ? { kind } : {}) });
export const shortA = sent("In the first book, the evidence suggests Rin is boldest.", [1, 3], [1, 3], "reading", true);
export const shortB = sent("In the second book, on balance Tam is boldest.", [2, 4], [2, 4], "reading", true);
export const noPosition = sent(`${PART_B} The evidence found doesn't support a clear position.`, [], [], "limit", true);
export const counter = sent("Against that, Mara's loyalty may count for more.", [3], [3], "counter", true);
export const limit = sent("The books don't settle: who was bravest overall.", [], [], "limit", true);

export const head: AskEvent[] = [
  step(0, "split", "start"),
  { type: "parts", t: 0.2, sub_questions: LENSES, lens_of: [0, 0, 1, 1], plan_questions: [PART_A, PART_B] },
  step(0.2, "split", "done", null, "4 parts"),
  step(0.3, "seeds", "start", 0),
  { type: "seeds", t: 0.4, part: 0, entities: [seed("rin", "Rin"), seed("mara", "Mara")] },
  step(0.4, "seeds", "done", 0, "2 found"),
  { type: "edges", t: 0.5, part: 0, template: "neighbours", total: 1, edges: [edge("rin", "mara", "c1")] },
  { type: "seeds", t: 0.6, part: 1, entities: [seed("tam", "Tam"), seed("joss", "Joss")] },
  { type: "edges", t: 0.7, part: 1, template: "neighbours", total: 1, edges: [edge("tam", "joss", "c2")] },
  { type: "seeds", t: 0.8, part: 2, entities: [seed("mara", "Mara")] },
  { type: "seeds", t: 0.9, part: 3, entities: [seed("joss", "Joss")] },
  { type: "evidence", t: 1.0, chunk_ids: ["c1", "c2", "c3", "c4"], edge_keys: [] },
];
export const plan: AskEvent = { type: "plan", t: 2, scope: "", candidates: 0, sections: [
  { index: 0, heading: "What did Rin risk", group: PART_A, entities: [] },
  { index: 1, heading: "What did Tam refuse", group: PART_A, entities: [] },
  { index: 2, heading: "Who stood by Rin", group: PART_B, entities: [] },
  { index: 3, heading: "Who stood by Tam", group: PART_B, entities: [] }] };
export const shortEvent = (prose: ProseSentence[]): AskEvent =>
  ({ type: "short_answer", t: 3, prose, text: "", claims, passages });
const startOf = (index: number, heading: string): AskEvent => ({ type: "section", t: 4, index, state: "start", kind: "section", heading });
const doneOf = (index: number, heading: string, group: string, prose: ProseSentence[], range: [number, number], extra = {}): AskEvent =>
  ({ type: "section", t: 5, index, state: "done", kind: "section", heading, style: "prose", text: "", prose,
    claims: claims.slice(range[0] - 1, range[1]), claims_range: range, passages: [], note: null, group, ...extra } as AskEvent);
export const sec = {
  0: [step(4, "section", "start", 0), startOf(0, "What did Rin risk"),
    doneOf(0, "What did Rin risk", PART_A, [sent("Rin crossed the pass in winter.", [1], [1])], [1, 1]), step(5, "section", "done", 0, "1 claims")],
  1: [step(5, "section", "start", 1), startOf(1, "What did Tam refuse"),
    doneOf(1, "What did Tam refuse", PART_A, [], [2, 2], { style: "failed", claims: [], claims_range: null, text: "", note: "the section could not be written" }),
    step(6, "section", "done", 1, "failed")],
  2: [step(6, "section", "start", 2), startOf(2, "Who stood by Rin"),
    doneOf(2, "Who stood by Rin", PART_B, [], [3, 3], { style: "empty", claims: [], claims_range: null }), step(7, "section", "done", 2, "nothing found")],
  3: [step(7, "section", "start", 3), startOf(3, "Who stood by Tam"),
    doneOf(3, "Who stood by Tam", PART_B, [sent("Joss stood by Tam.", [4], [4])], [4, 4]), step(8, "section", "done", 3, "1 claims")],
};
export const close: AskEvent = { type: "section", t: 9, index: 4, state: "done", kind: "close", heading: "Counter-case and limits",
  style: "prose", text: "", prose: [counter, limit], claims: [], claims_range: null, passages: [], note: null } as AskEvent;
export const answer: AskEvent = { type: "answer", t: 10, result: { answer: "", mode: "graph", template: "neighbours", note: null,
  model: "m", selection_model: "m", sub_questions: LENSES, claims, passages, edges: [], seeds: [], answer_style: "sectioned", prose: [],
  requests: 14, unwritten_sections: 2, short_answer: [shortA, noPosition],
  sections: [
    { heading: "What did Rin risk", kind: "section", style: "prose", text: "", prose: [sent("Rin crossed the pass in winter.", [1], [1])], claims_range: [1, 1], entities: [], note: null, group: PART_A },
    { heading: "Who stood by Tam", kind: "section", style: "prose", text: "", prose: [sent("Joss stood by Tam.", [4], [4])], claims_range: [4, 4], entities: [], note: null, group: PART_B },
    { heading: "Counter-case and limits", kind: "close", style: "prose", text: "", prose: [counter, limit], claims_range: null, entities: [], note: null }] } };
export const full: AskEvent[] = [...head, plan, shortEvent([shortA, noPosition]), ...sec[0], ...sec[1], ...sec[2], ...sec[3], close, answer];
