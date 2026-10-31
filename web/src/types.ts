export type StepName = "split" | "seeds" | "search" | "rank" | "write" | "compose" | "check";
// Rows that exist only in a deep answer: the planner and one per section (see askReducer's deepSteps).
export type DeepStepName = "plan" | "section";
export type Depth = "auto" | "deep" | "focused";
export const STEP_ORDER: StepName[] = ["split", "seeds", "search", "rank", "write", "compose", "check"];
export interface Seed { id: string; name: string; kind: string; books: string[]; score: number | null }
export interface Edge { subject: string; predicate: string; predicate_raw: string; object: string;
  evidence: string; confidence: number; book_id: string; chapter_seq: number; chunk_id: string;
  subject_id: string; object_id: string }
export interface Claim { text: string; passage: number; quote: string }
// `book_title` is absent on the passages a `section` event carries.
export interface Passage { n: number; chunk_id: string; book_id: string; book_title?: string | null;
  chapter_seq: number; text: string }
export interface ProseSentence { text: string; claims: number[]; passages: number[]; new_paragraph: boolean;
  // "reading": LORE's interpretation of the cited facts, not a direct statement;
  // "counter": the strongest case for the other side; "limit": what the books don't settle (no citations).
  // Absent means "fact".
  kind?: "fact" | "reading" | "counter" | "limit" }
// One section of a sectioned (deep) answer. Claim and passage numbers are the answer's own, as everywhere.
export interface SectionOut { heading: string; kind: "section" | "close"; style: "prose" | "claims"; text: string;
  prose: ProseSentence[]; claims_range: [number, number] | null; entities: string[]; note: string | null }
export interface SearchResult { answer: string; mode: string; template: string | null; note: string | null;
  model: string | null; selection_model: string | null; sub_questions: string[]; claims: Claim[];
  passages: Passage[]; edges: Edge[]; seeds: Seed[];
  prose?: ProseSentence[]; answer_style?: "prose" | "claims" | "sectioned"; unanswered?: string[]; gaps?: string[];
  // Sectioned answers only: the cited overview, the sections, and the model requests the answer used.
  overview?: ProseSentence[]; sections?: SectionOut[]; requests?: number }
export interface PlanSection { index: number; heading: string; entities: { id: string; name: string; kind: string }[] }
export interface SectionEvent { type: "section"; t: number; index: number; kind: "section" | "close"; heading: string;
  state: "start" | "done" | "skipped";
  style?: "prose" | "claims" | "empty" | "failed"; text?: string; prose?: ProseSentence[]; claims?: Claim[];
  claims_range?: [number, number] | null; passages?: Passage[]; note?: string | null;
  reason?: "budget" | "reserve" }
export interface ApiError { code: string; message: string }
export type AskEvent =
  | { type: "step"; t: number; name: StepName | DeepStepName; state: "start" | "done"; part: number | null; detail: string | null }
  // `lens_of` (per part: the plan part it is a lens of, or null) and `plan_questions` come only when lenses exist.
  | { type: "parts"; t: number; sub_questions: string[]; lens_of?: (number | null)[]; plan_questions?: string[] }
  | { type: "plan"; t: number; scope: string; candidates: number; sections: PlanSection[] }
  | SectionEvent
  | { type: "overview"; t: number; prose: ProseSentence[]; text: string }
  | { type: "seeds"; t: number; part: number; entities: Seed[] }
  | { type: "edges"; t: number; part: number; template: string | null; total: number; edges: Edge[] }
  | { type: "evidence"; t: number; chunk_ids: string[]; edge_keys: string[] }
  | { type: "answer"; t: number; result: SearchResult }
  | { type: "error"; t: number; error: ApiError };
export interface Book { id: string; title: string; author: string | null; status: string;
  chunk_count: number; entity_count: number | null; world?: string | null }
// Set on nodes and entities only when several worlds are shown together (the "all worlds" view).
export interface WorldRef { id: number; name: string }
export interface WorldBook { id: string; title: string; series: string | null; series_index: number | null }
export interface World { id: number; name: string; books: WorldBook[] }
export interface QuotaModel { model: string; count: number; limit: number; remaining: number }
export interface Quota { provider: "gemini" | "local"; models: QuotaModel[]; resets_at: string }
export interface McpInfo { stdio_command: string; http_command: string; http_url: string; http_running: boolean }
export interface EntityCardData { id: string; name: string; kind: string; aliases: string[]; books: string[]; world?: WorldRef;
  edges_out: Edge[]; edges_in: Edge[]; edges_out_total: number; edges_in_total: number; truncated: boolean }
export interface EntityKind { id: string; name: string; kind: string; books: string[]; world?: WorldRef }
export interface SubgraphNode { id: string; name: string; kind: string; books: string[]; world?: WorldRef }
export interface SubgraphLink { subject: string; subject_id: string; object: string; object_id: string;
  count: number; predicates: Record<string, number> }
export interface SubgraphData { nodes: SubgraphNode[]; edges: SubgraphLink[]; truncated: boolean; depth: number }
export const edgeKey = (e: Edge) => `${e.subject_id}|${e.predicate}|${e.object_id}|${e.chunk_id}`;
export interface BookJob { stage: string; state: "running" | "waiting_quota" | "failed" | "done" | "idle";
  done: number; total: number; stopped_reason: string | null; resumable_at: string | null;
  can_continue: boolean; continue_reason: string }
export interface LibraryBook { id: string; title: string; author: string | null; status: string;
  world: string | null; series: string | null; series_index: number | null; job: BookJob | null }
export type MoveResult = { started: true; world: WorldRef } | { started: false };
export interface GeminiUse { used_today: number; limit: number; answer_reserve: number }
export interface LibraryStatus { books: LibraryBook[]; gemini: GeminiUse }
