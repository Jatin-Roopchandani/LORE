import { STEP_ORDER, type ApiError, type AskEvent, type Edge, type SearchResult, type Seed, type StepName } from "./types";

export type StepState = "pending" | "running" | "done" | "skipped";
export interface StepView { name: StepName; state: StepState; detail: string | null; startedAt: number | null; doneAt: number | null }
export interface PartEdges { template: string | null; total: number; edges: Edge[] }
export interface AskState {
  status: "idle" | "running" | "done" | "error" | "interrupted";
  question: string; steps: StepView[]; subQuestions: string[];
  seeds: Seed[][]; edges: PartEdges[]; evidence: { chunkIds: string[]; edgeKeys: string[] } | null;
  result: SearchResult | null; error: ApiError | null; elapsed: number;
}

export function initialState(question = ""): AskState {
  return {
    status: question ? "running" : "idle", question,
    steps: STEP_ORDER.map((name) => ({ name, state: "pending", detail: null, startedAt: null, doneAt: null })),
    subQuestions: [], seeds: [], edges: [], evidence: null, result: null, error: null, elapsed: 0,
  };
}

const PART_STEPS: StepName[] = ["seeds", "search"];

function setAt<T>(list: T[], index: number, value: T): T[] {
  const copy = list.slice();
  copy[index] = value;
  return copy;
}

export function askReducer(state: AskState, event: AskEvent): AskState {
  const s: AskState = { ...state, elapsed: Math.max(state.elapsed, event.t ?? 0) };
  switch (event.type) {
    case "step": {
      const i = STEP_ORDER.indexOf(event.name);
      const steps = s.steps.map((step, j) => {
        if (event.state === "start") {
          if (j === i) return { ...step, state: "running" as const, startedAt: step.startedAt ?? event.t };
          if (j < i && !(event.name === "check" && step.name === "compose") && (step.state === "running" || step.state === "pending")) return { ...step, state: "done" as const, doneAt: event.t };
          return step;
        }
        if (j !== i) return step;
        const lastPart = !PART_STEPS.includes(event.name) || s.subQuestions.length === 0
          || event.part === s.subQuestions.length - 1;
        return { ...step, detail: event.detail ?? step.detail, state: lastPart ? ("done" as const) : step.state,
          doneAt: lastPart ? event.t : step.doneAt };
      });
      return { ...s, steps };
    }
    case "parts":
      return { ...s, subQuestions: event.sub_questions };
    case "seeds":
      return { ...s, seeds: setAt(s.seeds, event.part, [...(s.seeds[event.part] ?? []), ...event.entities]) };
    case "edges": {
      const prev = s.edges[event.part] ?? { template: event.template, total: 0, edges: [] };
      return { ...s, edges: setAt(s.edges, event.part, { template: event.template, total: event.total, edges: [...prev.edges, ...event.edges] }) };
    }
    case "evidence":
      return { ...s, evidence: { chunkIds: event.chunk_ids, edgeKeys: event.edge_keys } };
    case "answer":
      return {
        ...s, status: "done", result: event.result,
        subQuestions: s.subQuestions.length ? s.subQuestions : event.result.sub_questions,
        steps: s.steps.map((step) => step.state === "running" ? { ...step, state: "done", doneAt: event.t }
          : step.state === "pending" ? { ...step, state: "skipped" } : step),
      };
    case "error":
      return {
        ...s, status: event.error.code === "interrupted" ? "interrupted" : "error", error: event.error,
        steps: s.steps.map((step) => step.state === "running" ? { ...step, state: "pending" } : step),
      };
  }
}
