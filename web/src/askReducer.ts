import { STEP_ORDER, type ApiError, type AskEvent, type Claim, type Edge, type Passage, type PlanSection, type ProseSentence, type SearchResult, type Seed, type SectionEvent, type StepName } from "./types";

// `failed`: a section that could not be written; `skipped`: one not written (nothing found, or the budget).
export type StepState = "pending" | "running" | "done" | "skipped" | "failed";
export interface StepView { name: StepName; state: StepState; detail: string | null; startedAt: number | null; doneAt: number | null }
// The planner row and one row per section of a deep answer, in the order they arrived.
export interface DeepStepView { kind: "plan" | "recap" | "section"; part: number | null; state: StepState; detail: string | null; startedAt: number | null; doneAt: number | null }
export interface PlanView { scope: string; candidates: number; sections: PlanSection[] }
// One section of a sectioned answer as it streams in: writing, then done, skipped (budget) or failed.
export interface SectionView { index: number; kind: "section" | "close"; heading: string;
  state: "writing" | "done" | "skipped" | "failed" | "empty"; style: "prose" | "claims" | null;
  text: string; prose: ProseSentence[]; claimsRange: [number, number] | null; reason: "budget" | "reserve" | null;
  // The interpretive part this lens section is grouped under ("" for none).
  group: string;
  // The part's full question (`group` is its short label; "" for none).
  groupTitle: string;
  // The full lens question this section answers; the heading is a short label ("" for none).
  title: string }
export interface PartEdges { template: string | null; total: number; edges: Edge[] }
export interface AskState {
  status: "idle" | "running" | "done" | "error" | "interrupted";
  question: string; steps: StepView[]; subQuestions: string[];
  seeds: Seed[][]; edges: PartEdges[]; evidence: { chunkIds: string[]; edgeKeys: string[] } | null;
  result: SearchResult | null; error: ApiError | null; elapsed: number;
  // Per retrieval part: the plan part it is a lens of (null if none), and the plan's own questions.
  lensOf: (number | null)[]; planQuestions: string[];
  // Deep answers: the plan, the sections so far, and the claims and passages
  // the sections have brought so far, in the answer's own numbering (so citations work before the answer lands).
  deepSteps: DeepStepView[]; plan: PlanView | null; sections: SectionView[];
  liveClaims: Claim[]; livePassages: Passage[];
  // A deep answer's short answer: sent before its sections when interpretive, after them otherwise.
  shortAnswer: ProseSentence[] | null;
  // The question's request id (its first event): what "Report this answer" quotes.
  requestId: string | null;
}

export function initialState(question = ""): AskState {
  return {
    status: question ? "running" : "idle", question,
    steps: STEP_ORDER.map((name) => ({ name, state: "pending", detail: null, startedAt: null, doneAt: null })),
    subQuestions: [], seeds: [], edges: [], evidence: null, result: null, error: null, elapsed: 0,
    lensOf: [], planQuestions: [], deepSteps: [], plan: null, sections: [], liveClaims: [], livePassages: [], shortAnswer: null, requestId: null,
  };
}

const PART_STEPS: StepName[] = ["seeds", "search"];

function setAt<T>(list: T[], index: number, value: T): T[] {
  const copy = list.slice();
  copy[index] = value;
  return copy;
}

const isDeepStep = (name: string) => name === "plan" || name === "recap" || name === "section";

function deepStep(s: AskState, event: Extract<AskEvent, { type: "step" }>): AskState {
  const kind = event.name as "plan" | "recap" | "section";
  const part = kind === "section" ? event.part : null;
  const at = s.deepSteps.findIndex((d) => d.kind === kind && d.part === part);
  if (event.state === "start") {
    const row: DeepStepView = { kind, part, state: "running", detail: null, startedAt: event.t, doneAt: null };
    const deepSteps = at < 0 ? [...s.deepSteps, row] : setAt<DeepStepView>(s.deepSteps, at, { ...s.deepSteps[at], state: "running" });
    // The planner (or a recap's gathering) is where the generic rows stop: the question has been split.
    const steps = kind !== "section"
      ? s.steps.map((x) => x.name === "split" && x.state !== "done" ? { ...x, state: "done" as const, doneAt: event.t } : x) : s.steps;
    return { ...s, deepSteps, steps };
  }
  if (at < 0) return s;
  const row = s.deepSteps[at];
  // A section already marked failed or skipped (its `section` event came first) keeps that mark.
  const state = row.state === "failed" || row.state === "skipped" ? row.state : "done";
  return { ...s, deepSteps: setAt<DeepStepView>(s.deepSteps, at, { ...row, state, detail: event.detail ?? row.detail, doneAt: event.t }) };
}

function section(s: AskState, e: SectionEvent): AskState {
  const prev = s.sections.find((x) => x.index === e.index);
  const base: SectionView = prev ?? { index: e.index, kind: e.kind, heading: e.heading, state: "writing", style: null,
    text: "", prose: [], claimsRange: null, reason: null, group: e.group ?? "", groupTitle: e.group_title ?? "", title: e.title ?? "" };
  let view: SectionView = { ...base, kind: e.kind, heading: e.heading, group: e.group ?? base.group,
    groupTitle: e.group_title ?? base.groupTitle, title: e.title ?? base.title };
  let deepSteps = s.deepSteps;
  let liveClaims = s.liveClaims;
  let livePassages = s.livePassages;
  // The steps list tells the truth about a section: a failed one is failed, one never written is skipped.
  const mark = (state: "failed" | "skipped") => {
    const at = deepSteps.findIndex((d) => d.kind === "section" && d.part === e.index);
    const row: DeepStepView = at < 0 ? { kind: "section", part: e.index, state, detail: null, startedAt: null, doneAt: null }
      : { ...deepSteps[at], state };
    deepSteps = at < 0 ? [...deepSteps, row] : setAt<DeepStepView>(deepSteps, at, row);
  };
  if (e.state === "skipped") { view = { ...view, state: "skipped", reason: e.reason ?? "budget" }; mark("skipped"); }
  else if (e.state === "done") {
    const style = e.style ?? "prose";
    if (style === "failed") mark("failed");
    else if (style === "empty") mark("skipped");
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
  return { ...s, sections, liveClaims, livePassages, deepSteps };
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
    case "short_answer": {
      // The claims and passages come whole, in the answer's numbering, so its citations work at once.
      const have = new Set(s.livePassages.map((p) => p.n));
      const fresh = (event.passages ?? []).filter((p) => !have.has(p.n));
      return { ...s, shortAnswer: event.prose, liveClaims: event.claims?.length ? event.claims : s.liveClaims,
        livePassages: fresh.length ? [...s.livePassages, ...fresh] : s.livePassages };
    }
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
    case "request":
      return { ...s, requestId: event.request_id };
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


/** Sections of a sectioned answer that could not be written (failed, or found nothing): the final result's own count
 *  once the answer has landed, until then the ones the stream has said so far. */
export function unwrittenCount(state: AskState): number {
  if (state.result) return state.result.unwritten_sections ?? 0;
  return state.sections.filter((x) => x.state === "failed" || x.state === "empty").length;
}
