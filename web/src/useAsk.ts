import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { streamAsk } from "./api";
import { askReducer, initialState, type AskState } from "./askReducer";
import { chatsApi, failed } from "./chatsApi";
import { addToHistory, eventsOf, importBody, importDone, itemFromChat, keepLegacy, loadHistory, loadLegacy, markImportDone,
  saveHistory, type HistoryItem } from "./history";
import { loadDepth, saveDepth } from "./depth";
import { createScheduler, type Scheduler } from "./scheduler";
import type { AskEvent, Depth } from "./types";

type Action = { kind: "reset"; question: string } | { kind: "event"; event: AskEvent };
function reducer(state: AskState, action: Action): AskState {
  return action.kind === "reset" ? initialState(action.question) : askReducer(state, action.event);
}
const reduceMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

// `world` is the selected world (an id as a string, or "all"). Replaying an item asked in another world calls
// `onReplayWorld` with it, so the rest of the page follows.
export function useAsk(world: string = "all", onReplayWorld?: (world: string) => void) {
  const [state, dispatch] = useReducer(reducer, initialState());
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

  // `depth` overrides the Deep dive choice for this one question (the graph panel's Deep dive button).
  const ask = useCallback((question: string, depth?: Depth) => {
    stop();
    const asked = worldRef.current;
    last.current = { question, depth };
    dispatch({ kind: "reset", question });
    const controller = new AbortController();
    abort.current = controller;
    const sched = createScheduler((event) => dispatch({ kind: "event", event }), { reduceMotion: reduceMotion() });
    scheduler.current = sched;
    const id = `${Date.now()}`;
    setCurrent(id);
    setRun((n) => n + 1);
    (async () => {
      const events: AskEvent[] = [];
      for await (const event of streamAsk(question, controller.signal, asked, depth ?? depthRef.current)) { events.push(event); sched.push(event); }
      if (controller.signal.aborted) return;
      const answer = events.find((e) => e.type === "answer");
      if (answer && answer.type === "answer") {
        // Saved on the server: the item is that chat (its id), so delete and reopen reach it.
        const saved = answer.chat_id;
        if (saved) setCurrent(saved);
        setHistory((h) => addToHistory(h, { id: saved ?? id, question, events, askedAt: new Date().toISOString(), world: asked,
          ...(saved ? { synced: true } : {}) }));
      }
    })();
  }, []);

  const play = useCallback((item: HistoryItem) => {
    stop();
    if (item.world && item.world !== worldRef.current) onReplayWorld_.current?.(item.world);
    setCurrent(item.id);
    setRun((n) => n + 1);
    dispatch({ kind: "reset", question: item.question });
    const sched = createScheduler((event) => dispatch({ kind: "event", event }), { reduceMotion: reduceMotion(), replay: !reduceMotion() });
    scheduler.current = sched;
    item.events.forEach((e) => sched.push(e));
  }, []);

  // An item the server's list brought has no events yet: they come with the chat, once, with no model call.
  const replay = useCallback((id: string) => {
    const item = history.find((h) => h.id === id);
    if (!item) return;
    if (item.events.length || !item.synced) { play(item); return; }
    void (async () => {
      const r = await chatsApi.get(id);
      if (failed(r) || r.messages.length === 0) return;
      const events = eventsOf(r.messages[r.messages.length - 1]);
      const full = { ...item, events };
      setHistory((h) => h.map((x) => (x.id === id ? full : x)));
      play(full);
    })();
  }, [history, play]);

  // Back to the empty state (the library overview), as when the world changes.
  const clear = useCallback(() => {
    stop();
    setCurrent(null);
    setRun((n) => n + 1);
    dispatch({ kind: "reset", question: "" });
  }, []);

  // Deleting the entry on show keeps the answer on screen but no longer marks it as an entry.
  const remove = useCallback((id: string) => {
    if (historyRef.current.find((x) => x.id === id)?.synced) void chatsApi.remove(id);
    setHistory((h) => h.filter((x) => x.id !== id));
    setCurrent((c) => (c === id ? null : c));
  }, []);
  const clearHistory = useCallback(() => {
    historyRef.current.filter((x) => x.synced).forEach((x) => void chatsApi.remove(x.id));
    setHistory([]);
    setCurrent(null);
  }, []);

  const retry = useCallback(() => { if (last.current.question) ask(last.current.question, last.current.depth); }, [ask]);
  return { state, history, current, run, ask, replay, retry, clear, remove, clearHistory, depth, setDepth, busy: state.status === "running",
    importable, importLocal, declineImport };
}
