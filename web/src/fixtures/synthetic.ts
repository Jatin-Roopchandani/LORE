import type { AskEvent } from "../types";
const step = (t: number, name: any, state: any, part: number | null = null, detail: string | null = null): AskEvent =>
  ({ type: "step", t, name, state, part, detail });
export const interrupted: AskEvent[] = [step(0, "split", "start"), { type: "parts", t: 0.4, sub_questions: ["q"] },
  step(0.4, "split", "done", null, "1 part"), step(0.5, "seeds", "start", 0), { type: "error", t: 0, error: { code: "interrupted", message: "" } }];
export const budget: AskEvent[] = [{ type: "error", t: 0, error: { code: "budget_exhausted", message: "spent" } }];
export const empty: AskEvent[] = [{ type: "answer", t: 1.2, result: { answer: "The loaded books do not contain enough to answer that.", mode: "empty",
  template: null, note: null, model: null, selection_model: null, sub_questions: [], claims: [], passages: [], edges: [], seeds: [] } }];
