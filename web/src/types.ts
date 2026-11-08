export type StepName = "split" | "seeds" | "search" | "rank" | "write" | "compose" | "check";
// Rows that exist only in a sectioned answer: the planner (or, for a recap, gathering the story's span) and one per
// section (see askReducer's deepSteps).
export type DeepStepName = "plan" | "recap" | "section";
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
  kind?: "fact" | "reading" | "counter" | "limit";
  // A short answer part's short label (several interpretive parts only), shown muted before the sentence.
  label?: string }
// One section of a sectioned (deep) answer. Claim and passage numbers are the answer's own, as everywhere.
export interface SectionOut { heading: string; kind: "section" | "close"; style: "prose" | "claims"; text: string;
  prose: ProseSentence[]; claims_range: [number, number] | null; entities: string[]; note: string | null;
  // The interpretive part this lens section belongs to; "" (or absent) unless the question has several parts.
  group?: string;
  // The part's full question (`group` is its short label); "" or absent without a group.
  group_title?: string;
  // The full lens question the section answers (its heading is a short label); "" or absent otherwise.
  title?: string }
export interface SearchResult { answer: string; mode: string; template: string | null; note: string | null;
  model: string | null; selection_model: string | null; sub_questions: string[]; claims: Claim[];
  passages: Passage[]; edges: Edge[]; seeds: Seed[];
  prose?: ProseSentence[]; answer_style?: "prose" | "claims" | "sectioned"; unanswered?: string[]; gaps?: string[];
  // Sectioned answers only: the sections, and the model requests the answer used.
  sections?: SectionOut[]; requests?: number;
  // A deep answer opens with this. Interpretive: one sentence per interpretive part ("reading", or the "limit" line
  // saying the evidence found no clear position). Otherwise a few cited fact sentences (an analytical question's may
  // open with a "reading"), or the "limit" line saying no direct summary could be verified. `prose` and `sections`
  // are the argument below it.
  short_answer?: ProseSentence[];
  // Sections of a sectioned answer that failed or found nothing: they are left out of `sections`, counted here.
  unwritten_sections?: number;
  // Present only when a reading position limited the answer: "Answered from what you've read: up to chapter 9 of <book>".
  position_note?: string }
export interface PlanSection { index: number; heading: string; group?: string; group_title?: string; title?: string;
  entities: { id: string; name: string; kind: string }[] }
export interface SectionEvent { type: "section"; t: number; index: number; kind: "section" | "close"; heading: string;
  state: "start" | "done" | "skipped";
  style?: "prose" | "claims" | "empty" | "failed"; text?: string; prose?: ProseSentence[]; claims?: Claim[];
  claims_range?: [number, number] | null; passages?: Passage[]; note?: string | null;
  reason?: "budget" | "reserve"; group?: string; group_title?: string; title?: string }
// `request_id` (multi-user mode): the reference a friendly server error quotes; `retry_after`: seconds, on a 429.
export interface ApiError { code: string; message: string; request_id?: string; retry_after?: number }
export type AskEvent =
  | { type: "step"; t: number; name: StepName | DeepStepName; state: "start" | "done"; part: number | null; detail: string | null }
  // `lens_of` (per part: the plan part it is a lens of, or null) and `plan_questions` come only when lenses exist.
  | { type: "parts"; t: number; sub_questions: string[]; lens_of?: (number | null)[]; plan_questions?: string[] }
  | { type: "plan"; t: number; scope: string; candidates: number; sections: PlanSection[] }
  | SectionEvent
  // The short answer of a deep answer (before the sections when interpretive, after them otherwise), with every
  // claim and passage it cites.
  | { type: "short_answer"; t: number; prose: ProseSentence[]; text: string; claims?: Claim[]; passages?: Passage[] }
  | { type: "seeds"; t: number; part: number; entities: Seed[] }
  | { type: "edges"; t: number; part: number; template: string | null; total: number; edges: Edge[] }
  | { type: "evidence"; t: number; chunk_ids: string[]; edge_keys: string[] }
  // `chat_id`/`message_id`: where the server saved the answer in the user's history (absent if it could not).
  | { type: "answer"; t: number; result: SearchResult; chat_id?: string; message_id?: number }
  | { type: "error"; t: number; error: ApiError }
  // The first event of every question: its id, which a report of the answer quotes (release step 4).
  | { type: "request"; t: number; request_id: string }
  // A follow-up rewritten to stand alone from the chat's earlier turns, sent only when it differs from what was typed.
  | { type: "understood"; t: number; standalone: string };
export interface Book { id: string; title: string; author: string | null; status: string;
  chunk_count: number; entity_count: number | null; world?: string | null }
// Set on nodes and entities only when several worlds are shown together (the "all worlds" view).
export interface WorldRef { id: number; name: string }
export interface WorldBook { id: string; title: string; series: string | null; series_index: number | null }
export interface World { id: number; name: string; books: WorldBook[] }
export interface QuotaModel { model: string; count: number; limit: number; remaining: number }
// provider "billed": local mode on Claude, OpenAI or a paid Gemini plan (`billed_by`): no free-tier ledger.
export interface Quota { provider: "gemini" | "local" | "billed"; billed_by?: ProviderId; models: QuotaModel[]; resets_at: string }
export interface McpInfo { stdio_command: string; http_command: string; http_url: string; http_running: boolean;
  lore?: string; env?: Record<string, string>; claude_desktop?: string; claude_code?: string; claude_code_http?: string;
  library?: { data_dir: string; central_db: string | null } }
export interface McpCheck { ok: boolean; lore_found: boolean; tools: { name: string; summary: string }[]; problem: string | null;
  library_found?: boolean; hosted?: boolean }
// Multi mode's GET /api/mcp: the hosted endpoint's address (the page builds the snippets).
export interface McpHosted { hosted: true; base_url: string; mcp_url: string; max_tokens: number; oauth: boolean }
// One personal access token as listed (never the token itself; `token` only in the reply that made it).
export interface McpToken { id: string; name: string; prefix: string; created_at: string; last_used_at: string | null }
export interface McpTokenMade extends McpToken { token: string }
// An app connected through OAuth (one per consent), revocable in Settings.
export interface McpConnection { id: string; client_name: string; created_at: string; last_used_at: string | null }
// What the consent page shows: the client's own name (shown as text) and where it sends the browser back to.
export interface ConsentInfo { client_name: string | null; redirect_host: string }
export interface DeviceSession { id: string; device: string; created_at: string; last_active: string; current: boolean }
export interface EntityCardData { id: string; name: string; kind: string; aliases: string[]; books: string[]; world?: WorldRef;
  edges_out: Edge[]; edges_in: Edge[]; edges_out_total: number; edges_in_total: number; truncated: boolean }
export interface EntityKind { id: string; name: string; kind: string; books: string[]; world?: WorldRef }
export interface SubgraphNode { id: string; name: string; kind: string; books: string[]; world?: WorldRef }
export interface SubgraphLink { subject: string; subject_id: string; object: string; object_id: string;
  count: number; predicates: Record<string, number> }
export interface SubgraphData { nodes: SubgraphNode[]; edges: SubgraphLink[]; truncated: boolean; depth: number }
export const edgeKey = (e: Edge) => `${e.subject_id}|${e.predicate}|${e.object_id}|${e.chunk_id}`;
export interface BookJob { stage: string; state: "running" | "waiting_quota" | "waiting_key" | "failed" | "done" | "idle";
  done: number; total: number; stopped_reason: string | null; resumable_at: string | null;
  can_continue: boolean; continue_reason: string;
  // The provider the book's latest run used (multi mode: the user's choice at the time); absent from older servers.
  provider?: ProviderId | null;
  // The whole pipeline as the server will run it for this book (lore/orchestrator/plan.py), with seconds left per step
  // (null: unknown) and in all (`seconds_left`: the known part; `unknown_left`: some step has no estimate).
  plan?: StepPlan | null }
export interface PlanStep { stage: string; state: "done" | "current" | "upcoming"; done?: number; total?: number;
  seconds_left: number | null }
export interface StepPlan { steps: PlanStep[]; step: number; of: number; seconds_left: number | null; unknown_left: boolean }
export interface LibraryBook { id: string; title: string; author: string | null; status: string;
  world: string | null; series: string | null; series_index: number | null; job: BookJob | null }
export type MoveResult = { started: true; world: WorldRef } | { started: false };
export interface GeminiUse { used_today: number; limit: number; answer_reserve: number }
// `usage`: what the footer shows. `free`: Gemini's free tier (the `gemini` numbers). `metered`: Claude, OpenAI or a
// paid Gemini plan, with today's tokens, an estimated cost (where a price is known) and the cap (multi mode only).
export type LibraryUsage = { mode: "free"; provider: "gemini" }
  | { mode: "metered"; provider: ProviderId; tokens_today: number | null; cost_usd: number | null; daily_token_cap: number | null };
export interface LibraryStatus { books: LibraryBook[]; gemini: GeminiUse; usage?: LibraryUsage }

// The graph explorer's reads (profile, timeline, relations, edge, search, path, passage).
export interface EntityRef { id: string; name: string; kind: string }
export interface Quote { quote: string; chunk_id: string; book_id: string; chapter: number }
export interface ProfileStatement extends Quote { text: string }
export interface EntityProfile { id: string; name: string; kind: string; books: string[]; summary: string | null;
  aspects: { aspect: string; statements: ProfileStatement[] }[];
  counts: { beats: number; relations: number; chapters: number }; world?: WorldRef;
  // Set while a reading position is on: the whole-story summary is hidden and the statements shown are those read so far.
  summary_hidden?: boolean;
  // Under a reading position: a short intro written only from what has been read. `intro_pending` means a
  // written one may be fetched from /intro (the one shown meanwhile is the plain fallback).
  intro?: string | null; intro_source?: "model" | "fallback" | "none"; intro_pending?: boolean }
export interface EntityIntro { intro: string | null; source: "model" | "fallback" | "none" }
export interface Beat { kind: string; text: string; quote: string; chunk_id: string; book_id: string; book_title: string | null;
  chapter: number; position_label: string; others: { id: string | null; name: string }[] }
export interface Timeline { beats: Beat[]; order_note: string | null }
export interface PredicateCount { predicate: string; count: number; direction: "out" | "in" }
export interface ChapterRef { book_id: string; book_title: string | null; chapter: number }
export interface Relation { other: EntityRef; predicates: PredicateCount[]; evidence: Quote[];
  first_chapter: ChapterRef | null; last_chapter: ChapterRef | null }
export interface Relations { relations: Relation[]; order_note: string | null }
export interface EdgeRelationship { subject: EntityRef; object: EntityRef; predicate: string; predicate_raw: string;
  quote: string; chunk_id: string; book_id: string; book_title: string | null; chapter: number; position_label: string }
export interface EdgeDetail { a: EntityRef; b: EntityRef; relationships: EdgeRelationship[]; predicates: PredicateCount[]; order_note: string | null }
export interface SearchHit { id: string; name: string; kind: string; books: string[]; matched_alias: string | null; world?: WorldRef }
export interface PathHop { from: string; to: string; predicates: PredicateCount[]; evidence: Quote[] }
export interface PathResult { nodes: EntityRef[]; hops: PathHop[]; found: boolean; timed_out: boolean }
export interface PassageText { chunk_id: string; book_id: string; book_title: string | null; chapter: number; text: string; position_label: string }

// Sign-in and accounts (multi-user mode).
export interface AuthProviders { providers: string[]; email: boolean; invite_only: boolean }
export interface Me { user: { id: string; email: string; name: string | null; role: "user" | "admin" };
  identities?: { provider: string; created_at: string }[] }
export interface WaitlistEntry { id: number; email: string; reading: string | null; status: "pending" | "invited" | "declined";
  created_at: string; invited_at: string | null }
export interface AdminUser { id: string; email: string; name: string | null; role: string; status: string; created_at: string;
  has_key?: boolean; requests_today?: number }
// The signed-in user's Gemini key (multi-user mode): never the key itself, only its last four characters.
export interface KeyStatus { provider?: ProviderId; present: boolean; last4: string | null; added_at: string | null; last_ok_at: string | null;
  last_error: "rejected" | "unchecked" | null }
export interface KeySaved extends KeyStatus { check: "ok" | "unavailable"; warning?: string; models?: string[]; active?: ProviderId }
// The model providers a user can connect (GET /api/me/providers). Keys are never sent back, only their last four.
export type ProviderId = "gemini" | "anthropic" | "openai";
export interface ProviderInfo { provider: ProviderId; connected: boolean; active: boolean; last4: string | null;
  added_at: string | null; last_ok_at: string | null; last_error: "rejected" | "unchecked" | null;
  model: string | null; fast_model: string | null; models: string[]; paid_tier?: boolean }
// gemini_paid: the user's "I'm on a paid Gemini plan"; gemini_paid_hint: their free-setting key went past the free-tier
// daily count today with no 429 from Google (a hint only: LORE never switches it on).
export interface ProvidersState { active: ProviderId; providers: ProviderInfo[]; daily_token_cap: number | null;
  gemini_paid?: boolean; gemini_paid_hint?: boolean }
export interface TokenTotals { requests: number; input_tokens: number; output_tokens: number;
  estimated_cost_usd: number | null; models: Record<string, { provider: ProviderId; requests: number;
    input_tokens: number; output_tokens: number; estimated_cost_usd: number | null }> }
export interface UsageSummary { day: string; month: string; today: TokenTotals; month_total: TokenTotals;
  daily_token_cap: number | null; cap_reached: boolean; requests_limit: number }
// GET /api/books/{id}/estimate: a rough estimate (is_estimate) of what processing a book costs on a paid provider.
export interface ProcessEstimate { provider: ProviderId; model: string | null; chunks: number; input_tokens: number;
  output_tokens: number; estimated_cost_usd: number | null; needs_confirm: boolean; is_estimate: true }
export interface UsageToday { day: string; requests: number; limit: number; models: Record<string, number>;
  provider?: ProviderId; tokens?: UsageSummary; gemini_paid?: boolean }
export interface NewInvite { code: string; link: string; expires_at: string }
// A world's reading position: where the reader is, so the graph, panels and answers stay within what they have read.
// `order` is each book against the one being read: "before"/"after" in its series, "unknown" (it has its own read toggle).
/** A story chapter of a book: `seq` is the raw section a position stores, `number` the chapter as the reader counts it
 *  (front and back matter are not counted), `heading` its name when the book gives one. */
export interface PositionSection { seq: number; number: number; heading: string | null }
export interface PositionBook { id: string; title: string; series: string | null; series_index: number | null; chapters: number;
  order: "current" | "before" | "after" | "unknown"; sections?: PositionSection[] }
export interface ReadingPositionData { world: WorldRef; enabled: boolean; book_id: string | null; chapter: number;
  read_books: string[]; books: PositionBook[] }
export interface ReadingPositionBody { enabled: boolean; book_id: string | null; chapter: number; read_books: string[] }

// Server-side history (release step 4): each chat with its messages, a question and its answer each (follow-ups append).
// `answer` is what the stream showed: the result and its other events, or a marker when it was too large to keep.
export interface StoredAnswer { result?: SearchResult; steps?: AskEvent[]; truncated?: boolean; bytes?: number }
// `standalone`: a follow-up's question as the server understood it (null when it was answered as typed).
export interface ChatMessage { id: number; role: string; question: string; standalone?: string | null; depth: string | null; world: string | null;
  request_id: string | null; created_at: string; answer?: StoredAnswer;
  summary?: { answer_style: string | null; mode: string | null; deep: boolean; truncated: boolean } }
export interface ChatSummary { id: string; title: string; world: string | null; client_id: string | null; created_at: string;
  updated_at: string; latest?: ChatMessage }
export interface ChatList { chats: ChatSummary[]; next_offset: number | null }
export interface ChatDetail { chat: ChatSummary; messages: ChatMessage[] }
export interface NewChatBody { client_id?: string; title?: string; world?: string; created_at?: string;
  messages?: { question: string; answer?: StoredAnswer; depth?: string; world?: string }[] }
// Reported answers and their traces (admin only).
export interface AdminReport { id: number; request_id: string; note: string | null; created_at: string; status: "open" | "resolved";
  user_id: string; email: string; question: string | null; trace: boolean }
export interface TraceCall { stage: string; model: string | null; prompt: string | null; reply: string | null; latency_ms: number | null;
  ok: boolean | null; error: string | null; attempts?: { model: string; ok: boolean; latency_ms: number; error: string | null }[] }
export interface TraceRecord { request_id: string; user_id: string; created_at: string; question: string; world: string | null; depth: string | null;
  decomposition: Record<string, unknown> | null; steps: unknown; calls: TraceCall[] | { truncated: true; bytes: number } | null;
  answer: { answer?: string } | null; error: string | null; timings: Record<string, unknown> | null; model_usage: Record<string, unknown> | null }

// POST /api/books/upload (lore/web/upload.py). `processing` says what happened next: running in this process (local
// mode), queued for the worker (multi mode), waiting for the Gemini allowance to reset, or added while another book runs.
export type UploadProcessing =
  | { state: "processing" } | { state: "queued"; job: number } | { state: "busy" }
  // reason "token_cap": the user's own daily token cap (a billed provider), not Gemini's allowance.
  | { state: "waiting"; resumable_at: string; reason?: "token_cap" }
  // A paid provider (Claude, OpenAI) and an estimate above the threshold: nothing started; Continue confirms.
  | { state: "needs_confirm"; estimate: ProcessEstimate };
export interface UploadResult { book_id: string; title: string; world: WorldRef | null; filename: string; processing: UploadProcessing }
// An upload refusal; `book_id` comes with already_added (the book that is already in the library).
export interface UploadError { error: ApiError; book_id?: string }

// One admin action (release step 6): who did what to whom.
export interface AdminEvent { id: number; at: string; admin_id: string | null; admin: string | null; action: string; target_user_id: string | null; detail: Record<string, unknown> | null }
