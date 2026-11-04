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

export const LIBRARY = {
  open: "Books", manage: "Manage library\u2026", title: "Library", close: "Close", loading: "Loading the library…", empty: "No books yet.",
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
  pausedKey: "Paused: your Gemini key needs attention.",
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
  askCost: "Uses the daily allowance.",
  askCostDeep: "Uses the daily allowance: a deep answer can take a few dozen requests.",
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
  newQuestion: "New question", newQuestionHint: "Clear the answer, show the library and start a fresh question.",
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
    case "key_required":
      return { text: "Add your Gemini API key to ask questions." };
    case "key_invalid":
      return { text: "Gemini refused your API key. Replace it to carry on." };
    case "too_large":
      return { text: "This answer was too large to keep in your history." };
    default:
      return { text: error.message || "Something went wrong.", retry: true };
  }
}

// Theme picker.
export const THEME = {
  button: "Theme",
  menu: "Choose a theme",
  system: "System",
  systemHint: "Follows your device",
  groups: { dark: "Dark", light: "Light" },
};

// Sign-in, the waitlist and the public pages (multi-user mode).
export const AUTH = {
  brand: "LORE",
  homeTitle: "Ask a book anything. Every sentence checked against it.",
  homeLead: "LORE builds a private knowledge graph from the books you read and answers questions with passages you can verify.",
  homeBeta: "LORE is in a private beta. Sign-in is by invitation, and you bring your own free Gemini key.",
  signIn: "Sign in",
  requestInvite: "Request an invite",
  signInTitle: "Sign in to LORE",
  signInLead: "No password to remember: use Google, GitHub or an email link.",
  invited: "You have an invitation. Sign in any way below to create your account.",
  withGoogle: "Continue with Google",
  withGithub: "Continue with GitHub",
  or: "or",
  emailLabel: "Email",
  emailPlaceholder: "you@example.com",
  sendLink: "Email me a sign-in link",
  sending: "Sending…",
  linkSent: "If that address can sign in, a link is on its way. It works once and expires in 15 minutes.",
  newHere: "New here?",
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
  waitlistUnknown: "We don't have an account for that sign-in yet. LORE is invite-only while it's in beta, so request an invite below.",
  readingLabel: "What are you reading? (optional)",
  consentLabel: "I agree that LORE may email me about my invite request.",
  submit: "Request an invite",
  thanks: "Thanks. We'll email you when there's a place for you.",
  signOut: "Sign out",
  account: "Account",
  admin: "Admin",
  back: "Back",
  privacy: "Privacy",
  terms: "Terms",
  privacyTitle: "Privacy",
  // TODO(release step 7): the full privacy page. It must keep saying what is traced and who can read it.
  privacyBody: "This page is a placeholder while the beta starts. The full privacy note arrives before anyone is invited."
    + " Each question you ask keeps a trace for 30 days (your question, the book passages LORE read, and the model's"
    + " replies) so a reported answer can be looked into; only LORE's administrators can read traces.",
  termsTitle: "Terms",
  termsBody: "This page is a placeholder while the beta starts. The full terms arrive before anyone is invited.",
  notFound: "There's nothing at this address.",
};

// Each user's own Gemini key (multi-user mode): onboarding, the account menu and the errors that lead back to them.
export const KEYS = {
  onboardingTitle: "Add your Gemini API key",
  replaceTitle: "Replace your Gemini API key",
  why: "LORE reads your books and answers your questions with Google's Gemini models. Each reader brings their own free key, so LORE costs you nothing and your allowance is yours alone.",
  getKey: "Get a free key from Google AI Studio",
  getKeyUrl: "https://aistudio.google.com/app/apikey",
  freeTier: "The free tier allows about 1,000 requests a day, and resets at midnight Pacific time. A question takes a handful; processing a book takes more.",
  privacy: "The passages LORE finds and your questions are sent to Gemini under your own key. Your key is stored encrypted and is never shown in full again.",
  rejected: "Gemini refused your saved key. It may have been deleted or expired. Paste a new one to carry on; your paused books continue once it works.",
  fieldLabel: "Gemini API key",
  placeholder: "Paste your key here",
  save: "Check and save",
  checking: "Checking…",
  saved: "Key saved.",
  continue: "Continue",
  cancel: "Cancel",
  errors: {
    key_format: "That doesn't look like a Gemini API key. Copy the whole key from Google AI Studio and paste it again.",
    key_invalid: "Gemini didn't accept that key. Check you copied all of it, or create a new key in Google AI Studio.",
    rate_limited: "Too many tries. Please wait a while and try again.",
    unreachable: "LORE isn't reachable right now. Check your connection and try again.",
  } as Record<string, string>,
  saveFailed: "The key couldn't be saved. Please try again.",
  // The account menu.
  section: "Gemini key",
  endingIn: (last4: string) => `Key ending in …${last4}`,
  none: "No key saved.",
  needsReplacing: "Gemini refused this key. Replace it to carry on.",
  unchecked: "Not confirmed yet: it will be checked on your next question.",
  replace: "Replace",
  remove: "Remove",
  removeConfirm: "Remove your key? Asking questions and processing books stop until you add one again.",
  removeYes: "Remove key",
  usage: (used: number, limit: number) => `Requests used today: ${used.toLocaleString("en")} / about ${limit.toLocaleString("en")}`,
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
  userColumns: { email: "Email", name: "Name", role: "Role", status: "Status", joined: "Joined", key: "Gemini key", today: "Requests today" },
  hasKey: "Saved", noKey: "None",
};

// The entity profile's introduction under a reading position (written only from what has been read).
export const PROFILE = {
  introLabel: "Introduction so far",
  introHint: "Written only from what you have read so far.",
};

