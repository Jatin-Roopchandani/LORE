import { STEP_ORDER, type ApiError, type AskEvent, type Claim, type Edge, type Passage, type PlanSection, type ProseSentence, type SearchResult, type Seed, type SectionEvent, type StepName } from "./types";

export type StepState = "pending" | "running" | "done" | "skipped";
export interface StepView { name: StepName; state: StepState; detail: string | null; startedAt: number | null; doneAt: number | null }
// The planner row and one row per section of a deep answer, in the order they arrived.
export interface DeepStepView { kind: "plan" | "section"; part: number | null; state: StepState; detail: string | null; startedAt: number | null; doneAt: number | null }
export interface PlanView { scope: string; candidates: number; sections: PlanSection[] }
// One section of a sectioned answer as it streams in: writing, then done, skipped (budget) or failed.
export interface SectionView { index: number; kind: "section" | "close"; heading: string;
  state: "writing" | "done" | "skipped" | "failed" | "empty"; style: "prose" | "claims" | null;
  text: string; prose: ProseSentence[]; claimsRange: [number, number] | null; reason: "budget" | "reserve" | null }
export interface PartEdges { template: string | null; total: number; edges: Edge[] }
export interface AskState {
  status: "idle" | "running" | "done" | "error" | "interrupted";
  question: string; steps: StepView[]; subQuestions: string[];
  seeds: Seed[][]; edges: PartEdges[]; evidence: { chunkIds: string[]; edgeKeys: string[] } | null;
  result: SearchResult | null; error: ApiError | null; elapsed: number;
  // Per retrieval part: the plan part it is a lens of (null if none), and the plan's own questions.
  lensOf: (number | null)[]; planQuestions: string[];
  // Deep answers: the plan, the sections so far, the overview (it arrives last), and the claims and passages
  // the sections have brought so far, in the answer's own numbering (so citations work before the answer lands).
  deepSteps: DeepStepView[]; plan: PlanView | null; sections: SectionView[];
  overview: ProseSentence[] | null; liveClaims: Claim[]; livePassages: Passage[];
}

export function initialState(question = ""): AskState {
  return {
    status: question ? "running" : "idle", question,
    steps: STEP_ORDER.map((name) => ({ name, state: "pending", detail: null, startedAt: null, doneAt: null })),
    subQuestions: [], seeds: [], edges: [], evidence: null, result: null, error: null, elapsed: 0,
    lensOf: [], planQuestions: [], deepSteps: [], plan: null, sections: [], overview: null, liveClaims: [], livePassages: [],
  };
}

const PART_STEPS: StepName[] = ["seeds", "search"];

function setAt<T>(list: T[], index: number, value: T): T[] {
  const copy = list.slice();
  copy[index] = value;
  return copy;
}

const isDeepStep = (name: string) => name === "plan" || name === "section";

function deepStep(s: AskState, event: Extract<AskEvent, { type: "step" }>): AskState {
  const kind = event.name as "plan" | "section";
  const part = kind === "plan" ? null : event.part;
  const at = s.deepSteps.findIndex((d) => d.kind === kind && d.part === part);
  if (event.state === "start") {
    const row: DeepStepView = { kind, part, state: "running", detail: null, startedAt: event.t, doneAt: null };
    const deepSteps = at < 0 ? [...s.deepSteps, row] : setAt<DeepStepView>(s.deepSteps, at, { ...s.deepSteps[at], state: "running" });
    // The planner is where the generic rows stop: the question has been split.
    const steps = kind === "plan"
      ? s.steps.map((x) => x.name === "split" && x.state !== "done" ? { ...x, state: "done" as const, doneAt: event.t } : x) : s.steps;
    return { ...s, deepSteps, steps };
  }
  if (at < 0) return s;
  return { ...s, deepSteps: setAt<DeepStepView>(s.deepSteps, at, { ...s.deepSteps[at], state: "done", detail: event.detail ?? s.deepSteps[at].detail, doneAt: event.t }) };
}

function section(s: AskState, e: SectionEvent): AskState {
  const prev = s.sections.find((x) => x.index === e.index);
  const base: SectionView = prev ?? { index: e.index, kind: e.kind, heading: e.heading, state: "writing", style: null,
    text: "", prose: [], claimsRange: null, reason: null };
  let view: SectionView = { ...base, kind: e.kind, heading: e.heading };
  let liveClaims = s.liveClaims;
  let livePassages = s.livePassages;
  if (e.state === "skipped") view = { ...view, state: "skipped", reason: e.reason ?? "budget" };
  else if (e.state === "done") {
    const style = e.style ?? "prose";
    view = { ...view, state: style === "failed" ? "failed" : style === "empty" ? "empty" : "done",
      style: style === "prose" || style === "claims" ? style : null,
      text: e.text ?? "", prose: e.prose ?? [], claimsRange: e.claims_range ?? null };
    if (e.claims_range && e.claims) {
      liveClaims = liveClaims.slice();
      e.claims.forEach((c, i) => { liveClaims[e.claims_range![0] - 1 + i] = c; });
    }
    const have = new Set(livePassages.map((p) => p.n));
    const fresh = (e.passages ?? []).filter((p) => !have.has(p.n));
    if (fresh.length) livePassages = [...livePassages, ...fresh];
  }
  const sections = prev ? s.sections.map((x) => x.index === e.index ? view : x) : [...s.sections, view].sort((a, b) => a.index - b.index);
  return { ...s, sections, liveClaims, livePassages };
}

export function askReducer(state: AskState, event: AskEvent): AskState {
  const s: AskState = { ...state, elapsed: Math.max(state.elapsed, event.t ?? 0) };
  switch (event.type) {
    case "step": {
      if (isDeepStep(event.name)) return deepStep(s, event);
      const name: StepName = event.name;
      const i = STEP_ORDER.indexOf(name);
      const steps = s.steps.map((step, j) => {
        if (event.state === "start") {
          if (j === i) return { ...step, state: "running" as const, startedAt: step.startedAt ?? event.t };
          if (j < i && !(name === "check" && step.name === "compose") && (step.state === "running" || step.state === "pending")) return { ...step, state: "done" as const, doneAt: event.t };
          return step;
        }
        if (j !== i) return step;
        const lastPart = !PART_STEPS.includes(name) || s.subQuestions.length === 0
          || event.part === s.subQuestions.length - 1;
        return { ...step, detail: event.detail ?? step.detail, state: lastPart ? ("done" as const) : step.state,
          doneAt: lastPart ? event.t : step.doneAt };
      });
      return { ...s, steps };
    }
    case "parts":
      return { ...s, subQuestions: event.sub_questions, lensOf: event.lens_of ?? [], planQuestions: event.plan_questions ?? [] };
    case "plan":
      return { ...s, plan: { scope: event.scope, candidates: event.candidates, sections: event.sections } };
    case "section":
      return section(s, event);
    case "overview":
      return { ...s, overview: event.prose };
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
        deepSteps: s.deepSteps.map((d) => d.state === "running" ? { ...d, state: "done", doneAt: event.t } : d),
      };
    case "error":
      return {
        ...s, status: event.error.code === "interrupted" ? "interrupted" : "error", error: event.error,
        steps: s.steps.map((step) => step.state === "running" ? { ...step, state: "pending" } : step),
        deepSteps: s.deepSteps.map((d) => d.state === "running" ? { ...d, state: "pending" } : d),
        sections: s.sections.map((x) => x.state === "writing" ? { ...x, state: "failed" } : x),
      };
  }
}

// What a prose view reads its citations from: the final answer, or while a deep answer is still arriving,
// the claims and passages its sections have brought so far.
export function citationSource(state: AskState): SearchResult {
  if (state.result) return state.result;
  return { answer: "", mode: "graph", template: null, note: null, model: null, selection_model: null,
    sub_questions: state.subQuestions, claims: state.liveClaims, passages: state.livePassages, edges: [], seeds: [] };
}
