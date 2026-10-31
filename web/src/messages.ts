import type { ApiError, Depth, StepName } from "./types";

export interface Msg { text: string; retry?: boolean; command?: string }
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
  costNote: "A deep answer can take a few dozen requests from the daily allowance.",
  overview: "Overview",
  contents: "Contents",
  writing: "Writing…",
  claimsOnly: "Showing the checked facts; the written section didn't pass verification.",
  failed: "This section couldn't be written.",
  skipped: { budget: "Not written: the request limit for one answer was reached.",
    reserve: "Not written: the rest of today's allowance is kept for answering other questions." },
  requests: "Requests used",
  requestCount: (n: number) => `${n} ${n === 1 ? "request" : "requests"}`,
};

export const STEPS_DEEP = {
  plan: "Planning the sections",
  section: (n: number, heading: string | null) => heading ? `Section ${n}: ${heading}` : `Section ${n}`,
  group: (question: string) => question,
  lensDetail: (entities: number, relationships: number) =>
    `${entities} ${entities === 1 ? "entity" : "entities"} \u00b7 ${relationships} relationships`,
  partsLabel: "Parts of the question",
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

export const LIBRARY = {
  open: "Library", manage: "Manage library\u2026", title: "Library", close: "Close", loading: "Loading the library…", empty: "No books yet.",
  unreachable: "LORE isn't reachable. Check that `lore ui` is still running.",
  ready: "Ready", paused: "Paused", failed: "Failed", unprocessed: "Not processed yet",
  stage: (stage: string) => STAGE_LABEL[stage] ?? stage,
  progress: (done: number, total: number) => `${done} / ${total}`,
  progressLabel: (stage: string) => `${STAGE_LABEL[stage] ?? stage} progress`,
  continuesAfter: (time: string, left: string) => `Continues after ${time} (your time) \u2014 in ${left}`,
  inHM: (mins: number) => `${Math.floor(mins / 60)}h ${mins % 60}m`,
  continue: "Continue", retry: "Retry", process: "Process",
  action: (verb: string, title: string) => `${verb} ${title}`,
  pausedDefault: "Paused until the daily allowance resets.",
  failedDefault: "Processing failed.",
  startFailed: "Couldn't start processing.",
  gemini: (used: number, limit: number, reserve: number, internals: boolean) =>
    `${internals ? "Gemini requests" : "Question allowance"} today: ${used} of ${limit} used \u00b7 ${reserve} kept for answering questions`,
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
  ingestHint: "Adding books from the browser comes next. For now, run this in a terminal:",
  ingestWorldNote: "The command asks you to confirm before it creates a new world.",
  mcpNotRunning: "HTTP server not running",
  mcpRunning: "HTTP server running",
  wrongFileType: (name: string) => `${name} is not an EPUB or PDF. Choose a .epub or .pdf file.`,
};

// Example questions for the empty chat, filled with the library's most-connected entities (see examples.ts).
export const EXAMPLES = {
  connected: (a: string, b: string) => `How is ${a} connected to ${b}?`,
  about: (a: string) => `Tell me about ${a}`,
  place: (p: string) => `What happens in ${p}?`,
  group: (g: string) => `Who belongs to ${g}?`,
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
        + `${i ? "They reset" : "It resets"} at midnight Pacific${until(ctx.resetsAt, ctx.now ?? new Date())}.` };
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
      return { text: "LORE isn't reachable. Check that `lore ui` is still running.", retry: true };
    case "forbidden":
      return { text: "This page isn't allowed to talk to LORE. Open it from the address lore ui printed." };
    case "no_graph":
      return { text: error.message || "There's no graph yet. Process a book first." };
    case "bad_request":
      return { text: error.message || "That question couldn't be sent. Questions are 1 to 2,000 characters." };
    default:
      return { text: error.message || "Something went wrong.", retry: true };
  }
}
