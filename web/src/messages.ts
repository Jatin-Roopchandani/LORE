import type { ApiError, Depth, StepName } from "./types";

// `settings`: a Settings tab that fixes it (a link to `#settings/<tab>` is shown under the message).
export interface Msg { text: string; retry?: boolean; command?: string; settings?: "providers" | "usage" }
export const OLLAMA_START = "CUDA_VISIBLE_DEVICES=-1 OLLAMA_VULKAN=0 ~/.local/ollama/bin/ollama serve";

export const STEP_LABEL: Record<StepName, string> = {
  split: "Splitting the question", seeds: "Finding starting entities", search: "Searching the graph",
  rank: "Ranking passages", write: "Writing the answer", compose: "Writing the answer in prose", check: "Checking claims against passages",
};

export const STAGE_LABEL: Record<string, string> = {
  chunk: "Splitting into passages", extract: "Reading passages", load: "Building the graph",
  remove: "Taking it out of the graph", reassign: "Changing its world",
  disambiguate: "Merging duplicate entities", beats: "Finding story beats", codex: "Writing entity profiles", ready: "Finishing up",
};

// Deep answers: the Deep dive toggle, the sectioned answer and its steps.
export const DEEP = {
  toggleLabel: "Deep dive",
  options: { auto: "Auto", deep: "On", focused: "Off" } as Record<Depth, string>,
  toggleHint: {
    auto: "LORE decides whether a question needs a long, sectioned answer.",
    deep: "Every question gets a long, sectioned answer.",
    focused: "Every question gets a short answer.",
  } as Record<Depth, string>,
  costNote: "A deep answer can take a few dozen requests to your AI provider.",
  contents: "Contents",
  writing: "Writing…",
  claimsOnly: "Showing the checked facts; the written section didn't pass verification.",
  // An interpretive answer's short answer: the block at the top, one sentence per interpretive part.
  shortAnswer: "Short answer",
  // The one muted line that ends an answer with sections that failed or found nothing (they are not shown).
  unwritten: (n: number) => `${n} ${n === 1 ? "section" : "sections"} couldn't be written`,
  skipped: { budget: "Not written: the request limit for one answer was reached.",
    reserve: "Not written: the rest of today's allowance is kept for answering other questions." },
  requests: "Requests used",
  requestCount: (n: number) => `${n} ${n === 1 ? "request" : "requests"}`,
};

export const STEPS_DEEP = {
  plan: "Planning the sections",
  // A recap: the story's span gathered in order (its chapters and story beats) before its sections are written.
  recap: "Gathering the story so far, in order",
  section: (n: number, heading: string | null) => heading ? `Section ${n}: ${heading}` : `Section ${n}`,
  group: (question: string) => question,
  // The steps panel once the answer has landed: one line that opens it.
  summary: (parts: number, sections: number, seconds: number) =>
    ["How this was found", `${parts} ${parts === 1 ? "part" : "parts"}`,
      ...(sections > 0 ? [`${sections} ${sections === 1 ? "section" : "sections"}`] : []), `${seconds} s`].join(" \u00b7 "),
  // The list of the question's parts and, for an interpretive part, the lenses it was looked at through.
  splitLabel: "Question split into",
  split: (parts: number) => `Question split into \u00b7 ${parts} ${parts === 1 ? "part" : "parts"}`,
  lensLabel: "looked at:",
  // The mark of a section that could not be written, or was not written (budget, or nothing found).
  failed: "couldn't be written",
  skipped: "not written",
  lensDetail: (entities: number, relationships: number) =>
    `${entities} ${entities === 1 ? "entity" : "entities"} \u00b7 ${relationships} relationships`,
  partsLabel: "Parts of the question",
};

// The reading position: the header control, its badge and the note an answer carries.
export const POSITION = {
  label: "Reading position",
  switchLabel: "Limit everything to what I have read",
  bookLabel: "The book you are reading",
  chapterLabel: "Last chapter you finished",
  chapterValue: (n: number, max: number, heading?: string | null) =>
    n === 0 ? `Not started (${max} chapters)` : `Ch. ${n} of ${max}${heading ? ` \u00b7 ${heading}` : ""}`,
  bookOption: (title: string, index: number | null) => (index == null ? title : `${title} (book ${index})`),
  readHeading: "Books of unknown order",
  readHint: "LORE never guesses the order of books that are not in one series. Mark the ones you have read.",
  readLabel: (title: string) => `I have read ${title}`,
  badge: (title: string, chapter: number) => `Up to ${title} ch. ${chapter}`,
  chapterShort: (n: number) => (n === 0 ? "start" : `ch. ${n}`),
  noteLabel: "Reading position note",
  summaryHidden: "The summary covers the whole story, so it is hidden while a reading position is on. These are the statements from what you have read.",
  badgeLabel: (title: string, chapter: number, heading?: string | null) =>
    `Reading position on: up to ${title}, chapter ${chapter}${heading ? ` (${heading})` : ""}`,
  chooseWorld: "Choose a world to set a reading position. Positions already set still apply.",
  off: "Everything is shown, spoilers included.",
  on: "The graph, entity panels and answers stay within what you have read.",
  loading: "Loading\u2026",
  failed: "The reading position could not be saved.",
  noBooks: "This world has no books yet.",
};

export const WORLDS = {
  selectLabel: "World", all: "All worlds",
  legendLabel: "Worlds",
  nodeLabel: (name: string, world: string) => `${name} \u00b7 ${world}`,
  newWorld: "New world\u2026", newWorldName: "New world name",
  addWorldLabel: "World for this book",
  addWorldHint: "Books in one world share characters and places; books in different worlds never mix.",
 
  noBooksInWorld: "No books in this world yet.",
  ungrouped: "No world",
  series: (names: string[]) => `Series: ${names.join(", ")}`,
  groupLabel: (name: string) => `World ${name}`,
  moveOpen: "Move to world\u2026",
  moveOpenLabel: (title: string) => `Move ${title} to another world`,
  moveTargetLabel: (title: string) => `World to move ${title} to`,
  moveConfirm: (title: string, world: string, isNew: boolean) =>
    `Move \u201c${title}\u201d to ${isNew ? "a new world named " : "the world "}\u201c${world}\u201d? It is taken out of the graph and loaded into that world from the saved reading, which costs no new extraction. While it moves, questions can't use this book.`,
  moveConfirmButton: "Confirm move", moveCancel: "Cancel",
  moveSame: "That book is already in this world.",
  moveFailed: "Couldn't start the move.",
  rename: "Rename", renameLabel: (name: string) => `Rename world ${name}`,
  renameInput: (name: string) => `New name for world ${name}`,
  save: "Save", cancel: "Cancel",
  delete: "Delete", deleteLabel: (name: string) => `Delete world ${name}`,
  deleteConfirm: (name: string) => `Delete the empty world \u201c${name}\u201d?`,
  deleteConfirmButton: "Delete world", deleteFailed: "Couldn't delete the world.",
  changeFailed: "That change didn't go through.",
};

// A step's time estimate, rounded on purpose ("~5 min", "~1 h"). null: nothing measured yet, so no invented precision.
function duration(seconds: number | null): string {
  if (seconds == null) return "a few minutes";
  if (seconds < 60) return "under a minute";
  const min = seconds / 60;
  if (min < 10) return `~${Math.max(1, Math.round(min))} min`;
  if (min < 55) return `~${Math.round(min / 5) * 5} min`;
  const h = Math.round((seconds / 3600) * 2) / 2;
  return `~${h % 1 ? h.toFixed(1) : h.toFixed(0)} h`;
}

export const LIBRARY = {
  open: "Books", manage: "Manage library\u2026", title: "Library", close: "Close", loading: "Loading the library…", empty: "No books yet.",
  unreachable: "LORE isn't reachable. Check that `lore ui` is still running.",
  ready: "Ready", paused: "Paused", failed: "Failed", unprocessed: "Not processed yet",
  stage: (stage: string) => STAGE_LABEL[stage] ?? stage,
  // A running job's stage and the provider it runs on: "Reading passages · Claude".
  stageOn: (stage: string, provider: string) => `${STAGE_LABEL[stage] ?? stage} · ${provider}`,
  progress: (done: number, total: number) => `${done.toLocaleString("en")} / ${total.toLocaleString("en")}`,
  // A step that can't count its work yet (moving a book out of the graph, a load before it knows its mentions).
  slowStep: "This step can take a while on CPU.",
  // The step list under a running book: "Step 4 of 6 · Writing entity profiles · ~50 min left".
  stepOf: (step: number, of: number, label: string, left: string | null) =>
    `Step ${step} of ${of} \u00b7 ${label}${left ? ` \u00b7 ${left}` : ""}`,
  stepsLabel: "All steps",
  stepDone: "done",
  stepCount: (done: number, total: number) => `${done.toLocaleString("en")} / ${total.toLocaleString("en")}`,
  duration,
  // The whole: the known part, and "plus a few minutes" when some step has no estimate. null: nothing known.
  left: (seconds: number | null, unknown: boolean) =>
    seconds == null ? null : `${duration(seconds)} left${unknown ? " (plus a few minutes)" : ""}`,
  progressLabel: (stage: string) => `${STAGE_LABEL[stage] ?? stage} progress`,
  continuesAfter: (time: string, left: string) => `Continues after ${time} (your time) \u2014 in ${left}`,
  inHM: (mins: number) => `${Math.floor(mins / 60)}h ${mins % 60}m`,
  continue: "Continue", retry: "Retry", process: "Process",
  action: (verb: string, title: string) => `${verb} ${title}`,
  pausedDefault: "Paused until the daily allowance resets.",
  pausedKey: "Paused: your AI provider's key needs attention.",
  failedDefault: "Processing failed.",
  startFailed: "Couldn't start processing.",
  gemini: (used: number, limit: number, reserve: number, internals: boolean) =>
    `${internals ? "Gemini requests" : "Question allowance"} today: ${used} of ${limit} used \u00b7 ${reserve} kept for answering questions`,
  // Claude, OpenAI or a paid Gemini plan: tokens instead of the free tier's requests. `tokens` null: not counted (local).
  metered: (provider: string, tokens: number | null, usd: number | null, cap: number | null) =>
    tokens == null ? `${provider} \u00b7 billed to your own account`
      : `${provider} \u00b7 today: ${tokens.toLocaleString("en")} tokens`
        + (usd != null ? ` (about $${usd > 0 && usd < 0.01 ? "0.01" : usd.toFixed(2)})` : "")
        + (cap ? ` \u00b7 daily cap ${cap.toLocaleString("en")}` : ""),
};

// A graph link's label (graphView.ts): each relationship with its count, the rest as "+k", and both directions.
export const EDGE_LABEL = {
  one: (name: string, n: number) => (n > 1 ? `${name} ×${n}` : name),
  top: (named: string, rest: number) => named + (rest > 0 ? ` +${rest}` : ""),
  mutual: (name: string, there: number, back: number) => `${name} ×${there} · ×${back}`,
  ways: (there: string, back: string) => `${there} / ${back}`,
};

export const UI = {
  askPlaceholder: "Ask about your books…",
  noBooks: "Pick at least one book to see its part of the graph.",
  refusal: "The books don't contain enough to answer that. The graph shows where LORE looked.",
  evidence: (n: number) => `Evidence (${n} ${n === 1 ? "claim" : "claims"})`,
  unanswered: "The passages found don't answer:",
  readingNote: "A reading of the cited facts, not a direct statement from the book.",
  counterNote: "The strongest case for the other side, drawn from the cited facts: an interpretation, not a statement from the book.",
  limitNote: "What the passages found can't settle.",
  // The label above a run of interpretive sentences (readings, the other side, and what the books leave open).
  kindLabel: { reading: "LORE's reading", counter: "The other side", limit: "What the books don't settle" },
  proseFallback: "Showing the checked facts; the written answer didn't pass verification.",
  partial: (parts: string[]) => `Not found in the books: ${parts.join("; ")}`,
  showingEdges: (shown: number, total: number) => `Showing ${shown} of ${total} relationships`,
  graphIdle: "Evidence graph",
  overviewStage: "Library overview · your most-connected characters, places and groups",
  overviewHint: "Ask a question to see the part of the graph LORE searched.",
  emptyIntro: "Ask a question about your books. LORE answers with quotes from the text and shows the graph it searched.",
  examplesLabel: "Try one of these",
  legendStart: "Starting entity",
  legendPart: (n: number) => `Part ${n}`,
  legendKinds: [["--k-character", "Character"], ["--k-place", "Place"], ["--k-faction", "Group"], ["--k-object", "Object"],
    ["--k-concept", "Concept"], ["--k-event", "Event"], ["--k-other", "Other"]] as const,
  legendCited: "Cited",
  fitGraph: "Fit to view",
  answeredIn: (seconds: number) => `Answered in ${seconds.toFixed(1)}s · cited relationships glow`,
  ingestHint: "Run this in a terminal on the machine running LORE:",
  ingestWorldNote: "The command asks you to confirm before it creates a new world.",
  mcpNotRunning: "HTTP server not running",
  mcpRunning: "HTTP server running",
  wrongFileType: (name: string) => `${name} is not an EPUB or PDF. Choose a .epub or .pdf file.`,
};

// Adding a book in the browser (AddBookDialog, web/src/upload.ts).
export const UPLOAD = {
  title: "Add a book",
  drop: "Drop an EPUB or PDF here", dropOr: "or click to choose a file",
  limits: "EPUB or PDF, up to 50 MB. Add only books you own.",
  chosen: (name: string, bytes: number) => `${name} \u00b7 ${formatSize(bytes)}`,
  upload: "Upload", uploading: "Uploading\u2026", checking: "Checking the book\u2026",
  cancel: "Cancel", close: "Close", another: "Add another book",
  progressLabel: "Upload progress",
  progress: (loaded: number, total: number) => `${formatSize(loaded)} of ${formatSize(total)}`,
  intro: "LORE splits the book into passages, then extracts characters, places and relationships.",
  started: "Processing started",
  waitingTitle: "Book added",
  next: {
    processing: "LORE is reading the book now. Its progress shows in the Library.",
    queued: "The book is in the queue and starts shortly. Its progress shows in the Library.",
    busy: "Another book is being processed. When it is done, press Continue on this one in the Library.",
    waiting: (time: string) => `Today's allowance is used up, so the book waits. Press Continue in the Library after ${time} (your time).`,
    waitingCap: (time: string) => `Your daily token cap is reached, so the book waits. Press Continue in the Library after ${time} (your time).`,
  },
  addedTo: (title: string, world: string | null) => world ? `\u201c${title}\u201d is in the world \u201c${world}\u201d.` : `\u201c${title}\u201d is in your library.`,
  openLibrary: "Open it in the Library",
  startFailed: "Processing couldn't start. Press Continue on the book in the Library.",
  terminal: "Or add it from the terminal",
  tooBig: (name: string) => `${name} is larger than 50 MB.`,
};

const UPLOAD_ERRORS: Record<string, string> = {
  too_large: "That file is larger than 50 MB.",
  not_a_book: "That file isn't a readable EPUB or PDF. A scanned PDF with no text can't be read either.",
  unsafe_file: "That file's structure looks unsafe, so it was refused. Try a fresh copy of the book.",
  encrypted: "That book is locked (DRM or a password). Add a copy without protection.",
  too_complex: "That book is too large or complex to read safely. Try a different copy.",
  too_many_pages: "That PDF has too many pages to process.",
  too_many_books: "Your library is full. Remove a book before adding another.",
  daily_limit: "You've added as many books as you can today. Try again tomorrow.",
  rate_limited: "Too many uploads in a short time. Wait an hour and try again.",
  already_added: "That book is already in your library.",
  busy: "Another upload is still in progress. Wait for it to finish.",
  key_required: "Connect your AI provider before adding books.",
  key_invalid: "Your AI provider refused your API key. Replace it to carry on.",
  bad_upload: "The upload didn't arrive whole. Try again.",
  unknown_world: "That world no longer exists. Choose another.",
  invalid_world: "That world name can't be used. Choose another.",
  unreachable: "LORE isn't reachable. Check your connection and try again.",
  forbidden: "This page isn't allowed to upload. Reload it and try again.",
  unauthorized: "Your session has ended. Sign in again.",
  aborted: "The upload was cancelled.",
};

export function uploadError(error: ApiError): string {
  return UPLOAD_ERRORS[error.code] ?? "The upload didn't go through. Try again.";
}

function formatSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Example questions for the empty chat, filled with the library's most-connected entities (see examples.ts).
export const EXAMPLES = {
  connected: (a: string, b: string) => `How is ${a} connected to ${b}?`,
  about: (a: string) => `Tell me about ${a}`,
  place: (p: string) => `What happens in ${p}?`,
  group: (g: string) => `Who belongs to ${g}?`,
};

// The graph explorer: the graph pane's modes, the entity and edge panels, search, paths and filters.
const nth = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
const pretty = (p: string) => p.toLowerCase().replace(/_/g, " ");
export const EXPLORE = {
  modeLabel: "Graph view", modeAnswer: "This answer", modeLibrary: "Library",
  panelLabel: "Entity", edgeLabel: "Relationship", close: "Close", back: "Back",
  expand: "Expand neighbours", expanding: "Expanding\u2026", expandFailed: "Couldn't load the neighbours.",
  loading: "Loading\u2026", failed: "Couldn't load that.",
  tabsLabel: "Entity details",
  tabs: { profile: "Profile", timeline: "Timeline", relations: "Relationships", ask: "Ask" },
  startingEntity: "starting entity",
  // Profile
  noProfile: "No profile yet for this entity.",
  noProfileHint: "Showing the facts found in the graph instead.",
  rawFactsTitle: "Facts in the graph",
  noFacts: "No facts found for this entity.",
  counts: (c: { beats: number; relations: number; chapters: number }) =>
    [nth(c.beats, "event", "events"), nth(c.relations, "connection", "connections"), nth(c.chapters, "chapter", "chapters")].join(" \u00b7 "),
  aspect: (name: string) => name.charAt(0).toUpperCase() + name.slice(1).replace(/_/g, " "),
  // Passages (quote chips)
  chip: (book: string, chapter: number) => `${book} ch.${chapter}`,
  chipLabel: (book: string, chapter: number) => `Open the passage from ${book}, chapter ${chapter}`,
  passageLoading: "Loading the passage\u2026", passageFailed: "Couldn't load the passage.",
  passageSource: (title: string | null, chapter: number) => `${title ?? "Book"} \u00b7 chapter ${chapter}`,
  // Timeline
  noBeats: "No story events recorded for this entity.",
  chapter: (n: number) => `Chapter ${n}`,
  beatKind: (kind: string) => kind.charAt(0).toUpperCase() + kind.slice(1).replace(/_/g, " "),
  withOthers: "With",
  openEntity: (name: string) => `Open ${name}`,
  // Relationships
  noRelations: "No relationships recorded for this entity.",
  predicateLabel: (p: { predicate: string; count: number; direction: "out" | "in" }) =>
    `${p.direction === "in" ? "\u2190 " : ""}${pretty(p.predicate)}${p.count > 1 ? ` \u00d7${p.count}` : ""}${p.direction === "out" ? " \u2192" : ""}`,
  chapterSpan: (a: string, b: string) => (a === b ? a : `${a} \u2013 ${b}`),
  showEdge: (a: string, b: string) => `Show how ${a} and ${b} are connected`,
  // Ask
  askIntro: "Ask LORE about this entity. Each question is answered from the books.",
  askCost: "Uses your AI provider: requests from Gemini's free allowance, or tokens on a paid plan.",
  askCostDeep: "Uses your AI provider: a deep answer can take a few dozen requests.",
  askBusy: "LORE is still answering the previous question.",
  deepDive: (name: string) => `Deep dive on ${name}`,
  change: (name: string) => `How does ${name} change across the story?`,
  role: (name: string) => `What role does ${name} play?`,
  relationship: (a: string, b: string) => `How does the relationship between ${a} and ${b} develop?`,
  // Edge view
  edgeBetween: (a: string, b: string) => `${a} and ${b}`,
  noEdges: "No relationships recorded between these two.",
  edgeStatement: (subject: string, predicate: string, object: string) => `${subject} ${pretty(predicate)} ${object}`,
  // Search
  searchLabel: "Find an entity", searchPlaceholder: "Find an entity\u2026", searchResults: "Matching entities",
  noMatches: "No entity matches that.", searching: "Searching\u2026",
  matchedAlias: (alias: string) => `also called ${alias}`,
  // Connect
  connect: "Connect to\u2026", connectLabel: "Connect this entity to another", connectPick: "Find the second entity",
  connectCancel: "Cancel", connecting: "Looking for a connection\u2026",
  pathTitle: (from: string, to: string) => `${from} to ${to}`,
  pathHops: (n: number) => nth(n, "step", "steps"),
  pathNone: (from: string, to: string) => `No connection found between ${from} and ${to} within 4 steps.`,
  pathTimedOut: "The search took too long. Try a closer pair.",
  pathFailed: "Couldn't look for a connection.",
  pathSame: "That is the same entity.",
  clearPath: "Clear path", pathLabel: "Connection",
  hop: (n: number) => `Step ${n}`,
  // Filters
  filters: "Filters", filtersLabel: "Graph filters", kindsLabel: "Show kinds", booksLabel: "Show books",
  filtersActive: (n: number) => `Filters (${n} hidden)`,
  resetFilters: "Show everything",
  kindLabel: { Character: "Character", Place: "Place", Faction: "Group", Object: "Object", Concept: "Concept", Event: "Event" } as Record<string, string>,
  filteredOut: (nodes: number) => `${nth(nodes, "entity", "entities")} hidden by filters`,
};

// The history sidebar: earlier questions, grouped by when they were asked.
export const HISTORY = {
  title: "History", open: "History", close: "Close history",
  toggleLabel: (open: boolean) => (open ? "Hide history" : "Show history"),
  newQuestion: "New chat", newQuestionHint: "Start a new conversation: clear the answer, show the library and focus the ask box.",
  search: "Search history", searchPlaceholder: "Search questions\u2026",
  groups: { today: "Today", yesterday: "Yesterday", week: "This week", earlier: "Earlier" },
  empty: "Questions you ask will be listed here.",
  noMatch: (q: string) => `No question matches \u201c${q}\u201d.`,
  yesterday: "Yesterday",
  deep: "Deep answer",
  quickHint: (mac: boolean) => (mac ? "\u2318K" : "Ctrl K"), quickHintLabel: (mac: boolean) => `Quick switcher: press ${mac ? "Command K" : "Control K"} anywhere`,
  allWorlds: "All worlds",
  delete: (q: string) => `Delete: ${q}`, deleteShort: "\u00d7",
  clear: "Clear history",
  clearConfirm: (n: number) => `Clear all ${n} saved ${n === 1 ? "question" : "questions"}? This can't be undone.`,
  clearConfirmLabel: "Confirm clearing history", clearYes: "Clear", clearNo: "Keep",
  entries: "Earlier questions",
  // Server-side history (release step 4): the one-time import of chats saved only in this browser.
  importOffer: (n: number) => `Import your ${n} saved chat${n === 1 ? "" : "s"} from this browser`,
  importYes: "Import",
  importNo: "Not now",
};

// A chat: its earlier questions and answers stacked above the latest, and follow-ups asked in it.
export const CHAT = {
  followupPlaceholder: "Ask a follow-up\u2026",
  newChat: "New chat",
  newChatHint: "Start a fresh conversation: the next question won't follow up this one.",
  understoodAs: "Understood as:",
  showEarlier: (n: number) => `Show ${n} earlier ${n === 1 ? "question" : "questions"}`,
  expand: (q: string) => `Show the answer to: ${q}`,
  collapse: (q: string) => `Hide the answer to: ${q}`,
  earlierLabel: "Earlier in this chat",
  noAnswer: "No answer was kept for this question.",
};

// The Ctrl+K quick switcher: jump back to a past question, or ask a new one.
export const QUICK = {
  title: "Quick switcher", search: "Search past questions or type a new one", placeholder: "Search or ask\u2026",
  list: "Matching questions", ask: (q: string) => `Ask: ${q}`, empty: "No past question matches.",
};

function until(resetsAt: string | undefined, now: Date): string {
  if (!resetsAt) return "";
  const mins = Math.max(0, Math.round((new Date(resetsAt).getTime() - now.getTime()) / 60000));
  return ` (in about ${Math.floor(mins / 60)}h ${mins % 60}m)`;
}

export function errorMessage(error: ApiError, ctx: { showInternals: boolean; resetsAt?: string; now?: Date }): Msg {
  const i = ctx.showInternals;
  switch (error.code) {
    case "budget_exhausted":
      return { text: `Today's ${i ? "Gemini requests are" : "question allowance is"} used up. `
        + `${i ? "They reset" : "It resets"} at midnight Pacific${until(ctx.resetsAt, ctx.now ?? new Date())}.`, settings: "usage" };
    case "upstream_unavailable":
      // The backend has no separate Ollama code: an outage is upstream_unavailable naming Ollama.
      if (/ollama/i.test(error.message))
        return i ? { text: "LORE can't reach Ollama. Start it, then ask again.", command: OLLAMA_START, retry: true }
          : { text: "LORE's search service isn't running. Start it, then ask again.", retry: true };
      return { text: `${i ? "Gemini" : "The answer service"} is busy right now. Try again in a minute.`, retry: true };
    case "busy":
      return { text: "LORE is still answering the previous question." };
    case "interrupted":
      return { text: "The connection dropped before the answer finished.", retry: true };
    case "unreachable":
      return { text: i ? "LORE isn't reachable. Check that `lore ui` is still running." : "LORE isn't reachable right now. Check your connection and try again.", retry: true };
    case "forbidden":
      return { text: i ? "This page isn't allowed to talk to LORE. Open it from the address lore ui printed." : "That isn't allowed. Reload the page and try again." };
    case "rate_limited":
      return { text: error.message || "Too many requests. Please wait a moment and try again.", retry: true };
    case "no_graph":
      return { text: error.message || "There's no graph yet. Process a book first." };
    case "bad_request":
      return { text: error.message || "That question couldn't be sent. Questions are 1 to 2,000 characters." };
    case "key_required":
      return { text: "Connect your AI provider to ask questions.", settings: "providers" };
    case "key_invalid":
      return { text: "Your AI provider refused your API key. Replace it to carry on.", settings: "providers" };
    case "model_refused":
      return { text: "The AI model declined to answer this. Try asking it another way." };
    case "too_large":
      return { text: "This answer was too large to keep in your history." };
    case "chat_not_found":
      return { text: "This chat is no longer in your history. Start a new chat to ask again." };
    default: {
      // A friendly server error quotes its reference already; otherwise add it when there is one.
      const text = error.message || "Something went wrong.";
      return { text: text.includes("Reference:") ? text : text + reference(error.request_id), retry: true };
    }
  }
}

// Theme picker (Settings, Appearance).
export const THEME = {
  system: "System",
  systemHint: "Follows your device",
};

// Sign-in, the waitlist and the public pages (multi-user mode).
export const AUTH = {
  brand: "LORE",
  brandTag: "Reader",
  homeLabel: "LORE home",
  signIn: "Sign in",
  requestInvite: "Request an invite",
  // The night brand panel beside every sign-in page (a phone shows only the logo and the first line).
  brandLine: "Ask your books anything.",
  brandLineGlow: "Every answer checked against the page.",
  graphBookOfLife: "The Book of Life",
  signInTitle: "Welcome back",
  signInLead: "Sign in to pick up your reading where you left off.",
  invitedTitle: "You've been invited",
  invited: "Sign in any way below to create your account.",
  withGoogle: "Continue with Google",
  withGithub: "Continue with GitHub",
  or: "or continue with email",
  emailLabel: "Email address",
  emailPlaceholder: "you@example.com",
  sendLink: "Email me a sign-in link",
  sending: "Sending link…",
  // The sent state. It reads the same whoever asks, so it never says whether an address has an account.
  checkInbox: "Check your inbox",
  sentBefore: "We sent a sign-in link to ",
  sentAfter: ". It expires in 15 minutes.",
  sentHint: "Not there? Check your spam folder. A link only goes to an address with an account or an invite.",
  didntGet: "Didn't get it?",
  resendIn: (s: number) => `Resend in ${s}s`,
  resend: "Resend link",
  resent: "A new link is on its way.",
  differentEmail: "Use a different email",
  agreeBefore: "By continuing, you agree to LORE's ",
  agreeAnd: " and ",
  agreeAfter: ".",
  termsLink: "Terms",
  privacyLink: "Privacy Policy",
  newHere: "New to LORE?",
  emailMissing: "Enter your email address.",
  invalidEmail: "That doesn't look like an email address.",
  rateLimited: "Too many requests. Please try again in a little while.",
  sendFailed: "The request couldn't be sent. Check your connection and try again.",
  errors: {
    oauth_failed: "Sign-in with that provider didn't complete. Please try again.",
    oauth_denied: "Sign-in was cancelled.",
    email_unverified: "That provider didn't give us a verified email address, so we can't sign you in with it.",
    link_invalid: "That sign-in link has expired or was already used. Ask for a new one.",
    account_disabled: "This account is not active.",
  } as Record<string, string>,
  waitlistTitle: "Request an invite",
  waitlistLead: "LORE is invite-only while it's in beta. Leave your email and we'll write when there's a place.",
  waitlistDone: "You're on the list",
  haveAccount: "Already invited?",
  waitlistUnknown: "We don't have an account for that sign-in yet. LORE is invite-only while it's in beta, so request an invite below.",
  readingLabel: "What are you reading? (optional)",
  consentLabel: "I agree that LORE may email me about my invite request.",
  submit: "Request an invite",
  submitting: "Sending…",
  thanks: "Thanks. We'll email you when there's a place for you.",
  signOut: "Sign out",
  account: "Account",
  admin: "Admin",
  back: "Back",
  privacy: "Privacy",
  terms: "Terms",
  notFound: "There's nothing at this address.",
};

// Connecting an AI provider (multi-user mode): the onboarding screen ("Connect your AI provider") and the provider and
// usage panels the Settings popup shows. Prices are US dollars per million tokens, input / output.
export const PROVIDERS = {
  connectTitle: "Connect your AI provider",
  replaceTitle: "Your AI provider needs a new key",
  lead: "LORE reads your books and answers your questions with an AI model, under your own key. Pick one provider to start; you can add another or switch later in Settings.",
  rejected: "Your provider refused your saved key. It may have been deleted or expired. Paste a new one to carry on; your paused books continue once it works.",
  privacy: "The passages LORE finds and your questions are sent to the provider you choose, under your own key. Your key is stored encrypted and is never shown in full again.",
  names: { gemini: "Gemini", anthropic: "Claude", openai: "OpenAI" } as Record<string, string>,
  makers: { gemini: "Google", anthropic: "Anthropic", openai: "OpenAI" } as Record<string, string>,
  getKey: { gemini: "Get a free key from Google AI Studio", anthropic: "Get a key: console.anthropic.com → API Keys",
    openai: "Get a key: platform.openai.com → API keys" } as Record<string, string>,
  getKeyUrl: { gemini: "https://aistudio.google.com/app/apikey", anthropic: "https://console.anthropic.com/settings/keys",
    openai: "https://platform.openai.com/api-keys" } as Record<string, string>,
  cost: { gemini: "Free tier: about 1,000 requests a day, resetting at midnight Pacific.",
    anthropic: "Billed to your own account by the provider.", openai: "Billed to your own account by the provider." } as Record<string, string>,
  fieldLabel: (name: string) => `${name} API key`,
  placeholder: "Paste your key here",
  save: "Check and save",
  checking: "Checking…",
  errors: {
    key_format: "That doesn't look like a key for this provider. Copy the whole key and paste it again.",
    key_invalid: "The provider didn't accept that key. Check you copied all of it, or create a new one.",
    rate_limited: "Too many tries. Please wait a while and try again.",
    unreachable: "LORE isn't reachable right now. Check your connection and try again.",
    bad_request: "That choice couldn't be saved. Please try again.",
  } as Record<string, string>,
  saveFailed: "That couldn't be saved. Please try again.",
  modelLabel: "Model",
  fastModelLabel: "Model for short steps (optional)",
  fastModelSame: "Same as above",
  fastModelHint: "Searching, splitting questions and checking answers can run on a cheaper model.",
  openaiSuggested: "Suggested: the cheapest general model your key lists. You can pick another.",
  claudeModels: [
    { id: "claude-sonnet-5-5", name: "Claude Sonnet 5.5", price: "$2 in · $10 out per million tokens" },
    { id: "claude-opus-5-5", name: "Claude Opus 5.5", price: "$4 in · $20 out per million tokens" },
    { id: "claude-haiku-5-5", name: "Claude Haiku 5.5", price: "$0.10 in · $0.50 out per million tokens" },
  ],
  priceHeading: "Price",
  priceExplain: "\u201cIn\u201d is what LORE sends the model (passages and your question); \u201cout\u201d is what it writes back. A million tokens is roughly 750,000 words.",
  novelEstimate: "Estimate, not a measurement: processing a typical novel (about 100,000 words) uses roughly 1.2 million tokens, about $5 on Sonnet 5.5, $10 on Opus 5.5 or $0.30 on Haiku 5.5. Each question uses far fewer.",
  saved: "Key saved.",
  startReading: "Start reading",
  continue: "Continue",
  cancel: "Cancel",
  // Settings: connected providers.
  section: "AI providers",
  active: "In use",
  use: (name: string) => `Use ${name}`,
  endingIn: (last4: string) => `Key ending in …${last4}`,
  needsReplacing: "The provider refused this key. Replace it to carry on.",
  unchecked: "Not confirmed yet: it will be checked on your next question.",
  replace: "Replace key",
  remove: "Remove",
  removeConfirm: (name: string) => `Remove your ${name} key? If it is the one in use, LORE switches to another connected provider, or stops until you add one.`,
  removeYes: "Remove key",
  addAnother: "Add another provider",
  // Gemini: the paid-plan switch and the hint.
  geminiPaid: "I'm on a paid Gemini plan",
  geminiPaidHelp: "Removes LORE's free-tier daily limit and shows token usage and estimated cost instead. Google bills you for usage.",
  geminiPaidCost: "Paid plan: Google bills your account for usage. No LORE daily limit.",
  geminiPaidHint: "Your key seems to be on a paid plan. Turn on 'I'm on a paid Gemini plan' to remove LORE's daily limit.",
  dismiss: "Dismiss",
};

// Settings: what the user has used, and their own optional daily token cap (Claude and OpenAI).
export const USAGE = {
  section: "Usage",
  requestsToday: (used: number, limit: number) => `Gemini requests today: ${used.toLocaleString("en")} / about ${limit.toLocaleString("en")}`,
  resets: "The free tier resets at midnight Pacific.",
  tokens: (label: string, tokens: number) => `${label}: ${tokens.toLocaleString("en")} tokens`,
  today: "Today", month: "This month",
  cost: (usd: number) => `about $${usd > 0 && usd < 0.01 ? "0.01" : usd.toFixed(2)} (estimate)`,
  capLabel: "Daily token cap",
  capHint: "Optional. When today's tokens reach it, questions stop and books wait until midnight Pacific.",
  capOff: "Off",
  capSave: "Save cap",
  capClear: "Turn off",
  capReached: "Today's token cap is reached.",
  capInvalid: "Enter a whole number of tokens, or turn the cap off.",
};

// Before a book is processed on a paid provider (Claude, OpenAI).
export const ESTIMATE = {
  text: (tokens: number, usd: number | null) =>
    `Processing this book should use about ${(tokens / 1e6).toFixed(1)} million tokens${usd != null ? `, roughly $${usd.toFixed(2)}` : ""}, billed to your own account. This is an estimate; nothing has started yet.`,
  confirm: "Process it",
};

// The admin page.
export const ADMIN = {
  title: "Admin",
  forbidden: "This page is for administrators.",
  loadFailed: "Couldn't load this. Reload the page to try again.",
  waitlist: "Waitlist",
  noRequests: "No requests yet.",
  columns: { email: "Email", reading: "Reading", requested: "Requested", status: "Status", actions: "" },
  status: { pending: "Pending", invited: "Invited", declined: "Declined" } as Record<string, string>,
  approve: "Approve",
  decline: "Decline",
  approved: (email: string) => `Invitation sent to ${email}.`,
  declined: (email: string) => `Declined ${email}.`,
  actionFailed: "That didn't work. Nothing was changed.",
  invites: "Invites",
  invitesLead: "Make a code to hand to someone directly. It's shown once.",
  days: "Valid for (days)",
  createInvite: "Create invite",
  inviteCode: "Invite code",
  inviteLink: "Sign-in link",
  copy: "Copy",
  copied: "Copied",
  users: "Users",
  userColumns: { email: "Email", name: "Name", role: "Role", status: "Status", joined: "Joined", key: "AI key", today: "Requests today", actions: "" },
  reports: "Reported answers",
  noReports: "No reports yet.",
  reportColumns: { when: "When", email: "Email", question: "Question", note: "Note", status: "Status", actions: "" },
  reportStatus: { open: "Open", resolved: "Resolved" } as Record<string, string>,
  openTrace: "Open trace",
  resolve: "Resolve",
  traceGone: "This trace has expired (traces are kept 30 days).",
  traceTitle: "Trace",
  traceQuestion: "Question",
  traceDecomposition: "Decomposition",
  traceCalls: (n: number) => `Model calls (${n})`,
  traceAnswer: "Answer",
  traceError: "Error",
  tracePrompt: "Prompt",
  traceReply: "Reply",
  traceClose: "Close trace",
  hasKey: "Saved", noKey: "None",
};

// The entity profile's introduction under a reading position (written only from what has been read).
export const PROFILE = {
  introLabel: "Introduction so far",
  introHint: "Written only from what you have read so far.",
};


// "Report this answer" under each answer (release step 4).
export const REPORT = {
  link: "Report this answer",
  title: "Report this answer",
  lead: "Tell us what was wrong, if you like. The report includes this question's record, so we can see how the answer was made.",
  noteLabel: "What was wrong? (optional)",
  send: "Send report",
  sending: "Sending…",
  cancel: "Cancel",
  thanks: "Thanks. Your report was sent.",
  failed: "The report couldn't be sent. Please try again.",
};

// Release step 6: the mailed sign-in link opens this page; only its button spends the link (mail scanners open links too).
export const SIGNIN_LINK = {
  title: "Continue signing in",
  lead: "Press the button to finish signing in to LORE. The link works once.",
  why: "We ask for this one click so that a mail scanner opening the link can't use it up before you do.",
  button: "Continue signing in",
  missing: "This sign-in link is incomplete. Ask for a new one.",
  again: "Ask for a new link",
};

// The consent page an MCP client (claude.ai, Claude Desktop, Claude Code) sends the reader to (/oauth/consent).
export const CONSENT = {
  title: (name: string) => `${name} wants to read your LORE library`,
  titleUnnamed: "An app wants to read your LORE library",
  lead: "If you allow it, it can:",
  can: [
    "Search your books and read the characters, places and connections LORE found in them",
    "Ask LORE questions, which uses your own AI provider key and allowance",
  ],
  cannot: "It can't change your library, and it can't see your account, your settings or your keys. Your reading position still applies.",
  back: (host: string) => `Allowing sends you back to ${host}.`,
  allow: "Allow",
  allowing: "Connecting…",
  cancel: "Cancel",
  later: "You can disconnect it any time in Settings, under MCP connector.",
  loading: "Loading the request…",
  expired: "This connection request has expired or isn't valid. Start connecting again from your app.",
  failed: "That didn't work. Try again.",
};

// Release step 6: deleting one's own account (account menu) and the admin's user controls.
export const ACCOUNT_DELETE = {
  open: "Delete my account",
  title: "Delete your account",
  lead: "This deletes your library (books, graph and history), your saved AI provider keys and your sign-ins. It can't be undone.",
  confirmLabel: (email: string) => `Type ${email} to confirm`,
  confirm: "Delete my account for good",
  deleting: "Deleting…",
  cancel: "Cancel",
  failed: "The account couldn't be deleted. Please try again.",
};

// The Settings popup: one dialog over the app (never a page), opened from the ⚙ button, the account menu, or a
// `#settings/<tab>` link. Every string the popup and its tabs show, except the provider and usage panels' own.
export const SETTINGS = {
  title: "Settings",
  open: "Settings",
  close: "Close settings",
  tabsLabel: "Settings sections",
  tabs: {
    account: "Account", providers: "AI providers", usage: "Usage", appearance: "Appearance", reading: "Reading",
    mcp: "MCP connector", data: "Your data", admin: "Admin",
  } as Record<string, string>,
  leads: {
    account: "Who you are, how you sign in, and where you're signed in.",
    providers: "The AI provider LORE uses for your books and questions, under your own key.",
    usage: "What you've used today and this month.",
    appearance: "How LORE looks on this device.",
    reading: "How LORE opens and answers, on this device.",
    mcp: "Use your LORE library from Claude Desktop, Claude Code or any MCP client.",
    data: "Take your data with you, or delete it.",
    dataLocal: "Your library is on this computer. Here's where, and how to back it up.",
    admin: "Waitlist, invites, users and reports.",
  } as Record<string, string>,
  // Account
  accountMenu: "Account menu",
  name: "Name", email: "Email", noName: "Not set",
  methods: "Sign-in methods",
  methodNames: { google: "Google", github: "GitHub", email: "Email link" } as Record<string, string>,
  linkedSince: (when: string) => `Linked ${when}`,
  justNow: "just now",
  onDate: (date: string) => `on ${date}`,
  emailAlways: (email: string) => `You can always sign in with a link emailed to ${email}.`,
  connect: (name: string) => `Connect ${name}`,
  connectHint: (email: string) => `Sign in with the account that uses ${email}; it is linked to this account by that address.`,
  devices: "Signed-in devices",
  thisDevice: "This device",
  lastActive: (when: string) => `Last active ${when}`,
  signedInAt: (when: string) => `Signed in ${when}`,
  revoke: "Sign out",
  revokeLabel: (device: string) => `Sign out ${device}`,
  revokeOthers: "Sign out of other devices",
  revokedOthers: (n: number) => (n === 1 ? "Signed out of 1 other device." : `Signed out of ${n} other devices.`),
  onlyThisDevice: "You're signed in on this device only.",
  devicesFailed: "Your devices couldn't be loaded. Try again.",
  revokeFailed: "That device couldn't be signed out. Try again.",
  signOutHere: "Sign out of this device",
  // Usage (local mode: the Gemini daily budget)
  geminiToday: (left: number, limit: number) => `Gemini requests left today: ${left.toLocaleString("en")} of ${limit.toLocaleString("en")}`,
  requestsToday: (left: number, limit: number) => `Requests left today: ${left.toLocaleString("en")} of ${limit.toLocaleString("en")}`,
  perModel: (model: string, count: number, limit: number) => `${model}: ${count.toLocaleString("en")} of ${limit.toLocaleString("en")} used`,
  resets: (time: string) => `Resets at midnight Pacific (${time} here).`,
  localModels: "This library uses local models: there is no daily budget.",
  billed: (provider: string) => `This library runs on ${provider}, billed to your own account: LORE keeps no daily request limit.`,
  geminiNoneYet: "No Gemini requests yet today.",
  noneYet: "No requests yet today.",
  usageUnavailable: "Usage isn't available right now.",
  // Appearance
  theme: "Theme",
  themeBasics: "System, light and dark",
  themeGroups: { dark: "Dark themes", light: "Light themes" } as Record<string, string>,
  motion: "Motion",
  motionOptions: { system: "Follow my device", reduce: "Reduce motion" } as Record<string, string>,
  motionHint: "Reduce motion stops animations and the answer's streaming effect, whatever your device says.",
  // Reading
  world: "World to open in",
  worldHint: "The same choice as the World menu in the top bar.",
  allWorlds: "All worlds",
  position: "Reading position",
  positionHint: "Hide what comes after the chapter you've reached. It's set per world, from the bookmark button in the top bar.",
  positionOpen: "Set reading position",
  positionNeedsWorld: "Choose one world first: a reading position belongs to a world.",
  steps: "Show how this was found",
  stepsHint: "Open the steps panel under each answer by default.",
  depth: "Default answer depth",
  depthHint: "The same choice as Deep dive under the question box.",
  depthOptions: { auto: "Auto", deep: "Deep on", focused: "Deep off" } as Record<string, string>,
  internals: "Show internals",
  internalsHint: "Model names, request counts and technical errors. This browser only; lore.toml's [web] show_internals is the default.",
  // MCP connector
  mcpWhat: "LORE runs an MCP server on this computer. Connect it and Claude (or any MCP client) can search your library, look up characters and walk the graph, with your reading position applied.",
  mcpWhatHosted: "An MCP connector lets Claude Desktop, Claude Code or any MCP client search your library, look up characters and walk the graph, with your reading position applied.",
  mcpDesktop: "Claude Desktop",
  mcpDesktopHint: "Add this to claude_desktop_config.json (Settings, Developer, Edit Config), then restart Claude Desktop.",
  mcpCode: "Claude Code",
  mcpCodeHint: "Run once in a terminal. Claude Code starts LORE itself when it needs it.",
  mcpHttp: "HTTP",
  mcpHttpHint: "For clients that connect over HTTP. Start the server, then point the client at the address. Loopback only.",
  mcpHttpClaude: "Then, for Claude Code:",
  mcpHttpCommand: "the HTTP start command", mcpHttpAddress: "the HTTP address", mcpCodeHttp: "the Claude Code HTTP command",
  mcpCheck: "Check",
  mcpChecking: "Checking…",
  mcpOk: "The MCP server starts here.",
  mcpFailed: "The MCP server can't start here.",
  mcpCheckFailed: "The check couldn't run. Is LORE still running?",
  mcpNoLibrary: "No library was found at this data directory yet.",
  mcpTools: (n: number) => `The ${n} tools it exposes`,
  mcpLoading: "Loading the connection details…",
  // MCP connector, hosted accounts
  mcpAddress: "Server address",
  mcpAddressLabel: "the MCP server address",
  mcpTokens: "Access tokens",
  mcpTokensHint: "A token lets one MCP client read your library: search it, look up characters and ask questions on your own AI provider key. It can't change anything or see your account settings. Make one per app, and revoke any you no longer use.",
  mcpTokenName: "Token name",
  mcpTokenNamePlaceholder: "e.g. Claude Code on my laptop",
  mcpCreate: "Create token",
  mcpCreating: "Creating…",
  mcpTokenOnce: "Copy this token now. It won't be shown again.",
  mcpTokenLabel: "the new token",
  mcpTokenNone: "No tokens yet.",
  mcpTokenMax: (n: number) => `You have ${n} tokens, the most allowed. Revoke one to make another.`,
  mcpTokenWhen: (created: string, used: string | null) => `Created ${created}, ${used ? `last used ${used}` : "never used"}`,
  mcpTokenPrefix: (prefix: string) => `${prefix}…`,
  mcpTokenRevoke: "Revoke",
  mcpTokenRevokeLabel: (name: string) => `Revoke ${name}`,
  mcpTokenFailed: "The token couldn't be made. Try again.",
  mcpTokenNameNeeded: "Give the token a name first.",
  mcpTokensFailed: "Your tokens couldn't be loaded. Try again.",
  mcpRevokeFailed: "That token couldn't be revoked. Try again.",
  mcpSetup: "Connect a client",
  mcpSetupHint: "Each snippet has the server address filled in. Create a token first and they include it; otherwise put your token where it says <your token>.",
  mcpSetupCode: "Claude Code",
  mcpSetupCodeHint: "Run once in a terminal.",
  mcpSetupJson: "Any MCP client (JSON config)",
  mcpSetupJsonHint: "For clients configured with an mcpServers file, such as a project's .mcp.json.",
  mcpSetupCursor: "Cursor",
  mcpSetupCursorHint: "Add this to ~/.cursor/mcp.json (or the project's .cursor/mcp.json).",
  mcpTokenPlaceholder: "<your token>",
  mcpCheckHosted: "LORE's MCP server is ready.",
  mcpClaudeAi: "claude.ai and Claude Desktop",
  mcpClaudeAiSteps: [
    "In claude.ai, open Settings, then Connectors, and choose Add custom connector.",
    "Name it LORE and paste the server address above. Leave the advanced settings empty.",
    "Claude opens a LORE page: sign in if asked, then choose Allow.",
    "Claude Desktop uses the same connectors as your claude.ai account.",
  ],
  mcpClaudeAiOff: "Connecting from claude.ai needs this LORE server on an https address. Access tokens work everywhere else.",
  mcpConnections: "Connected apps",
  mcpConnectionsHint: "Apps you allowed from a LORE consent page. Disconnecting one ends its access at once.",
  mcpConnectionsNone: "No apps connected yet.",
  mcpConnectionWhen: (created: string, used: string | null) => `Connected ${created}, ${used ? `last used ${used}` : "not used yet"}`,
  mcpDisconnect: "Disconnect",
  mcpDisconnectLabel: (name: string) => `Disconnect ${name}`,
  mcpConnectionsFailed: "Your connected apps couldn't be loaded. Try again.",
  mcpDisconnectFailed: "That app couldn't be disconnected. Try again.",
  copy: "Copy", copied: "Copied",
  // Your data
  download: "Download my data",
  downloadHint: "A zip of your chats (every question and answer) and your books' details: titles, authors, series and worlds. Your AI provider keys are never included.",
  downloading: "Preparing…",
  downloadFailed: "Your data couldn't be downloaded. Try again.",
  libraryAt: "Your library lives here",
  chatsAt: "Your chats are kept here",
  backup: "To back it up",
  backupHint: "Stop lore ui and any lore mcp first, then copy the folder. To restore, put it back in the same place.",
  // Admin
  adminLink: "Open the admin page",
  openTab: (tab: string) => `Open ${tab} in Settings`,
  adminHint: "Opens in this tab.",
};

// A friendly server error carries a reference the user can quote.
export const reference = (id?: string) => (id ? ` Reference: ${id}` : "");

// Release step 6: the admin page's user controls and its log of admin actions.
export const ADMIN_USERS = {
  disable: "Disable", enable: "Enable", revoke: "Sign out everywhere", remove: "Delete",
  confirmRemove: (email: string) => `Delete ${email} and all their data? This can't be undone.`,
  yes: "Yes, delete", cancel: "Cancel",
  disabled: (email: string) => `${email} is disabled and signed out.`,
  enabled: (email: string) => `${email} can sign in again.`,
  revoked: (email: string) => `${email} is signed out everywhere.`,
  removed: (email: string) => `${email} was deleted.`,
  events: "Admin log", noEvents: "No admin actions yet.",
  eventColumns: { when: "When", admin: "Admin", action: "Action", target: "User" },
};

// The public homepage (release step 7): every string from docs/design/homepage.html, word for word. A segment list
// is a run of text with inline marks: a string is plain text; {cite} a citation chip; {mark} a highlighted quote;
// {b}, {i}, {em} bold, the glow-coloured relation and an emphasised title.
export type Seg = string | { cite: string } | { mark: string } | { b: string } | { i: string } | { em: string };

export const HOME = {
  title: "LORE · Ask your books",
  brand: "LORE",
  brandTag: "Reader",
  brandLabel: "LORE home",
  navLabel: "Main",
  navHow: "How it works",
  navBeta: "The beta",
  signIn: "Sign in",
  requestInvite: "Request an invite",
  openLore: "Open LORE",
  eyebrow: "Private beta",
  headline: "Every story is a web.",
  headlineGlow: "LORE draws it.",
  lede: "Ask about the books you own. LORE answers from the text, cites every sentence, and lights up the people and links behind the answer.",
  seeHow: "See how it answers",
  heroGraphLabel: "A graph of the first two chapters of A Study in Scarlet: Watson meets Stamford at the Criterion Bar, Stamford introduces him to Sherlock Holmes, and the two share rooms at 221B Baker Street.",
  heroGraph: {
    watson: "John Watson", stamford: "Stamford", criterion: "Criterion Bar", lab: "the hospital laboratory",
    afghanistan: "Afghanistan", maiwand: "Battle of Maiwand", murray: "Murray", baker: "221B Baker Street",
    lestrade: "Lestrade", yard: "Scotland Yard", holmes: "Sherlock Holmes",
    meetsAgain: "meets again", introduces: "introduces", sharesRooms: "shares rooms with",
  },
  youAsked: "You asked",
  scrollCue: "Scroll to see how it answers",

  howTitle: "How LORE answers a question",
  howIntro: ["Five steps, the same for every book you add. Here's one question about ", { em: "A Study in Scarlet" }, "."] as Seg[],
  steps: [
    { n: "01 · YOU ASK", h: "Ask it like you'd ask a friend who's read the book.", p: "Vague, specific, about one chapter or the whole series. LORE splits it into the parts that need answering." },
    { n: "02 · IT SEARCHES THE GRAPH", h: "It finds the people, places and links that matter.", p: "Every book becomes a map of who knows whom, where things happen and what happens when. The answer starts there, not with a guess." },
    { n: "03 · IT READS THE PASSAGES", h: "It goes back to the text.", p: "The exact passages behind those links are pulled from the book, and every fact must quote one." },
    { n: "04 · IT CHECKS EVERY SENTENCE", h: "A second pass cuts anything the book doesn't say.", p: "Every fact is checked against its passage. What the book doesn't say is cut, and you never see it." },
    { n: "05 · IT WRITES THE ANSWER", h: "Then it tells you, like a reader would.", p: "The checked facts become a short answer and a few clear paragraphs, every sentence cited. Where it offers its own reading, it says so." },
  ],
  stepsLabel: "Steps",
  stepDot: (n: number) => `Step ${n}`,
  // Read out when the scene moves to a step (the pinned card changes without a page change).
  stepNow: (n: number, total: number, heading: string) => `Step ${n} of ${total}: ${heading}`,
  progressLabel: "Step",
  progress: ["Ask", "Graph", "Passages", "Checked", "Written"],
  howQuestion: "How did Watson meet Sherlock Holmes?",
  graphLabel: "The graph",
  miniGraphLabel: "Watson met Stamford at the Criterion Bar; Stamford introduced him to Holmes",
  miniGraph: {
    watson: "Watson", stamford: "Stamford", holmes: "Sherlock Holmes", criterion: "Criterion Bar",
    afghanistan: "Afghanistan", baker: "Baker Street", lab: "the laboratory",
  },
  fromBook: "From the book",
  passages: [
    { text: ["“…I was standing at the ", { mark: "Criterion Bar" }, ", when some one tapped me on the shoulder, and turning round I recognized ", { mark: "young Stamford" }, ", who had been a dresser under me at Barts.”"] as Seg[], where: "A Study in Scarlet · ch. 1" },
    { text: ["“", { mark: "Dr. Watson, Mr. Sherlock Holmes" }, ",” said Stamford, introducing us."] as Seg[], where: "A Study in Scarlet · ch. 1" },
  ],
  checkedLabel: "The answer, checked",
  checked: [
    ["Watson runs into Stamford, who had been a dresser under him at Barts, at the Criterion Bar. ", { cite: "1" }] as Seg[],
    ["Stamford takes him to the hospital laboratory and introduces him to Sherlock Holmes. ", { cite: "2" }] as Seg[],
  ],
  cutClaim: "Holmes had already heard of Watson’s service.",
  cutWhy: "Cut: no passage says so",
  tick: "✓",
  cross: "✕",
  writtenLabel: "The written answer",
  shortAnswerLabel: "Short answer",
  shortAnswer: ["Through Stamford: an old dresser of Watson’s from Barts, met by chance at the Criterion Bar, who knew a man at the hospital laboratory looking for someone to share rooms. ", { cite: "1" }, { cite: "3" }] as Seg[],
  written: [
    ["Back in London and short of money, Watson was standing at the Criterion Bar when someone tapped him on the shoulder: young Stamford, who had been a dresser under him at Barts. ", { cite: "1" }, " When Watson said he was looking for cheap lodgings, “That’s a strange thing,” Stamford remarked: he was the second man that day to say so. The first was a fellow at the chemical laboratory up at the hospital, who couldn’t find anyone to go halves with him on some rooms. ", { cite: "3" }] as Seg[],
    ["Stamford took him there. “Dr. Watson, Mr. Sherlock Holmes,” he said, introducing them, and Holmes, gripping his hand, told him at once that he had been in Afghanistan. ", { cite: "2" }] as Seg[],
  ],
  readingKind: "LORE’s reading",
  reading: "The first thing Holmes ever does in the book is read Watson at a glance. Watson’s astonishment is the reader’s introduction to his method.",

  whyTitle: "Not another chatbot that skimmed the summary",
  whySub: "LORE only knows what's in the books you give it, and it shows its work.",
  features: [
    { num: "01", title: "Checked against the page", body: "Every sentence cites the passage it stands on, and a second pass cuts anything the passage doesn’t say. What you read, you can check.",
      eg: ["“You have been in Afghanistan, I perceive.” "] as Seg[], egNote: "ch. 1 · cited" },
    { num: "02", title: "Answers that think", body: "Ask why, or how someone changed. You get LORE’s reading and the other side, each labelled, and what the books leave open.",
      eg: [{ b: "LORE’s reading" }, " · ", { b: "The other side" }, " · ", { b: "What the books don’t settle" }] as Seg[] },
    { num: "03", title: "Explore the graph", body: "Characters, places and groups, and how they connect. Click any link to read the passage behind it.",
      eg: ["Watson —", { i: "meets again" }, "→ Stamford —", { i: "introduces" }, "→ Holmes"] as Seg[] },
    { num: "04", title: "Spoiler-safe", body: "Tell LORE which chapter you’re on. Answers, the graph and every character profile stop there, so nothing ahead leaks out.",
      eg: ["Reading position: ", { b: "ch. 2" }, " · nothing past it"] as Seg[] },
    { num: "05", title: "Your private library", body: "Your books stay in your own library, read with your own key. Nothing is shared between readers.",
      eg: ["EPUB or PDF · books you own"] as Seg[] },
  ] as { num: string; title: string; body: string; eg: Seg[]; egNote?: string }[],

  betaTitle: "How the beta works",
  betaSub: "Invite-only for now, while we let readers in in small groups.",
  betaSteps: [
    { b: "Request an invite", span: "Leave your email. We'll send a sign-in link when your spot opens." },
    { b: "Bring your own AI key", span: "Connect a key from your AI provider, so your questions run on your own account. Gemini (including its free tier), Claude and OpenAI all work today." },
    { b: "Add a book you own", span: "EPUB or PDF, up to 50 MB. LORE reads it once, then you can explore it and ask anything." },
  ],
  formTitle: "Request an invite",
  emailLabel: "Email",
  emailPlaceholder: "you@example.com",
  readingLabel: "What are you reading?",
  optional: "(optional)",
  readingPlaceholder: "The Wheel of Time, book 4",
  consent: "Email me when my invite is ready. Nothing else.",
  submit: "Request an invite",
  sending: "Sending…",
  done: "You’re on the list ✓",
  // The form's own messages (the mockup's form only said thanks).
  invalidEmail: "That doesn't look like an email address. Check it and try again.",
  needConsent: "Tick the box so we can email you when your invite is ready.",
  rateLimited: "Too many requests from here just now. Please try again in a little while.",
  offline: "We couldn't reach LORE. Check your connection and try again.",
  failed: "Something went wrong sending that. Please try again.",

  copyright: "© 2026 LORE",
  privacy: "Privacy",
  terms: "Terms",
};

// Privacy and terms (release step 7): short, plain and accurate to how LORE works. A section is a heading and its
// paragraphs; a paragraph given as a list of strings is a bulleted list.
export interface LegalSection { h: string; body: (string | string[])[] }
export const LEGAL = {
  updatedLabel: "Last updated",
  updated: "5 November 2026",
  // TODO(user): confirm this address exists and is read before the beta opens.
  contact: "privacy@jrvn.site",
  privacy: {
    title: "Privacy",
    docTitle: "Privacy · LORE",
    lede: "What LORE keeps about you, where it goes, and how to delete it, in plain English.",
    sections: [
      { h: "What we store", body: [[
        "Your account: your email address and your name.",
        "How you sign in: the Google or GitHub account, or the email address, linked to your account.",
        "Your books, in your own library, and everything LORE makes from them: the text, the graph of people, places and events, and the search index. Nothing is shared between readers.",
        "Your chats: the questions you ask and LORE’s answers.",
        "A trace of each question, for 30 days: your question, the book passages LORE read and the AI model’s replies, so that an answer you report can be looked into. Only LORE’s administrators can read traces.",
        "Reports you send with “Report this answer”, and your note.",
        "Usage counts: how many AI requests you make each day.",
        "Your AI key, encrypted. After you add it, LORE only ever shows its last four characters.",
        "Waitlist requests: your email and, if you tell us, what you’re reading.",
      ]] },
      { h: "Where it goes", body: [
        "Your books and questions go to your own AI provider, under your own key: Gemini (Google), Claude (Anthropic) or OpenAI, whichever you connect. To build a book’s graph, LORE sends the book’s text; when you ask, it sends your question and the passages it found. Your provider’s terms cover what it receives: on Gemini’s free tier, for example, Google may use it to improve its products.",
        "The search index (the embeddings) is made on our own server, not by an outside service.",
        "Sign-in and invite emails are sent through Resend, an email delivery service, which receives your email address to deliver them. If you sign in with Google or GitHub, they tell us your email address and name.",
        "Our server keeps logs of requests, including your IP address, to keep LORE running and safe.",
        "Nothing is sold or shared. There are no ads and no third-party analytics.",
      ] },
      { h: "Cookies", body: [
        "Two cookies, both needed to sign you in: a session cookie that keeps you signed in, and a CSRF cookie that stops other sites from making requests as you. While you sign in with Google or GitHub, one more cookie holds that sign-in for up to 10 minutes. There are no tracking cookies.",
        "Your browser also keeps a few display choices, such as your theme, on your own device.",
      ] },
      { h: "Keeping and deleting", body: [
        "Traces are deleted after 30 days.",
        "You can remove your AI key at any time from the account menu.",
        "Deleting a chat hides it at once and deletes its traces. Its text is removed for good when your history needs the room, or when you delete your account.",
        "“Delete my account”, in the account menu, removes everything: your library and books, chats, traces, reports, usage counts, your key and your sign-ins. A waitlist request under your email is anonymised.",
      ] },
      { h: "Contact", body: ["Questions about your data, or a request to see or delete it: write to privacy@jrvn.site."] },
    ] as LegalSection[],
  },
  terms: {
    title: "Terms",
    docTitle: "Terms · LORE",
    lede: "The few rules of LORE’s beta, in plain English.",
    sections: [
      { h: "The beta", body: [
        "LORE is in a private, invite-only beta. It is provided as is: it may have bugs or get things wrong, and it may change or stop at any time.",
        "Answers are written by an AI model from your books. LORE cites the passages behind every sentence, so check anything that matters against them.",
        "If these terms change, the date at the top changes too.",
      ] },
      { h: "Your books", body: [
        "Upload only books you own, for your own personal use. Your books stay yours: LORE uses them only to answer your questions.",
      ] },
      { h: "Your AI key", body: [
        "You are responsible for your AI key and its usage, including any limits or charges from your provider. LORE uses it only for your own books and questions.",
      ] },
      { h: "Fair use", body: [
        "Use LORE as a reader would. No abuse and no scraping: no automated or bulk requests, no attempts to reach other readers’ data, and nothing that overloads or attacks the service.",
      ] },
      { h: "Accounts", body: [
        "We can disable an account that misuses the service. You can delete yours at any time with “Delete my account”.",
      ] },
      // TODO(user): who provides LORE (name or company), the governing law and where disputes are heard.
      { h: "The legal details", body: [
        "Who provides LORE, and the law that governs these terms, will be set out here before invitations go out. Questions meanwhile: privacy@jrvn.site.",
      ] },
    ] as LegalSection[],
  },
};
