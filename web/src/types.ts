export type StepName = "split" | "seeds" | "search" | "rank" | "write" | "compose" | "check";
export const STEP_ORDER: StepName[] = ["split", "seeds", "search", "rank", "write", "compose", "check"];
export interface Seed { id: string; name: string; kind: string; books: string[]; score: number | null }
export interface Edge { subject: string; predicate: string; predicate_raw: string; object: string;
  evidence: string; confidence: number; book_id: string; chapter_seq: number; chunk_id: string;
  subject_id: string; object_id: string }
export interface Claim { text: string; passage: number; quote: string }
export interface Passage { n: number; chunk_id: string; book_id: string; book_title: string | null;
  chapter_seq: number; text: string }
export interface ProseSentence { text: string; claims: number[]; passages: number[]; new_paragraph: boolean;
  // "reading": an interpretation of the cited facts, not a direct statement;
  // "judgement": a compared, arguable case for a "who is the most ..." question (absent means "fact")
  kind?: "fact" | "reading" | "judgement" }
export interface SearchResult { answer: string; mode: string; template: string | null; note: string | null;
  model: string | null; selection_model: string | null; sub_questions: string[]; claims: Claim[];
  passages: Passage[]; edges: Edge[]; seeds: Seed[];
  prose?: ProseSentence[]; answer_style?: "prose" | "claims"; unanswered?: string[]; gaps?: string[] }
export interface ApiError { code: string; message: string }
export type AskEvent =
  | { type: "step"; t: number; name: StepName; state: "start" | "done"; part: number | null; detail: string | null }
  | { type: "parts"; t: number; sub_questions: string[] }
  | { type: "seeds"; t: number; part: number; entities: Seed[] }
  | { type: "edges"; t: number; part: number; template: string | null; total: number; edges: Edge[] }
  | { type: "evidence"; t: number; chunk_ids: string[]; edge_keys: string[] }
  | { type: "answer"; t: number; result: SearchResult }
  | { type: "error"; t: number; error: ApiError };
export interface Book { id: string; title: string; author: string | null; status: string;
  chunk_count: number; entity_count: number | null }
export interface QuotaModel { model: string; count: number; limit: number; remaining: number }
export interface Quota { provider: "gemini" | "local"; models: QuotaModel[]; resets_at: string }
export interface McpInfo { stdio_command: string; http_command: string; http_url: string; http_running: boolean }
export interface EntityCardData { id: string; name: string; kind: string; aliases: string[]; books: string[];
  edges_out: Edge[]; edges_in: Edge[]; edges_out_total: number; edges_in_total: number; truncated: boolean }
export interface EntityKind { id: string; name: string; kind: string; books: string[] }
export interface SubgraphNode { id: string; name: string; kind: string; books: string[] }
export interface SubgraphLink { subject: string; subject_id: string; object: string; object_id: string;
  count: number; predicates: Record<string, number> }
export interface SubgraphData { nodes: SubgraphNode[]; edges: SubgraphLink[]; truncated: boolean; depth: number }
export const edgeKey = (e: Edge) => `${e.subject_id}|${e.predicate}|${e.object_id}|${e.chunk_id}`;
export interface BookJob { stage: string; state: "running" | "waiting_quota" | "failed" | "done" | "idle";
  done: number; total: number; stopped_reason: string | null; resumable_at: string | null;
  can_continue: boolean; continue_reason: string }
export interface LibraryBook { id: string; title: string; author: string | null; status: string;
  series: string | null; series_index: number | null; job: BookJob | null }
export interface GeminiUse { used_today: number; limit: number; answer_reserve: number }
export interface LibraryStatus { books: LibraryBook[]; gemini: GeminiUse }
