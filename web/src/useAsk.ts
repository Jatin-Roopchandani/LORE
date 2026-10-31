import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { streamAsk } from "./api";
import { askReducer, initialState, type AskState } from "./askReducer";
import { addToHistory, loadHistory, saveHistory, type HistoryItem } from "./history";
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
  // Saved outside the state updater (updaters must stay pure); the first render's list came from storage.
  useEffect(() => { if (history !== loaded.current) saveHistory(history); }, [history]);
  const abort = useRef<AbortController | null>(null);
  const scheduler = useRef<Scheduler | null>(null);
  const last = useRef<string>("");
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

  const ask = useCallback((question: string) => {
    stop();
    const asked = worldRef.current;
    last.current = question;
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
      for await (const event of streamAsk(question, controller.signal, asked, depthRef.current)) { events.push(event); sched.push(event); }
      if (controller.signal.aborted) return;
      if (events.some((e) => e.type === "answer"))
        setHistory((h) => addToHistory(h, { id, question, events, askedAt: new Date().toISOString(), world: asked }));
    })();
  }, []);

  const replay = useCallback((id: string) => {
    const item = history.find((h) => h.id === id);
    if (!item) return;
    stop();
    if (item.world && item.world !== worldRef.current) onReplayWorld_.current?.(item.world);
    setCurrent(id);
    setRun((n) => n + 1);
    dispatch({ kind: "reset", question: item.question });
    const sched = createScheduler((event) => dispatch({ kind: "event", event }), { reduceMotion: reduceMotion(), replay: !reduceMotion() });
    scheduler.current = sched;
    item.events.forEach((e) => sched.push(e));
  }, [history]);

  // Back to the empty state (the library overview), as when the world changes.
  const clear = useCallback(() => {
    stop();
    setCurrent(null);
    setRun((n) => n + 1);
    dispatch({ kind: "reset", question: "" });
  }, []);

  const retry = useCallback(() => { if (last.current) ask(last.current); }, [ask]);
  return { state, history, current, run, ask, replay, retry, clear, depth, setDepth, busy: state.status === "running" };
}
