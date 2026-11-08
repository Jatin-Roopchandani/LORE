import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { streamAsk } from "./api";
import { askReducer, initialState, type AskState } from "./askReducer";
import { chatsApi, failed } from "./chatsApi";
import { addToHistory, entriesOf, importBody, importDone, itemFromChat, keepLegacy, loadHistory, loadLegacy, markImportDone,
  saveHistory, type HistoryItem, type ThreadEntry } from "./history";
import { loadDepth, saveDepth } from "./depth";
import { prefersReducedMotion } from "./settings";
import { createScheduler, type Scheduler } from "./scheduler";
import type { AskEvent, Depth } from "./types";

type Action = { kind: "reset"; question: string } | { kind: "event"; event: AskEvent };
function reducer(state: AskState, action: Action): AskState {
  return action.kind === "reset" ? initialState(action.question) : askReducer(state, action.event);
}
// The device's setting, or the Settings popup's "Reduce motion" override.
const reduceMotion = prefersReducedMotion;

const standaloneOf = (events: AskEvent[]): string | null => {
  const e = events.find((x) => x.type === "understood");
  return e && e.type === "understood" ? e.standalone : null;
};

// `world` is the selected world (an id as a string, or "all"). Replaying an item asked in another world calls
// `onReplayWorld` with it, so the rest of the page follows.
//
// A chat is a thread: `state` is its latest question (streaming, or replayed) and `thread` the earlier questions and
// answers above it, oldest first. Asking while a chat is open follows it up: the question goes with the chat's id and
// the server loads the earlier turns itself. `clear` (New chat) starts a fresh one.
export function useAsk(world: string = "all", onReplayWorld?: (world: string) => void) {
  const [state, dispatch] = useReducer(reducer, initialState());
  const [thread, setThread] = useState<ThreadEntry[]>([]);
  // The server chat a follow-up goes to (null: the next question starts a new chat).
  const chat = useRef<string | null>(null);
  // The open chat's saved entries, oldest first: the one on screen is last once it is answered.
  const entries = useRef<ThreadEntry[]>([]);
  // Chats opened or asked in this session: reopening one draws its thread with no request.
  const threads = useRef(new Map<string, ThreadEntry[]>());
  // The question on screen and its events; `done` once they are all known (answered, failed, or replayed).
  const live = useRef<{ entry: ThreadEntry; done: boolean } | null>(null);
  const asks = useRef(0);
  const [history, setHistory] = useState<HistoryItem[]>(() => loadHistory());
  const [current, setCurrent] = useState<string | null>(null);
  // Bumped by every ask, retry and replay: views key on it so each run starts fresh.
  const [run, setRun] = useState(0);
  const loaded = useRef(history);
  const historyRef = useRef(history);
  historyRef.current = history;
  // Saved outside the state updater (updaters must stay pure); the first render's list came from storage.
  useEffect(() => { if (history !== loaded.current) saveHistory(history); }, [history]);
  // How many chats saved only in this browser the one-time import offers (0: nothing to offer).
  const [importable, setImportable] = useState(0);

  // The server is the source of truth: its list replaces the cache (keeping cached events, so a replay needs no
  // request), and browser-only items are set aside for the import, offered once and only while the server has none.
  const refresh = useCallback(async () => {
    const r = await chatsApi.list(50);
    if (failed(r)) return;
    const legacy = loadLegacy();
    if (legacy.length) keepLegacy(legacy);
    setHistory((h) => {
      const byId = new Map(h.map((x) => [x.id, x]));
      return r.chats.map((c) => itemFromChat(c, byId.get(c.id)));
    });
    setImportable(!importDone() && r.chats.length === 0 ? legacy.length : 0);
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  const importLocal = useCallback(async () => {
    const legacy = loadLegacy();
    setImportable(0);
    for (const item of legacy) await chatsApi.create(importBody(item));
    markImportDone();
    await refresh();
  }, [refresh]);
  const declineImport = useCallback(() => { markImportDone(); setImportable(0); }, []);
  const abort = useRef<AbortController | null>(null);
  const scheduler = useRef<Scheduler | null>(null);
  const last = useRef<{ question: string; depth?: Depth }>({ question: "" });
  // The Deep dive choice (auto, deep or focused), remembered across visits and sent with every question.
  const [depth, setDepthState] = useState<Depth>(() => loadDepth());
  const depthRef = useRef(depth);
  depthRef.current = depth;
  const setDepth = useCallback((d: Depth) => { setDepthState(d); saveDepth(d); }, []);
  const worldRef = useRef(world);
  worldRef.current = world;
  const onReplayWorld_ = useRef(onReplayWorld);
  onReplayWorld_.current = onReplayWorld;

  const stop = () => { abort.current?.abort(); scheduler.current?.stop(); };
  useEffect(() => stop, []);

  // Starts the next question of the open chat. The finished question on screen moves up into the thread (one still
  // streaming is dropped: it was cut off). `again` (Retry) replaces the question on screen instead.
  const send = useCallback((question: string, depth: Depth | undefined, again: boolean) => {
    stop();
    const shown = live.current;
    if (!again && shown?.done) setThread((t) => [...t, shown.entry]);
    const asked = worldRef.current;
    last.current = { question, depth };
    const entry: ThreadEntry = { key: `q${++asks.current}`, question, standalone: null, events: [] };
    live.current = { entry, done: false };
    const following = chat.current;
    dispatch({ kind: "reset", question });
    const controller = new AbortController();
    abort.current = controller;
    const sched = createScheduler((event) => dispatch({ kind: "event", event }), { reduceMotion: reduceMotion() });
    scheduler.current = sched;
    const id = `${Date.now()}`;
    if (!following) setCurrent(id);
    setRun((n) => n + 1);
    (async () => {
      const events = entry.events;
      const d = depth ?? depthRef.current;
      // The chat's id only on a follow-up: a first question is sent exactly as before chats had threads.
      const stream = following ? streamAsk(question, controller.signal, asked, d, following) : streamAsk(question, controller.signal, asked, d);
      for await (const event of stream) { events.push(event); sched.push(event); }
      if (controller.signal.aborted) return;
      entry.standalone = standaloneOf(events);
      if (live.current?.entry === entry) live.current = { entry, done: true };
      const answer = events.find((e) => e.type === "answer");
      if (answer && answer.type === "answer") {
        // Saved on the server: the item is that chat (its id), so delete, reopen and follow-ups reach it.
        const saved = answer.chat_id;
        if (saved) {
          entries.current = saved === following ? [...entries.current, entry] : [entry];
          threads.current.set(saved, entries.current);
          chat.current = saved;
          setCurrent(saved);
        }
        // A chat is listed under its first question, with its latest answer's events.
        const title = saved ? entries.current[0].question : question;
        setHistory((h) => addToHistory(h, { id: saved ?? id, question: title, events, askedAt: new Date().toISOString(), world: asked,
          ...(saved ? { synced: true } : {}) }));
      }
    })();
  }, []);

  // `depth` overrides the Deep dive choice for this one question (the graph panel's Deep dive button).
  const ask = useCallback((question: string, depth?: Depth) => send(question, depth, false), [send]);

  // Draws a chat: `earlier` above, and `latest` replayed below (animated unless motion is reduced; no model call).
  const open = useCallback((id: string, itemWorld: string | undefined, earlier: ThreadEntry[], latest: ThreadEntry) => {
    stop();
    if (itemWorld && itemWorld !== worldRef.current) onReplayWorld_.current?.(itemWorld);
    setCurrent(id);
    setRun((n) => n + 1);
    setThread(earlier);
    live.current = { entry: latest, done: true };
    dispatch({ kind: "reset", question: latest.question });
    const sched = createScheduler((event) => dispatch({ kind: "event", event }), { reduceMotion: reduceMotion(), replay: !reduceMotion() });
    scheduler.current = sched;
    // A rewrite the server stored with the message, in case its events lack it (it is shown under the question).
    if (latest.standalone && !standaloneOf(latest.events)) sched.push({ type: "understood", t: 0, standalone: latest.standalone });
    latest.events.forEach((e) => sched.push(e));
  }, []);

  // A chat from the server opens whole: its messages come once (none if this session already has them), with no model
  // call. An item saved only in this browser is one question, and asking after it starts a new chat.
  const replay = useCallback((id: string) => {
    const item = history.find((h) => h.id === id);
    if (!item) return;
    const single = () => {
      chat.current = null;
      entries.current = [];
      open(item.id, item.world, [], { key: `i${item.id}`, question: item.question, standalone: standaloneOf(item.events), events: item.events });
    };
    const show = (all: ThreadEntry[]) => {
      chat.current = id;
      entries.current = all;
      open(id, item.world, all.slice(0, -1), all[all.length - 1]);
    };
    if (!item.synced) { single(); return; }
    const known = threads.current.get(id);
    if (known?.length) { show(known); return; }
    void (async () => {
      const r = await chatsApi.get(id);
      if (failed(r) || r.messages.length === 0) { if (item.events.length) single(); return; }
      const all = entriesOf(r.messages);
      threads.current.set(id, all);
      setHistory((h) => h.map((x) => (x.id === id ? { ...x, events: all[all.length - 1].events } : x)));
      show(all);
    })();
  }, [history, open]);

  // New chat: back to the empty state (the library overview), and the next question starts a fresh conversation.
  const clear = useCallback(() => {
    stop();
    chat.current = null;
    entries.current = [];
    live.current = null;
    setThread([]);
    setCurrent(null);
    setRun((n) => n + 1);
    dispatch({ kind: "reset", question: "" });
  }, []);

  // Deleting the entry on show keeps the answer on screen but no longer marks it as an entry; a question asked after
  // it starts a new chat.
  const remove = useCallback((id: string) => {
    if (historyRef.current.find((x) => x.id === id)?.synced) void chatsApi.remove(id);
    threads.current.delete(id);
    if (chat.current === id) chat.current = null;
    setHistory((h) => h.filter((x) => x.id !== id));
    setCurrent((c) => (c === id ? null : c));
  }, []);
  const clearHistory = useCallback(() => {
    historyRef.current.filter((x) => x.synced).forEach((x) => void chatsApi.remove(x.id));
    threads.current.clear();
    chat.current = null;
    setHistory([]);
    setCurrent(null);
  }, []);

  const retry = useCallback(() => { if (last.current.question) send(last.current.question, last.current.depth, true); }, [send]);
  return { state, thread, history, current, run, ask, replay, retry, clear, remove, clearHistory, depth, setDepth, busy: state.status === "running",
    importable, importLocal, declineImport };
}
