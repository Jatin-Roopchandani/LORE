// A recorded-shaped deep answer for two invented sections and a close, as the server streams it.
import type { AskEvent, Claim, Passage, ProseSentence } from "../types";

const pass = (n: number, chapter: number, text: string): Passage => ({ n, chunk_id: `c${n}`, book_id: "b1", chapter_seq: chapter, text });
export const claims: Claim[] = [
  { text: "The council rules the harbour.", passage: 1, quote: "council rules the harbour" },
  { text: "The council taxes the ferries.", passage: 1, quote: "taxes the ferries" },
  { text: "The guild trains the pilots.", passage: 2, quote: "trains the pilots" },
];
const sent = (text: string, cl: number[], pa: number[], kind?: ProseSentence["kind"], para = false): ProseSentence =>
  ({ text, claims: cl, passages: pa, new_paragraph: para, ...(kind ? { kind } : {}) });
export const s1: ProseSentence[] = [sent("The council rules the harbour and taxes the ferries.", [1, 2], [1])];
export const s2: ProseSentence[] = [sent("The guild trains the pilots.", [3], [2])];
export const closeProse: ProseSentence[] = [
  sent("On balance, power here rests on trade.", [1, 3], [1, 2], "reading", true),
  sent("A case can be made that the guild matters more.", [3], [2], "counter"),
  sent("The books don't settle: who founded the council.", [], [], "limit", true),
];
export const overviewProse: ProseSentence[] = [sent("A council and a guild run the harbour.", [1, 3], [1, 2])];

const step = (t: number, name: any, state: any, part: number | null = null, detail: string | null = null): AskEvent =>
  ({ type: "step", t, name, state, part, detail });
export const planEvent: AskEvent = { type: "plan", t: 1, scope: "the harbour", candidates: 5, sections: [
  { index: 0, heading: "The council", entities: [{ id: "e1", name: "Council", kind: "Faction" }] },
  { index: 1, heading: "The guild", entities: [{ id: "e2", name: "Guild", kind: "Faction" }] }] };
export const start = (index: number, heading: string, kind: "section" | "close" = "section"): AskEvent =>
  ({ type: "section", t: 2, index, state: "start", kind, heading });
export const done = (index: number, heading: string, prose: ProseSentence[], cl: Claim[], range: [number, number] | null, passages: Passage[], extra = {}): AskEvent =>
  ({ type: "section", t: 3, index, state: "done", kind: "section", heading, style: "prose", text: prose.map((p) => p.text).join(" "),
    prose, claims: cl, claims_range: range, passages, note: null, ...extra } as AskEvent);

export const head: AskEvent[] = [
  step(0, "split", "start"), { type: "parts", t: 0.2, sub_questions: ["What runs the harbour?"] }, step(0.2, "split", "done", null, "1 part"),
  step(0.3, "plan", "start"), planEvent, step(1, "plan", "done", null, "2 sections")];
export const sec0: AskEvent[] = [step(1, "section", "start", 0), start(0, "The council"),
  done(0, "The council", s1, claims.slice(0, 2), [1, 2], [pass(1, 3, "The council rules the harbour and taxes the ferries.")]),
  step(2, "section", "done", 0, "2 claims")];
export const sec1: AskEvent[] = [step(2, "section", "start", 1), start(1, "The guild"),
  done(1, "The guild", s2, claims.slice(2), [3, 3], [pass(2, 5, "The guild trains the pilots of the bay.")]),
  step(3, "section", "done", 1, "1 claim")];
export const tail: AskEvent[] = [
  { type: "overview", t: 4, prose: overviewProse, text: "" },
  { ...start(2, "Reading, counter-case and limits", "close"), state: "done", style: "prose", text: "", prose: closeProse,
    claims: [], claims_range: null, passages: [], note: null } as AskEvent,
  { type: "answer", t: 5, result: { answer: "", mode: "graph", template: "deep", note: null, model: "m", selection_model: "m",
    sub_questions: ["The council", "The guild"], claims, passages: [pass(1, 3, "The council rules the harbour and taxes the ferries."), pass(2, 5, "The guild trains the pilots of the bay.")],
    edges: [], seeds: [], answer_style: "sectioned", prose: [], requests: 11, overview: overviewProse,
    sections: [
      { heading: "The council", kind: "section", style: "prose", text: "", prose: s1, claims_range: [1, 2], entities: ["Council"], note: null },
      { heading: "The guild", kind: "section", style: "prose", text: "", prose: s2, claims_range: [3, 3], entities: ["Guild"], note: null },
      { heading: "Reading, counter-case and limits", kind: "close", style: "prose", text: "", prose: closeProse, claims_range: null, entities: [], note: null }] } }];
export const full: AskEvent[] = [...head, ...sec0, ...sec1, ...tail];
